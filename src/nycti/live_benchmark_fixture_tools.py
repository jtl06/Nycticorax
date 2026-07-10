from __future__ import annotations

import re

from nycti.chat.run_state import ToolExecutionResult, ToolStatus
from nycti.chat.tools.parsing import (
    parse_annual_performance_arguments,
    parse_browser_extract_arguments,
    parse_channel_context_arguments,
    parse_deep_research_arguments,
    parse_extract_url_arguments,
    parse_memory_search_arguments,
    parse_price_history_arguments,
    parse_python_exec_arguments,
    parse_tool_query_argument,
    parse_tool_symbol_list_arguments,
    parse_web_search_arguments,
    parse_youtube_transcript_arguments,
)
from nycti.chat.tools.schemas import (
    ANNUAL_PERFORMANCE_TOOL_NAME,
    BROWSER_EXTRACT_TOOL_NAME,
    DEEP_RESEARCH_TOOL_NAME,
    EXTRACT_URL_TOOL_NAME,
    GET_CHANNEL_CONTEXT_TOOL_NAME,
    IMAGE_SEARCH_TOOL_NAME,
    MEMORY_SEARCH_TOOL_NAME,
    PRICE_HISTORY_TOOL_NAME,
    PYTHON_EXEC_TOOL_NAME,
    STOCK_QUOTE_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    YOUTUBE_TRANSCRIPT_TOOL_NAME,
)

_DASHBOARD_URL = "https://bench.nycti.invalid/dashboard"
_ACME_URL = "https://bench.nycti.invalid/market/acme"
_ALFA_URL = "https://bench.nycti.invalid/market/alfa-annual"
_VIDEO_URL = "https://youtu.be/benchNycti01"
_VIDEO_SOURCE_URL = "https://bench.nycti.invalid/transcripts/benchNycti01"
_OWL_IMAGE_URL = "https://bench.nycti.invalid/images/snowy-owl.jpg"
_LUMEN_URL = "https://bench.nycti.invalid/lumenos/releases/7.4"
_PORT_AZURE_URL = "https://bench.nycti.invalid/news/port-azure-bridge"
_POLICY_URL = "https://bench.nycti.invalid/policy"
_ATLAS_URL = "https://bench.nycti.invalid/databases/atlasdb"
_NOVA_URL = "https://bench.nycti.invalid/databases/novadb"
_PYRA_URL = "https://bench.nycti.invalid/pyra/3.0-migration"
_NVIDIA_EARNINGS_URL = (
    "https://investor.nvidia.com/news/press-release-details/2026/"
    "NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx"
)
_AMD_EARNINGS_URL = (
    "https://ir.amd.com/news-events/press-releases/detail/1254/"
    "amd-reports-first-quarter-2026-financial-results"
)
_NVIDIA_EARNINGS_EVIDENCE = (
    "[S1] NVIDIA Q1 fiscal 2027 results\n"
    "NVIDIA reported Q1 fiscal 2027 on May 20, 2026. Revenue was $81.615 billion "
    "and adjusted diluted EPS was $1.87. NVIDIA guided Q2 fiscal 2027 revenue to "
    f"$91.0 billion, plus or minus 2%. URL: {_NVIDIA_EARNINGS_URL}"
)
_AMD_EARNINGS_EVIDENCE = (
    "[S2] AMD Q1 2026 results\n"
    "AMD reported Q1 2026 on May 5, 2026. Revenue was $10.253 billion and adjusted "
    "diluted EPS was $1.37. AMD guided Q2 2026 revenue to approximately $11.2 "
    f"billion, plus or minus $300 million. URL: {_AMD_EARNINGS_URL}"
)
_EARNINGS_EVIDENCE_BY_URL = {
    _NVIDIA_EARNINGS_URL.casefold(): _NVIDIA_EARNINGS_EVIDENCE,
    _AMD_EARNINGS_URL.casefold(): _AMD_EARNINGS_EVIDENCE,
}


