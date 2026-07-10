from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nycti.chat.run_state import ToolOutcome, ToolStatus
from nycti.chat.search_policy import web_search_options_for_query
from nycti.formatting import parse_json_object_payload
from nycti.llm.types import LLMUsage
from nycti.tavily.models import TavilySearchResult

if TYPE_CHECKING:
    from nycti.llm.client import OpenAIClient
    from nycti.tavily.client import TavilyClient


PLAN_FEATURE = "deep_research_plan"
REDUCE_FEATURE = "deep_research_reduce"
PLAN_MAX_TOKENS = 320
REDUCE_MAX_TOKENS = 480
MAX_REDUCTION_INPUT_CHARS = 8_000
MAX_OUTCOME_SOURCE_EXCERPT_CHARS = 700
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_SOURCE_ID_RE = re.compile(r"\[(S\d{1,2})\]", re.IGNORECASE)
_PRIMARY_PATH_TERMS = (
    "/docs",
    "/documentation",
    "/filing",
    "/investor",
    "/newsroom",
    "/press-release",
    "/publication",
    "/research",
    "/report",
)
_PRIMARY_TITLE_TERMS = (
    "annual report",
    "documentation",
    "official",
    "press release",
    "research paper",
)
_SECONDARY_HOST_TERMS = (
    "medium.com",
    "reddit.com",
    "substack.com",
    "wikipedia.org",
)


@dataclass(frozen=True, slots=True)
class DeepResearchConfig:
    economy_model: str
    min_queries: int = 2
    max_queries: int = 4
    max_results_per_query: int = 4
    max_sources_per_query: int = 3
    max_extracts: int = 6
    max_question_chars: int = 4_000
    max_query_chars: int = 240
    max_source_excerpt_chars: int = 900
    max_extracted_chars: int = 1_800
    max_reduction_chars: int = 1_200
    max_outcome_chars: int = 6_000
    model_timeout_seconds: float = 8.0
    search_timeout_seconds: float = 12.0
    extract_timeout_seconds: float = 10.0
    overall_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.economy_model.strip():
            raise ValueError("economy_model must not be blank")
        if not 2 <= self.min_queries <= self.max_queries <= 4:
            raise ValueError("query bounds must satisfy 2 <= min_queries <= max_queries <= 4")
        if not 1 <= self.max_results_per_query <= 8:
            raise ValueError("max_results_per_query must be between 1 and 8")
        if not 1 <= self.max_sources_per_query <= self.max_results_per_query:
            raise ValueError(
                "max_sources_per_query must be between 1 and max_results_per_query"
            )
        if not 1 <= self.max_extracts <= 8:
            raise ValueError("max_extracts must be between 1 and 8")
        for name, value, minimum in (
            ("max_question_chars", self.max_question_chars, 200),
            ("max_query_chars", self.max_query_chars, 40),
            ("max_source_excerpt_chars", self.max_source_excerpt_chars, 120),
            ("max_extracted_chars", self.max_extracted_chars, 240),
            ("max_reduction_chars", self.max_reduction_chars, 160),
            ("max_outcome_chars", self.max_outcome_chars, 800),
        ):
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
        for name, timeout_value in (
            ("model_timeout_seconds", self.model_timeout_seconds),
            ("search_timeout_seconds", self.search_timeout_seconds),
            ("extract_timeout_seconds", self.extract_timeout_seconds),
            ("overall_timeout_seconds", self.overall_timeout_seconds),
        ):
            if timeout_value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class DeepResearchModelCall:
    stage: str
    feature: str
    model: str
    status: str
    latency_ms: int
    usage: LLMUsage | None = None
    query: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class DeepResearchSearchCall:
    query: str
    status: str
    latency_ms: int
    result_count: int
    error: str = ""


@dataclass(frozen=True, slots=True)
class DeepResearchExtractCall:
    source_url: str
    status: str
    latency_ms: int
    content_chars: int
    used_snippet_fallback: bool
    error: str = ""


