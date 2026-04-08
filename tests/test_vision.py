import unittest
import sys
import types
from types import SimpleNamespace

from nycti.formatting import IMAGE_ANALYSIS_UNAVAILABLE, NO_IMAGE_ANALYSIS

fake_openai = types.ModuleType("openai")


class AsyncOpenAI:  # pragma: no cover - import shim for unit tests
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.embeddings = types.SimpleNamespace(create=None)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=None))


fake_openai.AsyncOpenAI = AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from nycti.llm.client import LLMResult, LLMUsage
from nycti.vision import VisionContextService


class _FakeLLMClient:
    def __init__(self, result: LLMResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def complete_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class VisionContextServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_context_returns_no_analysis_without_vision_model(self) -> None:
        service = VisionContextService(
            SimpleNamespace(openai_vision_model=None, max_completion_tokens=350),
            _FakeLLMClient(),
        )
        result = await service.build_context(
            prompt="what is this",
            image_attachment_urls=["https://cdn.example.com/a.png"],
            image_context_lines=["- image 1: current message from mat"],
        )
        self.assertEqual(result.text, NO_IMAGE_ANALYSIS)
        self.assertEqual(result.elapsed_ms, 0)
        self.assertIsNone(result.usage)

    async def test_build_context_uses_llm_with_prepared_images(self) -> None:
        llm_client = _FakeLLMClient(
            result=LLMResult(
                text="image 1 shows a chart",
                usage=LLMUsage(
                    feature="vision_context",
                    model="vision-model",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    estimated_cost_usd=0.01,
                ),
            )
        )

        async def _fake_download(url: str) -> str | None:
            return f"data:image/png;base64,{url.rsplit('/', 1)[-1]}"

        service = VisionContextService(
            SimpleNamespace(
                openai_vision_model="https://clarifai.com/gcp/generate/models/gemini-3-flash-preview",
                max_completion_tokens=350,
            ),
            llm_client,
            download_image_as_data_uri=_fake_download,
        )
        result = await service.build_context(
            prompt="describe it",
            image_attachment_urls=["https://cdn.example.com/a.png"],
            image_context_lines=["- image 1: current message from mat"],
        )
        self.assertEqual(result.text, "image 1 shows a chart")
        self.assertIsNotNone(result.usage)
        self.assertEqual(len(llm_client.calls), 1)
        content = llm_client.calls[0]["messages"][1]["content"]
        self.assertIsInstance(content, list)
        assert isinstance(content, list)
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,a.png"}},
        )

    async def test_build_context_returns_unavailable_when_preprocessing_drops_everything(self) -> None:
        async def _drop_all(_: str) -> str | None:
            return None

        llm_client = _FakeLLMClient()
        service = VisionContextService(
            SimpleNamespace(
                openai_vision_model="https://clarifai.com/gcp/generate/models/gemini-3-flash-preview",
                max_completion_tokens=350,
            ),
            llm_client,
            download_image_as_data_uri=_drop_all,
        )
        result = await service.build_context(
            prompt="describe it",
            image_attachment_urls=["https://cdn.example.com/a.png"],
            image_context_lines=["- image 1: current message from mat"],
        )
        self.assertEqual(result.text, IMAGE_ANALYSIS_UNAVAILABLE)
        self.assertEqual(llm_client.calls, [])

    async def test_prepare_image_inputs_returns_original_urls_for_normal_models(self) -> None:
        service = VisionContextService(
            SimpleNamespace(openai_vision_model=None, max_completion_tokens=350),
            _FakeLLMClient(),
        )
        self.assertEqual(
            await service.prepare_image_inputs_for_model(
                model="gpt-4.1-mini",
                image_urls=["https://cdn.example.com/a.png"],
            ),
            ["https://cdn.example.com/a.png"],
        )

    async def test_build_context_returns_unavailable_on_llm_error(self) -> None:
        service = VisionContextService(
            SimpleNamespace(openai_vision_model="vision-model", max_completion_tokens=350),
            _FakeLLMClient(error=RuntimeError("boom")),
        )
        result = await service.build_context(
            prompt="describe it",
            image_attachment_urls=["https://cdn.example.com/a.png"],
            image_context_lines=["- image 1: current message from mat"],
        )
        self.assertEqual(result.text, IMAGE_ANALYSIS_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
