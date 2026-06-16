from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from nycti.chat.run_state import AgentPermissions
from nycti.chat.tool_runner import ToolRunner
from nycti.chat.tools.schemas import GET_CHANNEL_CONTEXT_TOOL_NAME

EARNINGS_BENCHMARK_PROMPT = (
    "As of June 13, 2026, compare the latest quarter each company has actually reported for NVIDIA "
    "and AMD; do not assume they use the same fiscal quarter or year. Start with one batched web search containing "
    'official-site queries for `site:investor.nvidia.com "NVIDIA Announces Financial Results"` and '
    '`site:ir.amd.com "AMD Reports First Quarter 2026 Financial Results"`. Then use the official indexes at '
    "https://investor.nvidia.com/financial-info/financial-reports and https://ir.amd.com/ to locate and extract each "
    "latest earnings release. For this date-pinned fixture, directly extract "
    "https://investor.nvidia.com/news/press-release-details/2026/"
    "NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx and "
    "https://ir.amd.com/news-events/press-releases/detail/1284/"
    "amd-reports-first-quarter-2026-financial-results. Never construct or guess an investor-relations URL other "
    "than the provided fixtures; search its exact title if a provided URL fails. For each company provide: "
    "the fiscal quarter and report date, actual revenue, actual adjusted/non-GAAP diluted EPS, "
    "next-quarter revenue guidance, and at least one direct official investor-relations or SEC source URL. "
    "Do not substitute analyst estimates for reported actuals. If a required value is unavailable, say so explicitly."
)

CONTEXT_BENCHMARK_PROMPT = (
    "Run the deterministic Discord-context benchmark. The visible message window is intentionally incomplete. "
    "Use `channel_ctx` to retrieve older messages, then summarize only the final deployment plan. Include the final "
    "rollout method and deployment date/time, Marcus's task and due date, Elena's task and due date, the unresolved "
    "mobile-client question, and the go/no-go deadline. Later decisions supersede earlier proposals. Ignore off-topic "
    "chatter and use only Discord context; external research is forbidden."
)

SPACEX_PRICE_BENCHMARK_PROMPT = (
    "Benchmark current company/ticker grounding for this short Discord-style ask: "
    "'whats the price of spacex?' Treat this as a live/current market question. Use tools before answering. "
    "A stale answer that says SpaceX is private, has no ticker, or has no public price without checking current "
    "sources should fail. Answer with the current public ticker/status and price or clearly grounded uncertainty."
)

CONTEXT_BENCHMARK_HISTORY = """Older Discord channel context (raw, oldest to newest):
[2026-06-12 13:05 UTC] Priya: Tentative proposal: deploy Friday, June 19 at 18:00 UTC with blue-green.
[2026-06-12 13:12 UTC] Marcus: Lunch order is in the kitchen.
[2026-06-12 14:10 UTC] Priya: Final decision, superseding the earlier proposal: deploy Thursday, June 18, 2026 at 16:00 UTC with a 10% canary for 30 minutes, then roll out fully if healthy.
[2026-06-12 14:13 UTC] Marcus: I own the rollback runbook and rollback drill. I will finish both by Tuesday, June 16 at 18:00 UTC.
[2026-06-12 14:16 UTC] Elena: I own the alert dashboard and paging checks. They are due Wednesday, June 17 at 12:00 UTC.
[2026-06-12 14:20 UTC] Priya: Unresolved question: do mobile clients need a forced refresh after deployment?
[2026-06-12 14:22 UTC] Priya: The final go/no-go decision is due Wednesday, June 17 at 15:00 UTC.
[2026-06-12 14:30 UTC] Marcus: The coffee machine is broken again."""