def execute_fixture_web(arguments: str) -> ToolExecutionResult:
    payload = parse_web_search_arguments(arguments)
    if payload is None:
        return _invalid_arguments(WEB_SEARCH_TOOL_NAME)
    queries = "; ".join(payload.queries)
    earnings_blocks, earnings_sources = _earnings_evidence_for_queries(payload.queries)
    if earnings_blocks:
        return ToolExecutionResult(
            content=(
                f"Tavily web results for: {queries}\n\n"
                + "\n\n".join(earnings_blocks)
            ),
            status=ToolStatus.OK,
            metrics=_web_metrics(len(payload.queries)),
            provenance=earnings_sources,
        )
    if any("port azure" in query.casefold() for query in payload.queries):
        return ToolExecutionResult(
            content=(
                f"Tavily web results for: {queries}\n\n"
                "[S1] Port Azure bridge reopens after inspection\n"
                "The Port Azure bridge reopened at 09:30 local time on July 10, 2026. "
                f"URL: {_PORT_AZURE_URL}"
            ),
            status=ToolStatus.OK,
            metrics=_web_metrics(len(payload.queries)),
            provenance=(_PORT_AZURE_URL,),
        )
    if any("pyra" in query.casefold() for query in payload.queries):
        return ToolExecutionResult(
            content=(
                f"Tavily web results for: {queries}\n\n"
                "[S1] Pyra 3.0 migration guide\n"
                "Pyra 3.0 replaced mutex sessions with lease sessions; 2.9 used mutex sessions. "
                f"URL: {_PYRA_URL}"
            ),
            status=ToolStatus.OK,
            metrics=_web_metrics(len(payload.queries)),
            provenance=(_PYRA_URL,),
        )
    if not any("lumen" in query.casefold() for query in payload.queries):
        return ToolExecutionResult(
            content="Web benchmark fixture rejected an unrelated query.",
            status=ToolStatus.ERROR,
            metrics={
                "web_search_query_count": len(payload.queries),
                "web_search_status": "invalid_inputs",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"Tavily web results for: {queries}\n\n"
            "[S1] LumenOS 7.4 release notes\n"
            "LumenOS 7.4 is the latest stable release in this benchmark fixture. "
            f"Released July 2, 2026. URL: {_LUMEN_URL}"
        ),
        status=ToolStatus.OK,
        metrics=_web_metrics(len(payload.queries)),
        provenance=(_LUMEN_URL,),
    )


def execute_fixture_url_extract(arguments: str) -> ToolExecutionResult:
    payload = parse_extract_url_arguments(arguments)
    if payload is None:
        return _invalid_arguments(EXTRACT_URL_TOOL_NAME)
    normalized_url = payload.url.rstrip("/")
    if normalized_url == _DASHBOARD_URL:
        return ToolExecutionResult(
            content=(
                "Readable extraction found only an empty JavaScript shell. "
                "Use browser rendering to read the dashboard status."
            ),
            status=ToolStatus.EMPTY,
            metrics={
                "url_extract_count": 1,
                "url_extract_status": "js_shell",
                "live_benchmark_fixture_tool_count": 1,
            },
            retryable=True,
        )
    earnings_evidence = _EARNINGS_EVIDENCE_BY_URL.get(normalized_url.casefold())
    if earnings_evidence is not None:
        return ToolExecutionResult(
            content=f"Tavily extract for: {normalized_url}\n{earnings_evidence}",
            status=ToolStatus.OK,
            metrics={
                "url_extract_count": 1,
                "url_extract_ms": 0,
                "url_extract_provider": "benchmark_fixture",
                "live_benchmark_fixture_tool_count": 1,
            },
            provenance=(normalized_url,),
        )
    if normalized_url != _POLICY_URL:
        return ToolExecutionResult(
            content=f"No benchmark fixture exists for URL: {payload.url}",
            status=ToolStatus.ERROR,
            metrics={
                "url_extract_count": 1,
                "url_extract_status": "not_found",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"Tavily extract for: {_POLICY_URL}\n"
            "Nycti fixture policy: each API key may make at most 37 requests per minute."
        ),
        status=ToolStatus.OK,
        metrics={
            "url_extract_count": 1,
            "url_extract_ms": 0,
            "url_extract_provider": "benchmark_fixture",
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_POLICY_URL,),
    )