@dataclass(frozen=True, slots=True)
class DeepResearchResult:
    question: str
    queries: tuple[str, ...]
    outcomes: tuple[ToolOutcome, ...]
    model_calls: tuple[DeepResearchModelCall, ...]
    search_calls: tuple[DeepResearchSearchCall, ...]
    extract_calls: tuple[DeepResearchExtractCall, ...]
    used_fallback: bool

    @property
    def usages(self) -> tuple[LLMUsage, ...]:
        return tuple(call.usage for call in self.model_calls if call.usage is not None)

    @property
    def has_evidence(self) -> bool:
        return any(
            outcome.status == ToolStatus.OK
            and bool(outcome.content.strip())
            and bool(outcome.provenance)
            for outcome in self.outcomes
        )

    @property
    def status(self) -> str:
        degraded = (
            self.used_fallback
            or any(call.status != "ok" for call in self.search_calls)
            or any(call.status != "ok" for call in self.extract_calls)
            or any(call.status != "ok" for call in self.model_calls)
        )
        if self.has_evidence:
            return "partial" if degraded else "ok"
        if any(call.status in {"error", "timeout", "unavailable", "skipped"} for call in self.search_calls):
            return "error"
        return "empty"

    @property
    def metrics(self) -> dict[str, int | str]:
        provenance = {
            source
            for outcome in self.outcomes
            for source in outcome.provenance
        }
        reduction_calls = [call for call in self.model_calls if call.stage == "reduction"]
        return {
            "deep_research_status": self.status,
            "deep_research_query_count": len(self.queries),
            "deep_research_successful_query_count": sum(
                outcome.status == ToolStatus.OK for outcome in self.outcomes
            ),
            "deep_research_source_count": len(provenance),
            "deep_research_search_failure_count": sum(
                call.status not in {"ok", "empty"} for call in self.search_calls
            ),
            "deep_research_search_empty_count": sum(
                call.status == "empty" for call in self.search_calls
            ),
            "deep_research_extract_count": len(self.extract_calls),
            "deep_research_extract_failure_count": sum(
                call.status != "ok" for call in self.extract_calls
            ),
            "deep_research_snippet_fallback_count": sum(
                call.used_snippet_fallback for call in self.extract_calls
            ),
            "deep_research_model_call_count": len(self.model_calls),
            "deep_research_model_failure_count": sum(
                call.status != "ok" for call in self.model_calls
            ),
            "deep_research_reduction_fallback_count": sum(
                call.status != "ok" for call in reduction_calls
            ),
            "deep_research_used_fallback": "yes" if self.used_fallback else "no",
        }


@dataclass(frozen=True, slots=True)
class _Source:
    title: str
    url: str
    canonical_url: str
    snippet: str
    published_date: str
    score: float
    discovery_order: int


@dataclass(frozen=True, slots=True)
class _SearchResult:
    query_index: int
    query: str
    sources: tuple[_Source, ...]
    call: DeepResearchSearchCall


@dataclass(frozen=True, slots=True)
class _ExtractResult:
    source: _Source
    content: str
    call: DeepResearchExtractCall


@dataclass(frozen=True, slots=True)
class _ReductionResult:
    summary: str
    selected_ids: tuple[str, ...]
    call: DeepResearchModelCall


