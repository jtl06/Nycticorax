from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

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