def execute_fixture_python(arguments: str) -> ToolExecutionResult:
    code = parse_python_exec_arguments(arguments)
    if code is None:
        return _invalid_arguments(PYTHON_EXEC_TOOL_NAME)
    if "9173*62011" not in re.sub(r"\s+", "", code).casefold():
        return ToolExecutionResult(
            content="Python benchmark fixture rejected an unrelated calculation.",
            status=ToolStatus.ERROR,
            metrics={
                "python_exec_count": 1,
                "python_exec_status": "error",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content="Python result:\n568826903",
        status=ToolStatus.OK,
        metrics={
            "python_exec_count": 1,
            "python_exec_ms": 0,
            "python_exec_status": "ok",
            "live_benchmark_fixture_tool_count": 1,
        },
    )


def execute_fixture_quote(arguments: str) -> ToolExecutionResult:
    symbols = parse_tool_symbol_list_arguments(arguments)
    if not symbols:
        return _invalid_arguments(STOCK_QUOTE_TOOL_NAME)
    if "ACME" not in symbols:
        return ToolExecutionResult(
            content="Market quote fixture supports only ACME.",
            status=ToolStatus.ERROR,
            metrics={
                "stock_quote_count": 1,
                "stock_quote_status": "error",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            "Twelve Data market quote for: Acme Corp (ACME)\n"
            "Last price: $137.25 USD\n"
            "Timestamp: 2026-07-10 15:30:00 UTC"
        ),
        status=ToolStatus.OK,
        metrics={
            "stock_quote_count": 1,
            "stock_quote_symbol_count": len(symbols),
            "stock_quote_symbols": ", ".join(symbols),
            "stock_quote_status": "ok",
            "stock_quote_ms": 0,
            "market_data_provider": "benchmark_fixture",
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_ACME_URL,),
    )


def execute_fixture_deep_research(arguments: str) -> ToolExecutionResult:
    payload = parse_deep_research_arguments(arguments)
    if payload is None:
        return _invalid_arguments(DEEP_RESEARCH_TOOL_NAME)
    normalized_question = payload.question.casefold()
    earnings_question = (
        any(name in normalized_question for name in ("nvidia", "nvda"))
        and any(name in normalized_question for name in ("amd", "advanced micro devices"))
    )
    earnings_urls = {_normalize_fixture_url(value) for value in payload.urls}
    expected_earnings_urls = {
        _normalize_fixture_url(_NVIDIA_EARNINGS_URL),
        _normalize_fixture_url(_AMD_EARNINGS_URL),
    }
    earnings_inputs_valid = (
        set(payload.symbols).issubset({"NVDA", "AMD"})
        and earnings_urls.issubset(expected_earnings_urls)
        and not payload.youtube_urls
        and not payload.calculations
    )
    if earnings_question and earnings_inputs_valid:
        return ToolExecutionResult(
            content=(
                f"Deep research evidence for: {payload.question}\n"
                "Economy-model reduction of two official earnings releases:\n"
                f"{_NVIDIA_EARNINGS_EVIDENCE}\n\n{_AMD_EARNINGS_EVIDENCE}"
            ),
            status=ToolStatus.OK,
            metrics={
                "deep_research_tool_count": 1,
                "deep_research_query_count": 2,
                "deep_research_successful_query_count": 2,
                "deep_research_source_count": 2,
                "deep_research_status": "ok",
                "deep_research_model": "benchmark-economy-fixture",
                "live_benchmark_fixture_tool_count": 1,
            },
            provenance=(_NVIDIA_EARNINGS_URL, _AMD_EARNINGS_URL),
        )
    has_specialized_inputs = any(
        (payload.urls, payload.symbols, payload.youtube_urls, payload.calculations)
    )
    if has_specialized_inputs:
        inputs_ok = (
            len(payload.urls) == 1
            and {_normalize_fixture_url(value) for value in payload.urls}
            == {_normalize_fixture_url(_POLICY_URL)}
            and len(payload.symbols) == 1
            and set(payload.symbols) == {"ACME"}
            and len(payload.youtube_urls) == 1
            and {
                _normalize_fixture_url(value)
                for value in payload.youtube_urls
            }
            == {_normalize_fixture_url(_VIDEO_URL)}
            and len(payload.calculations) == 1
            and _is_expected_calculation(payload.calculations[0])
        )
        if not inputs_ok:
            return ToolExecutionResult(
                content="Composite fixture rejected mismatched specialized inputs.",
                status=ToolStatus.ERROR,
                metrics={
                    "deep_research_tool_count": 1,
                    "deep_research_status": "invalid_inputs",
                    "deep_research_invalid_input_count": 1,
                    "live_benchmark_fixture_miss_count": 1,
                },
            )
        return ToolExecutionResult(
            content=(
                f"Deep research evidence for: {payload.question}\n"
                "Composite economy-model reduction:\n"
                f"Policy limit: 37 requests per minute. URL: {_POLICY_URL}\n"
                f"ACME latest price: $137.25 USD. URL: {_ACME_URL}\n"
                "Restricted calculation result: 568826903.\n"
                "Transcript steps: inventory, shadow traffic, then cutover. "
                f"URL: {_VIDEO_SOURCE_URL}"
            ),
            status=ToolStatus.OK,
            metrics={
                "deep_research_tool_count": 1,
                "deep_research_query_count": 2,
                "deep_research_successful_query_count": 2,
                "deep_research_source_count": 3,
                "deep_research_specialized_call_count": 4,
                "deep_research_url_count": len(payload.urls),
                "deep_research_symbol_count": len(payload.symbols),
                "deep_research_transcript_count": len(payload.youtube_urls),
                "deep_research_calculation_count": len(payload.calculations),
                "deep_research_status": "ok",
                "deep_research_model": "benchmark-economy-fixture",
                "live_benchmark_fixture_tool_count": 1,
            },
            provenance=(_POLICY_URL, _ACME_URL, _VIDEO_SOURCE_URL),
        )
    if not all(name in normalized_question for name in ("atlasdb", "novadb")):
        return ToolExecutionResult(
            content="Deep-research benchmark fixture rejected an unrelated question.",
            status=ToolStatus.ERROR,
            metrics={
                "deep_research_tool_count": 1,
                "deep_research_status": "invalid_inputs",
                "deep_research_invalid_input_count": 1,
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"Deep research evidence for: {payload.question}\n"
            "Economy-model reduction: AtlasDB favors transactional consistency; "
            "NovaDB favors analytical scan throughput.\n"
            "Provenance evidence:\n"
            f"[S1] AtlasDB 3.2 technical brief\nURL: {_ATLAS_URL}\n"
            "AtlasDB sustained 18,400 audited transactions per second with serializable writes.\n"
            f"[S2] NovaDB 5.1 benchmark\nURL: {_NOVA_URL}\n"
            "NovaDB scanned 11.8 TB per minute in the published analytical benchmark."
        ),
        status=ToolStatus.OK,
        metrics={
            "deep_research_tool_count": 1,
            "deep_research_query_count": 2,
            "deep_research_successful_query_count": 2,
            "deep_research_source_count": 2,
            "deep_research_status": "ok",
            "deep_research_model": "benchmark-economy-fixture",
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_ATLAS_URL, _NOVA_URL),
    )


def _web_metrics(query_count: int) -> dict[str, int | str]:
    return {
        "web_search_query_count": query_count,
        "web_search_ms": 0,
        "live_benchmark_fixture_tool_count": 1,
    }


def _earnings_evidence_for_queries(
    queries: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = " ".join(queries).casefold()
    blocks: list[str] = []
    sources: list[str] = []
    if any(name in normalized for name in ("nvidia", "nvda")):
        blocks.append(_NVIDIA_EARNINGS_EVIDENCE)
        sources.append(_NVIDIA_EARNINGS_URL)
    if any(name in normalized for name in ("amd", "advanced micro devices")):
        blocks.append(_AMD_EARNINGS_EVIDENCE)
        sources.append(_AMD_EARNINGS_URL)
    return tuple(blocks), tuple(sources)


def execute_fixture_browser_extract(arguments: str) -> ToolExecutionResult:
    payload = parse_browser_extract_arguments(arguments)
    if payload is None:
        return _invalid_arguments(BROWSER_EXTRACT_TOOL_NAME)
    if payload.url.rstrip("/") != _DASHBOARD_URL:
        return ToolExecutionResult(
            content=f"Browser benchmark fixture has no page for: {payload.url}",
            status=ToolStatus.ERROR,
            metrics={
                "browser_extract_count": 1,
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"Browser extract for: {_DASHBOARD_URL}\n"
            "Rendered JavaScript dashboard status: all 12 services operational. "
            "Last refreshed 2026-07-10 15:30 UTC."
        ),
        status=ToolStatus.OK,
        metrics={
            "browser_extract_count": 1,
            "browser_extract_ms": 0,
            "browser_extract_headed": "yes" if payload.headed else "no",
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_DASHBOARD_URL,),
    )


def execute_fixture_price_history(arguments: str) -> ToolExecutionResult:
    payload = parse_price_history_arguments(arguments)
    if payload is None:
        return _invalid_arguments(PRICE_HISTORY_TOOL_NAME)
    if payload.symbol != "ACME":
        return ToolExecutionResult(
            content="Price-history fixture supports only ACME.",
            status=ToolStatus.ERROR,
            metrics={
                "price_history_count": 1,
                "price_history_status": "error",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            "Twelve Data price history for ACME (1day):\n"
            "2026-07-06 close $132.10\n"
            "2026-07-07 close $133.40\n"
            "2026-07-08 close $134.80\n"
            "2026-07-09 close $136.00\n"
            "2026-07-10 close $137.25"
        ),
        status=ToolStatus.OK,
        metrics={
            "price_history_count": 1,
            "price_history_candle_count": min(payload.outputsize, 5),
            "price_history_status": "ok",
            "price_history_ms": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_ACME_URL,),
    )


def execute_fixture_annual_performance(arguments: str) -> ToolExecutionResult:
    payload = parse_annual_performance_arguments(arguments)
    if payload is None:
        return _invalid_arguments(ANNUAL_PERFORMANCE_TOOL_NAME)
    if "ALFA" not in payload.symbols:
        return ToolExecutionResult(
            content="Annual-performance fixture supports only ALFA.",
            status=ToolStatus.ERROR,
            metrics={
                "annual_performance_count": 1,
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            "Calendar-year performance for ALFA:\n"
            "2024 price change: +12.5%; cash distributions: $1.20\n"
            "2025 price change: +8.2%; cash distributions: $1.35"
        ),
        status=ToolStatus.OK,
        metrics={
            "annual_performance_count": 1,
            "annual_performance_symbol_count": len(payload.symbols),
            "annual_performance_year_count": 2,
            "annual_performance_ms": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_ALFA_URL,),
    )


def execute_fixture_youtube_transcript(arguments: str) -> ToolExecutionResult:
    payload = parse_youtube_transcript_arguments(arguments)
    if payload is None:
        return _invalid_arguments(YOUTUBE_TRANSCRIPT_TOOL_NAME)
    if payload.url.rstrip("/") != _VIDEO_URL:
        return ToolExecutionResult(
            content=f"No transcript fixture exists for: {payload.url}",
            status=ToolStatus.ERROR,
            metrics={
                "youtube_transcript_count": 1,
                "youtube_transcript_status": "error",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"YouTube transcript summary for: {_VIDEO_URL}\n"
            "The speaker's three migration steps are inventory, shadow traffic, and cutover."
        ),
        status=ToolStatus.OK,
        metrics={
            "youtube_transcript_count": 1,
            "youtube_transcript_status": "ok",
            "youtube_transcript_ms": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_VIDEO_SOURCE_URL,),
    )


def execute_fixture_image_search(arguments: str) -> ToolExecutionResult:
    query = parse_tool_query_argument(arguments)
    if query is None:
        return _invalid_arguments(IMAGE_SEARCH_TOOL_NAME)
    normalized_query = query.casefold()
    if "owl" not in normalized_query or "snow" not in normalized_query:
        return ToolExecutionResult(
            content="Image-search benchmark fixture rejected an unrelated query.",
            status=ToolStatus.ERROR,
            metrics={
                "image_search_query_count": 1,
                "image_search_status": "invalid_inputs",
                "live_benchmark_fixture_miss_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            f"Tavily image results for: {query}\n"
            f"Snowy owl in flight: {_OWL_IMAGE_URL}"
        ),
        status=ToolStatus.OK,
        metrics={
            "image_search_query_count": 1,
            "image_search_ms": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
        provenance=(_OWL_IMAGE_URL,),
    )


def execute_fixture_memory_search(
    arguments: str,
    *,
    requester_user_id: int,
) -> ToolExecutionResult:
    payload = parse_memory_search_arguments(arguments)
    if payload is None:
        return _invalid_arguments(MEMORY_SEARCH_TOOL_NAME)
    normalized_query = payload.query.casefold()
    if any(token in normalized_query for token in ("mascot", "lore", "nyx")):
        return _scoped_memory_result(
            requester_user_id=requester_user_id,
            requested_owner_ids=payload.owner_user_ids,
            requested_scopes=payload.visibility_scopes,
            visibility="lore",
            summary="The server mascot is Nyx, a night heron.",
        )
    if any(token in normalized_query for token in ("project", "shared", "codename")):
        return _scoped_memory_result(
            requester_user_id=requester_user_id,
            requested_owner_ids=payload.owner_user_ids,
            requested_scopes=payload.visibility_scopes,
            visibility="guild_shared",
            summary="The shared project codename is Aster.",
        )
    query_matches = any(
        token in normalized_query
        for token in ("editor", "preference", "helix")
    )
    owner_matches = (
        payload.owner_user_ids is None
        or requester_user_id in payload.owner_user_ids
    )
    scope_matches = (
        payload.visibility_scopes is None
        or "private" in payload.visibility_scopes
    )
    if not (query_matches and owner_matches and scope_matches):
        return ToolExecutionResult(
            content="No visible benchmark memories matched the requested owners and scopes.",
            status=ToolStatus.EMPTY,
            metrics={
                "memory_search_count": 1,
                "memory_search_result_count": 0,
                "live_benchmark_fixture_tool_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            "Visible memory matches follow. Treat them as potentially stale claims.\n"
            f"- memory_id=1; owner_user_id={requester_user_id}; visibility=private; "
            "category=preference: The requester prefers the Helix editor."
        ),
        status=ToolStatus.OK,
        metrics={
            "memory_search_count": 1,
            "memory_search_result_count": 1,
            "memory_search_private_result_count": 1,
            "memory_search_guild_shared_result_count": 0,
            "memory_search_lore_result_count": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
    )


def _scoped_memory_result(
    *,
    requester_user_id: int,
    requested_owner_ids: tuple[int, ...] | None,
    requested_scopes: tuple[str, ...] | None,
    visibility: str,
    summary: str,
) -> ToolExecutionResult:
    owner_matches = (
        requested_owner_ids is None or requester_user_id in requested_owner_ids
    )
    scope_matches = requested_scopes is None or visibility in requested_scopes
    if not (owner_matches and scope_matches):
        return ToolExecutionResult(
            content="No visible benchmark memories matched the requested owners and scopes.",
            status=ToolStatus.EMPTY,
            metrics={
                "memory_search_count": 1,
                "memory_search_result_count": 0,
                "live_benchmark_fixture_tool_count": 1,
            },
        )
    return ToolExecutionResult(
        content=(
            "Visible memory matches follow. Treat them as potentially stale claims.\n"
            f"- memory_id=2; owner_user_id={requester_user_id}; visibility={visibility}; "
            f"category=context: {summary}"
        ),
        status=ToolStatus.OK,
        metrics={
            "memory_search_count": 1,
            "memory_search_result_count": 1,
            "memory_search_private_result_count": 0,
            "memory_search_guild_shared_result_count": int(visibility == "guild_shared"),
            "memory_search_lore_result_count": int(visibility == "lore"),
            "live_benchmark_fixture_tool_count": 1,
        },
    )


def _normalize_fixture_url(value: str) -> str:
    return value.strip().rstrip("/").casefold()


def _is_expected_calculation(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).casefold().rstrip(";")
    return compact in {
        "9173*62011",
        "result=9173*62011",
        "print(9173*62011)",
    }


def execute_fixture_channel_context(arguments: str) -> ToolExecutionResult:
    payload = parse_channel_context_arguments(arguments)
    if payload is None:
        return _invalid_arguments(GET_CHANNEL_CONTEXT_TOOL_NAME)
    return ToolExecutionResult(
        content=(
            "Older Discord channel context (raw, oldest to newest):\n"
            "[2026-06-12 13:05 UTC] Priya: Tentative proposal: deploy Friday, June 19 "
            "at 18:00 UTC with blue-green.\n"
            "[2026-06-12 13:12 UTC] Marcus: Lunch order is in the kitchen.\n"
            "[2026-06-12 14:10 UTC] Priya: Final decision, superseding the earlier "
            "proposal: deploy Thursday, June 18, 2026 at 16:00 UTC with a 10% canary "
            "for 30 minutes, then roll out fully if healthy.\n"
            "[2026-06-12 14:13 UTC] Marcus: I own the rollback runbook and rollback drill. "
            "I will finish both by Tuesday, June 16 at 18:00 UTC.\n"
            "[2026-06-12 14:16 UTC] Elena: I own the alert dashboard and paging checks. "
            "They are due Wednesday, June 17 at 12:00 UTC.\n"
            "[2026-06-12 14:20 UTC] Priya: Unresolved question: do mobile clients need "
            "a forced refresh after deployment?\n"
            "[2026-06-12 14:22 UTC] Priya: The final go/no-go decision is due Wednesday, "
            "June 17 at 15:00 UTC.\n"
            "[2026-06-12 14:30 UTC] Marcus: The coffee machine is broken again."
        ),
        status=ToolStatus.OK,
        metrics={
            "channel_context_fetch_count": 1,
            "channel_context_mode": payload.mode,
            "channel_context_multiplier": payload.multiplier,
            "channel_context_expand": "yes" if payload.expand else "no",
            "channel_context_status": "ok",
            "channel_context_fetch_ms": 0,
            "live_benchmark_fixture_tool_count": 1,
        },
    )


def _invalid_arguments(tool_name: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        content=f"{tool_name} benchmark fixture received invalid arguments.",
        status=ToolStatus.ERROR,
        metrics={"live_benchmark_invalid_tool_arguments_count": 1},
    )