class CompositeDeepResearchService:
    """Run a bounded, read-only web-research fan-out using an economy model."""

    def __init__(
        self,
        *,
        llm_client: OpenAIClient,
        tavily_client: TavilyClient,
        config: DeepResearchConfig,
    ) -> None:
        self.llm_client = llm_client
        self.tavily_client = tavily_client
        self.config = config

    async def research(
        self,
        question: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeepResearchResult:
        normalized_question = _cap_text(
            _compact(question),
            self.config.max_question_chars,
        )
        if not normalized_question:
            raise ValueError("research question must not be blank")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        overall_timeout = self.config.overall_timeout_seconds
        if timeout_seconds is not None:
            overall_timeout = min(overall_timeout, timeout_seconds)
        deadline = time.perf_counter() + overall_timeout

        queries, planning_call, planning_fallback = await self._plan_queries(
            normalized_question,
            deadline=deadline,
        )
        search_results = await asyncio.gather(
            *(
                self._search_one(index, query, deadline=deadline)
                for index, query in enumerate(queries)
            )
        )

        sources_by_query, unique_sources = _dedupe_and_rank_sources(
            search_results,
            max_sources_per_query=self.config.max_sources_per_query,
        )
        extract_targets = unique_sources[: self.config.max_extracts]
        extract_results = await asyncio.gather(
            *(
                self._extract_one(
                    source,
                    focus_query=_focus_query_for_source(source, search_results),
                    deadline=deadline,
                )
                for source in extract_targets
            )
        )
        extracted_by_url = {
            result.source.canonical_url: result.content
            for result in extract_results
            if result.content
        }
        evidence_by_query = {
            index: tuple(
                source
                for source in sources
                if extracted_by_url.get(source.canonical_url) or source.snippet
            )
            for index, sources in sources_by_query.items()
        }

        reductions = await asyncio.gather(
            *(
                self._reduce_one(
                    query,
                    evidence_by_query.get(index, ()),
                    extracted_by_url=extracted_by_url,
                    deadline=deadline,
                )
                for index, query in enumerate(queries)
                if evidence_by_query.get(index)
            )
        )
        reduction_by_query = {result.call.query: result for result in reductions}
        extract_calls = tuple(result.call for result in extract_results)
        outcomes = tuple(
            self._build_outcome(
                index=index,
                search_result=search_result,
                sources=evidence_by_query.get(index, ()),
                extracted_by_url=extracted_by_url,
                reduction=reduction_by_query.get(search_result.query),
                planning_fallback=planning_fallback,
                extract_calls=extract_calls,
            )
            for index, search_result in enumerate(search_results)
        )
        model_calls = (planning_call, *(result.call for result in reductions))
        used_fallback = (
            planning_fallback
            or any(call.status != "ok" for call in (result.call for result in search_results))
            or any(call.status != "ok" for call in extract_calls)
            or any(result.call.status != "ok" for result in reductions)
        )
        return DeepResearchResult(
            question=normalized_question,
            queries=queries,
            outcomes=outcomes,
            model_calls=model_calls,
            search_calls=tuple(result.call for result in search_results),
            extract_calls=extract_calls,
            used_fallback=used_fallback,
        )

    async def _plan_queries(
        self,
        question: str,
        *,
        deadline: float,
    ) -> tuple[tuple[str, ...], DeepResearchModelCall, bool]:
        started_at = time.perf_counter()
        fallback_queries = _fallback_queries(question, self.config)
        timeout = _stage_timeout(
            deadline,
            self.config.model_timeout_seconds,
        )
        if timeout is None:
            return fallback_queries, DeepResearchModelCall(
                stage="planning",
                feature=PLAN_FEATURE,
                model=self.config.economy_model,
                status="skipped",
                latency_ms=0,
                error="overall_timeout",
            ), True
        availability = getattr(self.llm_client, "is_model_available", None)
        if callable(availability) and not availability(self.config.economy_model):
            return fallback_queries, DeepResearchModelCall(
                stage="planning",
                feature=PLAN_FEATURE,
                model=self.config.economy_model,
                status="unavailable",
                latency_ms=_elapsed_ms(started_at),
                error="model_unavailable",
            ), True
        try:
            result = await asyncio.wait_for(
                self.llm_client.complete_chat(
                    model=self.config.economy_model,
                    feature=PLAN_FEATURE,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Decompose a rigorous research question into focused web searches. "
                                f"Current UTC date: {datetime.now(timezone.utc).date().isoformat()}. "
                                f"Return JSON only: {{\"queries\":[...]}} with {self.config.min_queries} "
                                f"to {self.config.max_queries} concise, non-overlapping queries. "
                                "Include relevant dates, versions, entities, comparison dimensions, "
                                "primary-source terms, and a countercheck where useful. Do not answer "
                                "the question and do not call tools."
                            ),
                        },
                        {"role": "user", "content": question},
                    ],
                    max_tokens=PLAN_MAX_TOKENS,
                    temperature=0,
                    request_timeout_seconds=timeout,
                    request_max_retries=0,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return fallback_queries, DeepResearchModelCall(
                stage="planning",
                feature=PLAN_FEATURE,
                model=self.config.economy_model,
                status="timeout",
                latency_ms=_elapsed_ms(started_at),
                error="TimeoutError",
            ), True
        except Exception as exc:
            return fallback_queries, DeepResearchModelCall(
                stage="planning",
                feature=PLAN_FEATURE,
                model=self.config.economy_model,
                status="error",
                latency_ms=_elapsed_ms(started_at),
                error=type(exc).__name__,
            ), True

        planned = _parse_queries(result.text, self.config)
        if len(planned) < self.config.min_queries:
            combined = _fill_queries(planned, fallback_queries, self.config)
            return combined, DeepResearchModelCall(
                stage="planning",
                feature=PLAN_FEATURE,
                model=self.config.economy_model,
                status="invalid",
                latency_ms=_elapsed_ms(started_at),
                usage=result.usage,
                error="invalid_query_plan",
            ), True
        return planned, DeepResearchModelCall(
            stage="planning",
            feature=PLAN_FEATURE,
            model=self.config.economy_model,
            status="ok",
            latency_ms=_elapsed_ms(started_at),
            usage=result.usage,
        ), False

    async def _search_one(
        self,
        index: int,
        query: str,
        *,
        deadline: float,
    ) -> _SearchResult:
        started_at = time.perf_counter()
        timeout = _stage_timeout(deadline, self.config.search_timeout_seconds)
        if timeout is None:
            return _SearchResult(
                query_index=index,
                query=query,
                sources=(),
                call=DeepResearchSearchCall(
                    query=query,
                    status="skipped",
                    latency_ms=0,
                    result_count=0,
                    error="overall_timeout",
                ),
            )
        raw_results: tuple[TavilySearchResult, ...] = ()
        try:
            search_options = web_search_options_for_query(
                query,
                configured_depth=getattr(self.tavily_client, "search_depth", ""),
            )
            response = await asyncio.wait_for(
                self.tavily_client.search(
                    query,
                    max_results=self.config.max_results_per_query,
                    search_depth=search_options.get("search_depth"),
                    topic=search_options.get("topic"),
                    time_range=search_options.get("time_range"),
                ),
                timeout=timeout,
            )
        except TimeoutError:
            status, error = "timeout", "TimeoutError"
        except Exception as exc:
            status, error = "error", type(exc).__name__
        else:
            status, error, raw_results = "ok", "", tuple(response.results)

        sources = _normalize_sources(
            raw_results,
            query_index=index,
            config=self.config,
        )
        if status == "ok" and not sources:
            status = "empty"
        return _SearchResult(
            query_index=index,
            query=query,
            sources=sources,
            call=DeepResearchSearchCall(
                query=query,
                status=status,
                latency_ms=_elapsed_ms(started_at),
                result_count=len(sources),
                error=error,
            ),
        )

    async def _extract_one(
        self,
        source: _Source,
        *,
        focus_query: str,
        deadline: float,
    ) -> _ExtractResult:
        started_at = time.perf_counter()
        timeout = _stage_timeout(deadline, self.config.extract_timeout_seconds)
        if timeout is None:
            return _ExtractResult(
                source=source,
                content="",
                call=DeepResearchExtractCall(
                    source_url=source.url,
                    status="skipped",
                    latency_ms=0,
                    content_chars=0,
                    used_snippet_fallback=bool(source.snippet),
                    error="overall_timeout",
                ),
            )
        try:
            response = await asyncio.wait_for(
                self.tavily_client.extract(
                    source.url,
                    query=focus_query,
                    chunks_per_source=3,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            status, error, content = "timeout", "TimeoutError", ""
        except Exception as exc:
            status, error, content = "error", type(exc).__name__, ""
        else:
            matching = next(
                (
                    entry
                    for entry in response.results
                    if _canonical_url(entry.url) == source.canonical_url
                ),
                response.results[0] if response.results else None,
            )
            content = _cap_text(
                _compact(matching.raw_content if matching is not None else ""),
                self.config.max_extracted_chars,
            )
            status, error = ("ok", "") if content else ("empty", "")
        return _ExtractResult(
            source=source,
            content=content,
            call=DeepResearchExtractCall(
                source_url=source.url,
                status=status,
                latency_ms=_elapsed_ms(started_at),
                content_chars=len(content),
                used_snippet_fallback=status != "ok" and bool(source.snippet),
                error=error,
            ),
        )

    async def _reduce_one(
        self,
        query: str,
        sources: tuple[_Source, ...],
        *,
        extracted_by_url: dict[str, str],
        deadline: float,
    ) -> _ReductionResult:
        evidence_prefix = "Focus query:\n" + query + "\n\nEvidence JSON:\n"
        source_payload = _source_payload(
            sources,
            extracted_by_url=extracted_by_url,
            max_input_chars=MAX_REDUCTION_INPUT_CHARS - len(evidence_prefix),
        )
        evidence_message = evidence_prefix + json.dumps(source_payload, ensure_ascii=False)
        fallback_ids = tuple(entry["source_id"] for entry in source_payload)
        started_at = time.perf_counter()
        timeout = _stage_timeout(deadline, self.config.model_timeout_seconds)
        if timeout is None:
            return _ReductionResult(
                summary="",
                selected_ids=fallback_ids,
                call=DeepResearchModelCall(
                    stage="reduction",
                    feature=REDUCE_FEATURE,
                    model=self.config.economy_model,
                    status="skipped",
                    latency_ms=0,
                    query=query,
                    error="overall_timeout",
                ),
            )
        availability = getattr(self.llm_client, "is_model_available", None)
        if callable(availability) and not availability(self.config.economy_model):
            return _ReductionResult(
                summary="",
                selected_ids=fallback_ids,
                call=DeepResearchModelCall(
                    stage="reduction",
                    feature=REDUCE_FEATURE,
                    model=self.config.economy_model,
                    status="unavailable",
                    latency_ms=_elapsed_ms(started_at),
                    query=query,
                    error="model_unavailable",
                ),
            )
        try:
            result = await asyncio.wait_for(
                self.llm_client.complete_chat(
                    model=self.config.economy_model,
                    feature=REDUCE_FEATURE,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Reduce bounded web evidence for another assistant. Use only the "
                                "provided excerpts. Return JSON only with `summary` and `source_ids`. "
                                "The summary must be concise, preserve conflicts/uncertainty, cite "
                                "claims with the supplied IDs like [S1], and contain no URLs. "
                                "source_ids must contain only IDs that materially support it. Do not "
                                "answer beyond the evidence and do not call tools."
                            ),
                        },
                        {
                            "role": "user",
                            "content": evidence_message,
                        },
                    ],
                    max_tokens=REDUCE_MAX_TOKENS,
                    temperature=0,
                    request_timeout_seconds=timeout,
                    request_max_retries=0,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return _reduction_fallback(
                query,
                self.config.economy_model,
                fallback_ids,
                status="timeout",
                error="TimeoutError",
                started_at=started_at,
            )
        except Exception as exc:
            return _reduction_fallback(
                query,
                self.config.economy_model,
                fallback_ids,
                status="error",
                error=type(exc).__name__,
                started_at=started_at,
            )

        summary, selected_ids = _parse_reduction(
            result.text,
            known_ids=fallback_ids,
            max_chars=self.config.max_reduction_chars,
        )
        if not summary or not selected_ids:
            fallback = _reduction_fallback(
                query,
                self.config.economy_model,
                fallback_ids,
                status="invalid",
                error="invalid_reduction",
                started_at=started_at,
            )
            return _ReductionResult(
                summary=fallback.summary,
                selected_ids=fallback.selected_ids,
                call=DeepResearchModelCall(
                    stage=fallback.call.stage,
                    feature=fallback.call.feature,
                    model=fallback.call.model,
                    status=fallback.call.status,
                    latency_ms=fallback.call.latency_ms,
                    usage=result.usage,
                    query=fallback.call.query,
                    error=fallback.call.error,
                ),
            )
        return _ReductionResult(
            summary=summary,
            selected_ids=selected_ids,
            call=DeepResearchModelCall(
                stage="reduction",
                feature=REDUCE_FEATURE,
                model=self.config.economy_model,
                status="ok",
                latency_ms=_elapsed_ms(started_at),
                usage=result.usage,
                query=query,
            ),
        )

    def _build_outcome(
        self,
        *,
        index: int,
        search_result: _SearchResult,
        sources: tuple[_Source, ...],
        extracted_by_url: dict[str, str],
        reduction: _ReductionResult | None,
        planning_fallback: bool,
        extract_calls: tuple[DeepResearchExtractCall, ...],
    ) -> ToolOutcome:
        query = search_result.query
        arguments = json.dumps({"query": query}, sort_keys=True, separators=(",", ":"))
        base_metrics: dict[str, int | str] = {
            "deep_research_query_index": index + 1,
            "deep_research_search_status": search_result.call.status,
            "deep_research_result_count": len(sources),
            "deep_research_planning_fallback": "yes" if planning_fallback else "no",
            "deep_research_extract_failure_count": sum(
                call.status != "ok" for call in extract_calls
            ),
        }
        if not sources:
            status = (
                ToolStatus.EMPTY
                if search_result.call.status in {"ok", "empty"}
                else ToolStatus.ERROR
            )
            detail = (
                "returned no usable sources"
                if status == ToolStatus.EMPTY
                else f"failed ({search_result.call.error or search_result.call.status})"
            )
            return ToolOutcome(
                call_id=f"deep-research-{index + 1}",
                tool_name="deep_research",
                arguments=arguments,
                status=status,
                content=f"Deep research search for `{query}` {detail}.",
                metrics=base_metrics,
                retryable=status == ToolStatus.ERROR,
                latency_ms=search_result.call.latency_ms,
            )

        selected_ids = reduction.selected_ids if reduction is not None else ()
        selected_indexes = {
            int(source_id[1:]) - 1
            for source_id in selected_ids
            if source_id[1:].isdigit()
        }
        selected_sources = tuple(
            source for source_index, source in enumerate(sources) if source_index in selected_indexes
        ) or sources
        content, included_sources = _render_outcome_evidence(
            query=query,
            all_sources=sources,
            selected_sources=selected_sources,
            extracted_by_url=extracted_by_url,
            reduction_summary=reduction.summary if reduction is not None else "",
            max_chars=self.config.max_outcome_chars,
        )
        if not included_sources:
            return ToolOutcome(
                call_id=f"deep-research-{index + 1}",
                tool_name="deep_research",
                arguments=arguments,
                status=ToolStatus.EMPTY,
                content=f"Deep research search for `{query}` returned no bounded evidence.",
                metrics=base_metrics,
                latency_ms=search_result.call.latency_ms,
            )
        reduction_status = reduction.call.status if reduction is not None else "not_run"
        base_metrics.update(
            {
                "deep_research_source_count": len(included_sources),
                "deep_research_reduction_status": reduction_status,
                "deep_research_snippet_source_count": sum(
                    source.canonical_url not in extracted_by_url for source in included_sources
                ),
                "deep_research_model": self.config.economy_model,
            }
        )
        return ToolOutcome(
            call_id=f"deep-research-{index + 1}",
            tool_name="deep_research",
            arguments=arguments,
            status=ToolStatus.OK,
            content=content,
            metrics=base_metrics,
            provenance=tuple(source.url for source in included_sources),
            latency_ms=search_result.call.latency_ms,
        )


def _render_outcome_evidence(
    *,
    query: str,
    all_sources: tuple[_Source, ...],
    selected_sources: tuple[_Source, ...],
    extracted_by_url: dict[str, str],
    reduction_summary: str,
    max_chars: int,
) -> tuple[str, tuple[_Source, ...]]:
    def assemble(summary: str) -> tuple[str, tuple[_Source, ...]]:
        lines = [f"Deep research evidence for: {query}"]
        if summary:
            lines.extend(("Economy-model reduction:", summary))
        lines.append("Provenance evidence:")
        included: list[_Source] = []
        for source in selected_sources:
            original_index = all_sources.index(source) + 1
            block_lines = [f"[S{original_index}] {source.title}", f"URL: {source.url}"]
            if source.published_date:
                block_lines.append(f"Published: {source.published_date}")
            block_prefix = "\n".join((*block_lines, "Excerpt: "))
            existing = "\n\n".join(lines)
            remaining = max_chars - len(existing) - 2 - len(block_prefix)
            if remaining < 80:
                break
            excerpt = extracted_by_url.get(source.canonical_url) or source.snippet
            block = block_prefix + _cap_text(
                excerpt,
                min(remaining, MAX_OUTCOME_SOURCE_EXCERPT_CHARS),
            )
            candidate = "\n\n".join((*lines, block))
            if len(candidate) > max_chars:
                break
            lines.append(block)
            included.append(source)
        return "\n\n".join(lines), tuple(included)

    content, included = assemble(reduction_summary)
    if reduction_summary and len(included) < len(selected_sources):
        content, included = assemble("")
    return content, included


def _parse_queries(text: str, config: DeepResearchConfig) -> tuple[str, ...]:
    payload = parse_json_object_payload(text)
    raw_queries = payload.get("queries") if payload is not None else None
    if not isinstance(raw_queries, list):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for raw_query in raw_queries:
        query = _cap_text(_compact(str(raw_query)), config.max_query_chars)
        key = query.casefold()
        if not query or key in seen:
            continue
        seen.add(key)
        values.append(query)
        if len(values) >= config.max_queries:
            break
    return tuple(values)


def _fallback_queries(question: str, config: DeepResearchConfig) -> tuple[str, ...]:
    suffixes = (
        "primary sources official evidence",
        "independent evidence limitations counterarguments",
        "latest data methodology comparison",
        "expert analysis conflicting findings",
    )
    queries: list[str] = [_cap_text(question, config.max_query_chars)]
    for suffix in suffixes:
        if len(queries) >= config.min_queries:
            break
        prefix_chars = max(config.max_query_chars - len(suffix) - 1, 1)
        candidate = f"{_cap_text(question, prefix_chars)} {suffix}".strip()
        if candidate.casefold() not in {query.casefold() for query in queries}:
            queries.append(candidate)
    return tuple(queries[: config.max_queries])


def _fill_queries(
    planned: tuple[str, ...],
    fallback: tuple[str, ...],
    config: DeepResearchConfig,
) -> tuple[str, ...]:
    values = list(planned)
    seen = {value.casefold() for value in values}
    for candidate in fallback:
        if candidate.casefold() in seen:
            continue
        values.append(candidate)
        seen.add(candidate.casefold())
        if len(values) >= config.min_queries:
            break
    return tuple(values[: config.max_queries])


def _normalize_sources(
    results: tuple[TavilySearchResult, ...],
    *,
    query_index: int,
    config: DeepResearchConfig,
) -> tuple[_Source, ...]:
    sources: list[_Source] = []
    seen: set[str] = set()
    for index, result in enumerate(results):
        canonical = _canonical_url(result.url)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        sources.append(
            _Source(
                title=_cap_text(_compact(result.title), 240) or "Untitled source",
                url=result.url.strip(),
                canonical_url=canonical,
                snippet=_cap_text(
                    _compact(result.content),
                    config.max_source_excerpt_chars,
                ),
                published_date=_cap_text(_compact(result.published_date), 64),
                score=float(result.score) if result.score is not None else 0.0,
                discovery_order=(query_index * config.max_results_per_query) + index,
            )
        )
    return tuple(sources)


def _dedupe_and_rank_sources(
    search_results: tuple[_SearchResult, ...] | list[_SearchResult],
    *,
    max_sources_per_query: int,
) -> tuple[dict[int, tuple[_Source, ...]], tuple[_Source, ...]]:
    best_by_url: dict[str, _Source] = {}
    urls_by_query: dict[int, list[str]] = {}
    for result in search_results:
        query_urls = urls_by_query.setdefault(result.query_index, [])
        for source in result.sources:
            query_urls.append(source.canonical_url)
            existing = best_by_url.get(source.canonical_url)
            if existing is None or _source_rank(source) > _source_rank(existing):
                best_by_url[source.canonical_url] = source

    unique_sources = tuple(
        sorted(best_by_url.values(), key=_source_rank, reverse=True)
    )
    rank_position = {
        source.canonical_url: position for position, source in enumerate(unique_sources)
    }
    sources_by_query: dict[int, tuple[_Source, ...]] = {}
    for query_index, urls in urls_by_query.items():
        deduped_urls = tuple(dict.fromkeys(urls))
        ranked_urls = sorted(deduped_urls, key=rank_position.__getitem__)
        sources_by_query[query_index] = tuple(
            best_by_url[url] for url in ranked_urls[:max_sources_per_query]
        )
    return sources_by_query, unique_sources


def _source_rank(source: _Source) -> tuple[int, float, int, int]:
    return (
        _primary_source_score(source),
        source.score,
        len(source.snippet),
        -source.discovery_order,
    )


def _primary_source_score(source: _Source) -> int:
    parsed = urlsplit(source.canonical_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    title = source.title.casefold()
    score = 0
    if host.endswith((".gov", ".edu")) or host in {"sec.gov", "www.sec.gov"}:
        score += 6
    if any(term in path for term in _PRIMARY_PATH_TERMS):
        score += 3
    if any(term in title for term in _PRIMARY_TITLE_TERMS):
        score += 2
    if any(term in host for term in _SECONDARY_HOST_TERMS):
        score -= 4
    return score


def _focus_query_for_source(
    source: _Source,
    search_results: tuple[_SearchResult, ...] | list[_SearchResult],
) -> str:
    for result in search_results:
        if any(candidate.canonical_url == source.canonical_url for candidate in result.sources):
            return result.query
    return ""


def _source_payload(
    sources: tuple[_Source, ...],
    *,
    extracted_by_url: dict[str, str],
    max_input_chars: int,
) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for index, source in enumerate(sources, start=1):
        excerpt = extracted_by_url.get(source.canonical_url) or source.snippet
        entry = {
            "source_id": f"S{index}",
            "title": source.title,
            "published_date": source.published_date,
            "excerpt": excerpt,
        }
        candidate = [*payload, entry]
        if len(json.dumps(candidate, ensure_ascii=False)) > max_input_chars:
            empty_entry = {**entry, "excerpt": ""}
            fixed_chars = len(json.dumps([*payload, empty_entry], ensure_ascii=False))
            remaining = max_input_chars - fixed_chars
            if remaining >= 120:
                entry["excerpt"] = _cap_text(excerpt, remaining)
                while (
                    len(json.dumps([*payload, entry], ensure_ascii=False)) > max_input_chars
                    and len(entry["excerpt"]) > 120
                ):
                    overflow = (
                        len(json.dumps([*payload, entry], ensure_ascii=False))
                        - max_input_chars
                    )
                    entry["excerpt"] = _cap_text(
                        entry["excerpt"],
                        max(len(entry["excerpt"]) - overflow, 120),
                    )
                if len(json.dumps([*payload, entry], ensure_ascii=False)) <= max_input_chars:
                    payload.append(entry)
            break
        payload.append(entry)
    return payload


def _parse_reduction(
    text: str,
    *,
    known_ids: tuple[str, ...],
    max_chars: int,
) -> tuple[str, tuple[str, ...]]:
    payload = parse_json_object_payload(text)
    if payload is None:
        return "", ()
    summary = _cap_text(_compact(str(payload.get("summary", ""))), max_chars)
    if not summary or _URL_RE.search(summary):
        return "", ()
    raw_ids = payload.get("source_ids")
    if not isinstance(raw_ids, list):
        return "", ()
    known = {value.upper() for value in known_ids}
    selected = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in raw_ids
            if str(value).strip().upper() in known
        )
    )
    cited = {match.group(1).upper() for match in _SOURCE_ID_RE.finditer(summary)}
    if not selected or not cited or not cited.issubset(set(selected)):
        return "", ()
    return summary, selected


