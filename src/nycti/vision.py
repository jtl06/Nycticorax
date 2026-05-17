from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import mimetypes
import time
from urllib import request as urllib_request

from nycti.config import Settings
from nycti.formatting import (
    IMAGE_ANALYSIS_UNAVAILABLE,
    NO_IMAGE_ANALYSIS,
    build_multimodal_user_content,
    model_requires_data_uri_image_input,
)
from nycti.llm.client import LLMUsage, OpenAIClient

LOGGER = logging.getLogger(__name__)
MAX_IMAGE_DATA_URI_BYTES = 5 * 1024 * 1024
IMAGE_FETCH_TIMEOUT_SECONDS = 15


@dataclass(slots=True)
class VisionContextResult:
    text: str
    usage: LLMUsage | None
    elapsed_ms: int


class VisionContextService:
    def __init__(
        self,
        settings: Settings,
        llm_client: OpenAIClient,
        *,
        download_image_as_data_uri: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self._download_image_as_data_uri = download_image_as_data_uri or _download_image_as_data_uri

    async def build_context(
        self,
        *,
        prompt: str,
        image_attachment_urls: list[str],
        image_context_lines: list[str],
    ) -> VisionContextResult:
        if not image_attachment_urls or not self.settings.openai_vision_model:
            return VisionContextResult(text=NO_IMAGE_ANALYSIS, usage=None, elapsed_ms=0)
        vision_prompt = (
            "Describe the included Discord images for a text-only assistant. "
            "Do not answer the user's full question. Only summarize what is visibly in the images, "
            "and match observations to the provided image labels when possible.\n\n"
            f"User request:\n{prompt}\n\n"
            f"Included image context:\n{chr(10).join(image_context_lines) or '(none)'}"
        )
        started_at = time.perf_counter()
        vision_image_inputs = await self.prepare_image_inputs_for_model(
            model=self.settings.openai_vision_model,
            image_urls=image_attachment_urls,
        )
        if not vision_image_inputs:
            LOGGER.warning(
                "Vision context skipped for model %s because no image inputs remained after preprocessing.",
                self.settings.openai_vision_model,
            )
            return VisionContextResult(
                text=IMAGE_ANALYSIS_UNAVAILABLE,
                usage=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
        try:
            result = await self.llm_client.complete_chat(
                model=self.settings.openai_vision_model,
                feature="vision_context",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a vision analysis assistant. "
                            "Summarize visible image details for another assistant. "
                            "Be concrete, concise, and explicit about uncertainty."
                        ),
                    },
                    {
                        "role": "user",
                        "content": build_multimodal_user_content(vision_prompt, vision_image_inputs),
                    },
                ],
                max_tokens=min(self.settings.max_completion_tokens, 500),
                temperature=0.2,
            )
        except Exception as exc:
            LOGGER.exception(
                "Vision context generation failed for model %s with %s image(s): %s. Continuing without image analysis.",
                self.settings.openai_vision_model,
                len(image_attachment_urls),
                exc,
            )
            return VisionContextResult(
                text=IMAGE_ANALYSIS_UNAVAILABLE,
                usage=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
        return VisionContextResult(
            text=result.text.strip() or IMAGE_ANALYSIS_UNAVAILABLE,
            usage=result.usage,
            elapsed_ms=_elapsed_ms(started_at),
        )

    async def prepare_image_inputs_for_model(
        self,
        *,
        model: str | None,
        image_urls: list[str],
    ) -> list[str]:
        if not image_urls:
            return []
        if not model_requires_data_uri_image_input(model):
            return image_urls
        converted = await asyncio.gather(
            *(self._download_image_as_data_uri(url) for url in image_urls),
            return_exceptions=True,
        )
        prepared: list[str] = []
        for image_url, result in zip(image_urls, converted):
            if isinstance(result, Exception):
                LOGGER.warning(
                    "Failed to convert image URL to data URI for model %s: %s (%s)",
                    model,
                    image_url,
                    result,
                )
                continue
            if not result:
                LOGGER.warning(
                    "Failed to convert image URL to data URI for model %s: %s",
                    model,
                    image_url,
                )
                continue
            prepared.append(result)
        return prepared


async def _download_image_as_data_uri(image_url: str) -> str | None:
    return await asyncio.to_thread(_download_image_as_data_uri_sync, image_url)


def _download_image_as_data_uri_sync(image_url: str) -> str | None:
    request = urllib_request.Request(
        image_url,
        headers={"User-Agent": "Nycti/1.0"},
        method="GET",
    )
    with urllib_request.urlopen(request, timeout=IMAGE_FETCH_TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_type()
        media_type = content_type if content_type.startswith("image/") else None
        if media_type is None:
            guessed_type, _ = mimetypes.guess_type(image_url)
            if guessed_type and guessed_type.startswith("image/"):
                media_type = guessed_type
        if media_type is None:
            raise ValueError("response was not an image")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_IMAGE_DATA_URI_BYTES:
                raise ValueError(
                    f"image exceeded {MAX_IMAGE_DATA_URI_BYTES} byte data URI limit"
                )
            chunks.append(chunk)
    encoded = base64.b64encode(b"".join(chunks)).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _elapsed_ms(started_at: float) -> int:
    return round(max(time.perf_counter() - started_at, 0.0) * 1000)