URL_RE = re.compile(r"https?://[^\s<>\])]+", re.IGNORECASE)
DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},?\s+20\d{2}\b",
    re.IGNORECASE,
)
FISCAL_RE = re.compile(
    r"\bQ[1-4]\s+(?:(?:FY|fiscal)\s*)?20\d{2}\b"
    r"|\b(?:first|second|third|fourth)\s+(?:fiscal\s+)?quarter\b",
    re.IGNORECASE,
)
REVENUE_RE = re.compile(
    r"(?:revenue.{0,60}(?:\$|USD\s*)\d|(?:\$|USD\s*)\d.{0,60}revenue)",
    re.IGNORECASE | re.DOTALL,
)
ADJUSTED_EPS_RE = re.compile(
    r"(?:adjusted|non[- ]?GAAP).{0,60}(?:diluted\s+)?EPS.{0,40}(?:\$|USD\s*)?\d"
    r"|(?:diluted\s+)?EPS.{0,40}(?:\$|USD\s*)?\d.{0,60}(?:adjusted|non[- ]?GAAP)",
    re.IGNORECASE | re.DOTALL,
)
GUIDANCE_RE = re.compile(
    r"(?:guidance|outlook|expects?|forecast).{0,160}revenue.{0,80}(?:\$|USD\s*)\d"
    r"|(?:next[- ]quarter|Q[1-4](?:\s+(?:FY|fiscal)\s*\d{2,4})?)\s+"
    r"(?:revenue\s+)?guidance.{0,80}(?:\$|USD\s*)\d",
    re.IGNORECASE | re.DOTALL,
)
FAILURE_RE = re.compile(
    r"\b(?:couldn't synthesize|could not synthesize|please retry|unable to answer|no usable|"
    r"final synthesis failed)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EarningsCompanyScore:
    company: str
    report_period_and_date: bool
    actual_revenue: bool
    adjusted_eps: bool
    revenue_guidance: bool
    official_source: bool
    correct_report_period_and_date: bool = False
    correct_actual_revenue: bool = False
    correct_adjusted_eps: bool = False
    correct_revenue_guidance: bool = False
    correct_official_source: bool = False

    @property
    def points(self) -> int:
        return sum(
            (
                self.report_period_and_date,
                self.actual_revenue,
                self.adjusted_eps,
                self.revenue_guidance,
                self.official_source,
            )
        )

    @property
    def missing(self) -> tuple[str, ...]:
        fields = (
            ("report period/date", self.report_period_and_date),
            ("actual revenue", self.actual_revenue),
            ("adjusted EPS", self.adjusted_eps),
            ("revenue guidance", self.revenue_guidance),
            ("official source", self.official_source),
        )
        return tuple(name for name, present in fields if not present)

    @property
    def correctness_points(self) -> int:
        return sum(
            (
                self.correct_report_period_and_date,
                self.correct_actual_revenue,
                self.correct_adjusted_eps,
                self.correct_revenue_guidance,
                self.correct_official_source,
            )
        )

    @property
    def incorrect(self) -> tuple[str, ...]:
        fields = (
            ("report period/date", self.report_period_and_date, self.correct_report_period_and_date),
            ("actual revenue", self.actual_revenue, self.correct_actual_revenue),
            ("adjusted EPS", self.adjusted_eps, self.correct_adjusted_eps),
            ("revenue guidance", self.revenue_guidance, self.correct_revenue_guidance),
            ("official source", self.official_source, self.correct_official_source),
        )
        return tuple(name for name, present, correct in fields if present and not correct)


@dataclass(frozen=True, slots=True)
class EarningsBenchmarkScore:
    companies: tuple[EarningsCompanyScore, EarningsCompanyScore]
    correctness_checks: int
    correctness_total: int = 10

    @property
    def completeness_points(self) -> int:
        return sum(company.points for company in self.companies)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(
            f"{company.company} {field}"
            for company in self.companies
            for field in company.missing
        )

    @property
    def incorrect(self) -> tuple[str, ...]:
        return tuple(
            f"{company.company} {field}"
            for company in self.companies
            for field in company.incorrect
        )


@dataclass(frozen=True, slots=True)
class ContextBenchmarkScore:
    final_plan: bool
    marcus_task: bool
    elena_task: bool
    unresolved_question: bool
    go_no_go_deadline: bool
    used_channel_context: bool
    avoided_web_search: bool
    avoided_superseded_plan: bool

    @property
    def points(self) -> int:
        return sum(
            (
                self.final_plan,
                self.marcus_task,
                self.elena_task,
                self.unresolved_question,
                self.go_no_go_deadline,
                self.used_channel_context,
                self.avoided_web_search,
                self.avoided_superseded_plan,
            )
        )

    @property
    def failed(self) -> tuple[str, ...]:
        checks = (
            ("final plan", self.final_plan),
            ("Marcus task", self.marcus_task),
            ("Elena task", self.elena_task),
            ("unresolved question", self.unresolved_question),
            ("go/no-go deadline", self.go_no_go_deadline),
            ("channel_ctx used", self.used_channel_context),
            ("external research avoided", self.avoided_web_search),
            ("superseded plan omitted", self.avoided_superseded_plan),
        )
        return tuple(name for name, passed in checks if not passed)


@dataclass(frozen=True, slots=True)
class CurrentPriceBenchmarkScore:
    used_tool: bool
    used_web_or_quote: bool
    mentions_spacex_or_spcx: bool
    includes_price_or_grounded_uncertainty: bool
    avoids_stale_private_claim: bool
    avoids_token_confusion: bool

    @property
    def points(self) -> int:
        return sum(
            (
                self.used_tool,
                self.used_web_or_quote,
                self.mentions_spacex_or_spcx,
                self.includes_price_or_grounded_uncertainty,
                self.avoids_stale_private_claim,
                self.avoids_token_confusion,
            )
        )

    @property
    def failed(self) -> tuple[str, ...]:
        checks = (
            ("tool used", self.used_tool),
            ("web or quote used", self.used_web_or_quote),
            ("SpaceX/SPCX mentioned", self.mentions_spacex_or_spcx),
            ("price or grounded uncertainty", self.includes_price_or_grounded_uncertainty),
            ("stale private/no ticker claim avoided", self.avoids_stale_private_claim),
            ("token confusion avoided", self.avoids_token_confusion),
        )
        return tuple(name for name, passed in checks if not passed)


class ContextBenchmarkToolExecutor:
    async def execute(
        self,
        *,
        tool_name: str,
        arguments: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        source_message_id: int | None,
        permissions: AgentPermissions,
        run_id: str,
        step_index: int,
    ) -> tuple[str, dict[str, int | str]]:
        del (
            arguments,
            guild_id,
            channel_id,
            user_id,
            source_message_id,
            permissions,
            run_id,
            step_index,
        )
        if tool_name == GET_CHANNEL_CONTEXT_TOOL_NAME:
            return CONTEXT_BENCHMARK_HISTORY, {
                "channel_context_fetch_count": 1,
                "channel_context_fetch_ms": 0,
                "channel_context_status": "benchmark_fixture",
            }
        return (
            f"{tool_name} failed because external tools are disabled in the context benchmark.",
            {"context_benchmark_unexpected_tool_count": 1},
        )


def build_context_benchmark_tool_runner() -> ToolRunner:
    return ToolRunner(ContextBenchmarkToolExecutor())


def score_earnings_benchmark(answer: str) -> EarningsBenchmarkScore:
    nvidia_segment = _company_segment(answer, ("nvidia", "nvda"), ("amd", "advanced micro devices"))
    amd_segment = _company_segment(answer, ("amd", "advanced micro devices"), ("nvidia", "nvda"))
    companies = (
        _score_company(
            "NVIDIA",
            nvidia_segment,
            official_hosts=("investor.nvidia.com", "sec.gov"),
            expected_period_re=r"\b(?:Q1\s+(?:FY|fiscal)\s*2027|first\s+quarter\s+(?:of\s+)?fiscal\s+2027)\b",
            expected_date_re=r"\bMay\s+20,?\s+2026\b",
            expected_revenue_re=r"(?:\$81\.6(?:15)?\s*billion|\$81,615\b)",
            expected_eps_re=r"\$1\.87\b",
            expected_guidance_re=(
                r"(?:\$91(?:\.0)?\s*billion.{0,40}(?:\+/-|±|plus\s+or\s+minus)\s*2\s*%"
                r"|(?:\$89\.18\s*(?:billion|B).{0,40}\$92\.8\s*(?:billion|B)))"
            ),
        ),
        _score_company(
            "AMD",
            amd_segment,
            official_hosts=("ir.amd.com", "sec.gov"),
            expected_period_re=r"\b(?:Q1|first\s+quarter)\s+(?:fiscal\s+)?2026\b",
            expected_date_re=r"\bMay\s+0?5,?\s+2026\b",
            expected_revenue_re=r"(?:\$10\.3(?:\s*billion)?|\$10,253\b)",
            expected_eps_re=r"\$1\.37\b",
            expected_guidance_re=(
                r"\$11\.2\s*billion.{0,50}(?:\+/-|±|plus\s+or\s+minus)\s*\$?300\s*(?:million|M)\b"
            ),
        ),
    )
    return EarningsBenchmarkScore(
        companies=companies,
        correctness_checks=(
            sum(company.correctness_points for company in companies)
            if not FAILURE_RE.search(answer)
            else 0
        ),
    )


def score_context_benchmark(
    answer: str,
    metrics: dict[str, int | str],
) -> ContextBenchmarkScore:
    normalized = answer.casefold()
    final_plan = bool(
        re.search(r"\b(?:june\s+18|18\s+june)\b", answer, re.IGNORECASE)
        and re.search(r"\b(?:16:00|4:00\s*p\.?m\.?)\s*(?:utc)?\b", answer, re.IGNORECASE)
        and re.search(r"\b(?:10%\s+)?canary\b", answer, re.IGNORECASE)
    )
    marcus_task = bool(
        "marcus" in normalized
        and "rollback" in normalized
        and "runbook" in normalized
        and re.search(r"\b(?:june\s+16|16\s+june)\b", answer, re.IGNORECASE)
    )
    elena_task = bool(
        "elena" in normalized
        and "alert" in normalized
        and ("paging" in normalized or "page" in normalized)
        and re.search(r"\b(?:june\s+17|17\s+june)\b", answer, re.IGNORECASE)
    )
    unresolved_question = bool(
        "mobile" in normalized
        and "forced refresh" in normalized
        and re.search(r"\b(?:unresolved|open question|decide|whether)\b", answer, re.IGNORECASE)
    )
    go_no_go_deadline = bool(
        re.search(r"\bgo[/-]no[- ]go\b", answer, re.IGNORECASE)
        and re.search(r"\b(?:june\s+17|17\s+june)\b", answer, re.IGNORECASE)
        and re.search(r"\b(?:15:00|3:00\s*p\.?m\.?)\s*(?:utc)?\b", answer, re.IGNORECASE)
    )
    return ContextBenchmarkScore(
        final_plan=final_plan,
        marcus_task=marcus_task,
        elena_task=elena_task,
        unresolved_question=unresolved_question,
        go_no_go_deadline=go_no_go_deadline,
        used_channel_context=int(metrics.get("channel_context_fetch_count", 0)) > 0,
        avoided_web_search=int(metrics.get("web_search_query_count", 0)) == 0,
        avoided_superseded_plan=not bool(
            re.search(r"\b(?:june\s+19|19\s+june|blue-green)\b", answer, re.IGNORECASE)
        ),
    )


def score_current_price_benchmark(
    answer: str,
    metrics: dict[str, int | str],
) -> CurrentPriceBenchmarkScore:
    normalized = answer.casefold()
    stale_private_claim = bool(
        re.search(r"\bspacex\s+is\s+private\b", answer, re.IGNORECASE)
        or re.search(r"\b(?:no|not)\s+(?:public\s+)?(?:ticker|public\s+price|stock)\b", answer, re.IGNORECASE)
    )
    token_confusion = bool(
        ("token" in normalized or "crypto" in normalized)
        and not re.search(r"\b(?:not|unofficial|ignore|unless)\b.{0,80}\b(?:token|crypto)\b", answer, re.IGNORECASE)
    )
    includes_price = bool(
        re.search(r"(?:\$|USD\s*)\d+(?:\.\d+)?", answer)
        or re.search(r"\b(?:couldn't verify|could not verify|no reliable current price|not enough evidence)\b", answer, re.IGNORECASE)
    )
    web_queries = int(metrics.get("web_search_query_count", 0))
    quote_calls = int(metrics.get("stock_quote_count", 0))
    tool_calls = int(metrics.get("agent_tool_call_count", metrics.get("tool_call_count", 0)))
    return CurrentPriceBenchmarkScore(
        used_tool=tool_calls > 0 or web_queries > 0 or quote_calls > 0,
        used_web_or_quote=web_queries > 0 or quote_calls > 0,
        mentions_spacex_or_spcx=bool(re.search(r"\b(?:spacex|spcx)\b", answer, re.IGNORECASE)),
        includes_price_or_grounded_uncertainty=includes_price,
        avoids_stale_private_claim=not stale_private_claim,
        avoids_token_confusion=not token_confusion,
    )


def format_current_price_benchmark_score(
    score: CurrentPriceBenchmarkScore,
    metrics: dict[str, int | str],
) -> str:
    failed = ", ".join(score.failed) if score.failed else "none"
    lines = [
        "current_price_benchmark",
        f"score={score.points}/6 failed={failed}",
        (
            f"turns={metrics.get('agent_model_turn_count', 0)} "
            f"tools={metrics.get('agent_tool_call_count', 0)} "
            f"web_queries={metrics.get('web_search_query_count', 0)} "
            f"quotes={metrics.get('stock_quote_count', 0)} "
            f"tokens={metrics.get('chat_total_tokens', 0)} "
            f"latency_ms={metrics.get('end_to_end_ms', 0)}"
        ),
    ]
    return "```text\n" + "\n".join(lines) + "\n```"


def format_context_benchmark_score(
    score: ContextBenchmarkScore,
    metrics: dict[str, int | str],
) -> str:
    failed = ", ".join(score.failed) if score.failed else "none"
    lines = [
        "context_benchmark",
        f"score={score.points}/8 failed={failed}",
        (
            f"turns={metrics.get('agent_model_turn_count', 0)} "
            f"tools={metrics.get('agent_tool_call_count', 0)} "
            f"ctx_calls={metrics.get('channel_context_fetch_count', 0)} "
            f"web_queries={metrics.get('web_search_query_count', 0)} "
            f"tokens={metrics.get('chat_total_tokens', 0)} "
            f"latency_ms={metrics.get('end_to_end_ms', 0)}"
        ),
    ]
    return "```text\n" + "\n".join(lines) + "\n```"


def format_earnings_benchmark_score(
    score: EarningsBenchmarkScore,
    metrics: dict[str, int | str],
) -> str:
    missing = ", ".join(score.missing) if score.missing else "none"
    incorrect = ", ".join(score.incorrect) if score.incorrect else "none"
    retries = (
        int(metrics.get("native_tool_fallback_count", 0))
        + int(metrics.get("chat_continuation_count", 0))
        + int(metrics.get("agent_correction_count", 0))
    )
    lines = [
        "earnings_benchmark",
        (
            f"completeness={score.completeness_points}/10 "
            f"correctness_checks={score.correctness_checks}/{score.correctness_total}"
        ),
        f"missing={missing}",
        f"incorrect={incorrect}",
        (
            f"turns={metrics.get('agent_model_turn_count', 0)} "
            f"tools={metrics.get('agent_tool_call_count', 0)} "
            f"retries={retries} "
            f"tokens={metrics.get('chat_total_tokens', 0)} "
            f"latency_ms={metrics.get('end_to_end_ms', 0)}"
        ),
    ]
    return "```text\n" + "\n".join(lines) + "\n```"


def _score_company(
    company: str,
    segment: str,
    *,
    official_hosts: tuple[str, ...],
    expected_period_re: str,
    expected_date_re: str,
    expected_revenue_re: str,
    expected_eps_re: str,
    expected_guidance_re: str,
) -> EarningsCompanyScore:
    urls = URL_RE.findall(segment)
    official_source = any(
        any(host == domain or host.endswith("." + domain) for domain in official_hosts)
        for host in (_url_host(url) for url in urls)
    )
    return EarningsCompanyScore(
        company=company,
        report_period_and_date=bool(FISCAL_RE.search(segment) and DATE_RE.search(segment)),
        actual_revenue=bool(REVENUE_RE.search(segment)),
        adjusted_eps=bool(ADJUSTED_EPS_RE.search(segment)),
        revenue_guidance=bool(GUIDANCE_RE.search(segment)),
        official_source=official_source,
        correct_report_period_and_date=bool(
            re.search(expected_period_re, segment, re.IGNORECASE)
            and re.search(expected_date_re, segment, re.IGNORECASE)
        ),
        correct_actual_revenue=bool(re.search(expected_revenue_re, segment, re.IGNORECASE)),
        correct_adjusted_eps=bool(re.search(expected_eps_re, segment, re.IGNORECASE)),
        correct_revenue_guidance=bool(
            re.search(expected_guidance_re, segment, re.IGNORECASE | re.DOTALL)
        ),
        correct_official_source=official_source,
    )


def _company_segment(
    answer: str,
    aliases: tuple[str, ...],
    other_aliases: tuple[str, ...],
) -> str:
    start_matches = [
        match
        for alias in aliases
        if (match := re.search(rf"\b{re.escape(alias)}\b", answer, re.IGNORECASE))
    ]
    if not start_matches:
        return ""
    start = min(match.start() for match in start_matches)
    end_matches = [
        match
        for alias in other_aliases
        if (
            match := re.search(
                rf"\b{re.escape(alias)}\b",
                answer[start + 1 :],
                re.IGNORECASE,
            )
        )
    ]
    end = start + 1 + min(match.start() for match in end_matches) if end_matches else len(answer)
    return answer[start:end]


def _url_host(url: str) -> str:
    return (urlparse(url.rstrip(".,")).hostname or "").casefold()