def _reduction_fallback(
    query: str,
    model: str,
    fallback_ids: tuple[str, ...],
    *,
    status: str,
    error: str,
    started_at: float,
) -> _ReductionResult:
    return _ReductionResult(
        summary="",
        selected_ids=fallback_ids,
        call=DeepResearchModelCall(
            stage="reduction",
            feature=REDUCE_FEATURE,
            model=model,
            status=status,
            latency_ms=_elapsed_ms(started_at),
            query=query,
            error=error,
        ),
    )


def _canonical_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname.casefold() if parsed.hostname else ""
        port = parsed.port
    except (AttributeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not hostname:
        return None
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, hostname, path, query, ""))


def _stage_timeout(deadline: float, stage_limit: float) -> float | None:
    remaining = deadline - time.perf_counter()
    if remaining <= 0.001:
        return None
    return min(stage_limit, remaining)


def _elapsed_ms(started_at: float) -> int:
    return max(round((time.perf_counter() - started_at) * 1_000), 0)


def _compact(value: str) -> str:
    return " ".join(value.split())


def _cap_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    shortened = value[: max_chars - 3].rsplit(" ", 1)[0].rstrip()
    return f"{shortened or value[: max_chars - 3].rstrip()}..."


def stable_research_key(question: str) -> str:
    """Return a non-secret stable key suitable for cache or telemetry correlation."""

    return hashlib.sha256(_compact(question).casefold().encode()).hexdigest()[:16]
