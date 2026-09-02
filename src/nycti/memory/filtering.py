from __future__ import annotations

import math
import re
from collections import Counter

ALLOWED_MEMORY_CATEGORIES = {"preference", "plan", "project", "lore"}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "but",
    "for",
    "from",
    "have",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "just",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "so",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
    "you",
    "your",
}
LOW_VALUE_PATTERNS = (
    re.compile(r"^(lol|lmao|lmfao|ok|okay|nice|cool|bet|yup|nope|same|true|wtf)[!. ]*$", re.I),
    re.compile(r"^(ha){2,}[!. ]*$", re.I),
)
TRANSIENT_MEMORY_PATTERNS = (
    re.compile(r"\bfree (?:phone|iphone|apple watch|watch)\b", re.I),
    re.compile(r"\b(?:deal|deals|promotion|promotions|promo|discount|discounts|coupon|coupons|offer|offers|trade[- ]?in)\b", re.I),
    re.compile(r"\b(?:official|product) page links?\b", re.I),
    re.compile(r"\b(?:phone|data|carrier|cell)\s+plans?\b", re.I),
    re.compile(r"\bfilter(?:ed|ing)? out\b.*\b(?:plan|plans|carrier|carriers|network|networks)\b", re.I),
)
SENSITIVE_PATTERNS = (
    re.compile(r"\b(password|passcode|api[\s_-]?key|secret|token|private key|seed phrase)\b", re.I),
    re.compile(r"\bssn\b|\bsocial security\b", re.I),
    re.compile(r"\bcredit card\b|\bdebit card\b|\bcvv\b", re.I),
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(
        r"\b(?:own|hold|holding|bought|sold)\s+\d+(?:\.\d+)?\s+"
        r"(?:shares?|contracts?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:position size|cost basis|account balance|brokerage account)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:net worth|annual income|salary|compensation|rsus?|vesting|financial goal|"
        r"retirement wealth)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:weighs?|body weight|medical condition|diagnos(?:is|ed)|medication|"
        r"health condition)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:converting|converted|conversion)\s+to\s+"
        r"(?:islam|christianity|judaism|hinduism|buddhism)\b",
        re.I,
    ),
    re.compile(r"\b(?:cannot|can't|may not)\s+trade\b", re.I),
)
USEFUL_SIGNAL_PATTERNS = (
    re.compile(r"\b(i like|i love|i hate|i prefer|my favorite)\b", re.I),
    re.compile(r"\b(i want|i'm aiming for|i am aiming for|my goal is|i'm trying to|i am trying to)\b", re.I),
    re.compile(r"\b(applying to|interviewing for|recruiting for|trying to get|want to get)\b", re.I),
    re.compile(r"\b(i work at|i study|i'm studying|i am studying|my job is|my major is)\b", re.I),
    re.compile(r"\b(i go by|call me|my name is|i'm from|i am from)\b", re.I),
    re.compile(r"\b(i use|i mainly use|i usually use|i always use)\b", re.I),
    re.compile(r"\bwe (always|usually|tend to|play|watch|meet)\b", re.I),
    re.compile(r"\b(i am|i'm|i’ve been|i have been) working on\b", re.I),
    re.compile(
        r"\b(?:i(?:'m| am)?|we(?:'re| are)?|my|our)\b.{0,50}"
        r"\b(?:project|deadline|launch|shipping|building|working on)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:i|we|my|our)\b.{0,50}"
        r"\b(?:next week|every friday|recurring|monthly|weekly|daily|every week)\b",
        re.I,
    ),
    re.compile(r"\b(?:remember that|from now on|i no longer|i switched|i changed|my .{1,40} is)\b", re.I),
)
GUILD_LORE_SIGNAL_PATTERNS = (
    re.compile(r"\b(?:we|our server|this server|everyone here)\s+(?:always|usually|call|calls|refer|refers|treat|treats|consider|considers)\b", re.I),
    re.compile(r"\b(?:server lore|running joke|inside joke|guild tradition|server tradition)\b", re.I),
)
CUSTOM_EMOJI_TOKEN_PATTERN = re.compile(
    r"(?:<a?:[A-Za-z0-9_]{2,32}:\d+>|:[A-Za-z0-9_]{2,32}:)"
)
EMOJI_MEANING_SIGNAL_PATTERNS = (
    re.compile(
        r"(?:<a?:[A-Za-z0-9_]{2,32}:\d+>|:[A-Za-z0-9_]{2,32}:)"
        r".{0,80}\b(?:means?|represents?|is for|is used for|use it (?:for|when))\b",
        re.I,
    ),
    re.compile(
        r"\b(?:we use|use|using)\b.{0,40}"
        r"(?:<a?:[A-Za-z0-9_]{2,32}:\d+>|:[A-Za-z0-9_]{2,32}:)"
        r".{0,80}\b(?:for|when|to mean)\b",
        re.I,
    ),
)
GUILD_SHARED_CONFIGURATION_PATTERNS = (
    re.compile(
        r"\b(?:server|guild)[ -]?wide\b|\b(?:our|the)\s+(?:shared|default)\s+"
        r"(?:watchlist|list|report|setting|preference)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:for|in)\s+(?:this|the|our)\s+(?:server|guild)\b|"
        r"\b(?:everyone|anyone)\s+(?:here|in (?:this|the) server)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:from now on|going forward|for future)\b.{0,80}"
        r"\b(?:market|stock|sector|earnings)?\s*(?:reports?|updates?|queries|summaries)\b",
        re.I,
    ),
)
SHAREABLE_MARKET_CONFIGURATION_PATTERNS = (
    re.compile(
        r"\b(?:future|default|shared|server|guild|watchlist)\b.{0,100}"
        r"\b(?:market|stock|ticker|report|query|track)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:include|track|add)\b.{0,100}\b(?:future market|market report|watchlist)\b",
        re.I,
    ),
)
EXPLICIT_MEMORY_DIRECTIVE_PATTERNS = (
    re.compile(r"\b(?:remember|keep in mind|from now on|until further notice)\b", re.I),
)
EXPLICIT_WORKING_MEMORY_PATTERNS = (
    re.compile(
        r"\b(?:remember|keep (?:this|that) in mind).{0,60}"
        r"\b(?:temporar(?:y|ily)|for (?:the )?next|for \d+ (?:days?|weeks?)|until)\b",
        re.I,
    ),
    re.compile(r"\buntil further notice\b", re.I),
)
MEMORY_RETRACTION_PATTERNS = (
    re.compile(r"\bforget\b", re.I),
    re.compile(
        r"\b(?:i|we|my|our)\b.{0,60}"
        r"\b(?:no longer|not anymore|isn't true anymore|is not true anymore)\b",
        re.I,
    ),
    re.compile(r"\b(?:i|we)\s+(?:stopped|quit|left|switched from|changed from)\b", re.I),
)
FIRST_PERSON_DECLARATIVE_PATTERN = re.compile(
    r"(?:^|(?<=[.!])\s+)"
    r"i(?:'m| am| have|\s+[a-z][a-z'-]*)\b"
    r"[^?]*(?:[.!]|$)",
    re.I,
)


def contains_sensitive_pattern(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in SENSITIVE_PATTERNS)


def contains_transient_memory_pattern(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in TRANSIENT_MEMORY_PATTERNS)


def looks_like_low_value_chatter(text: str) -> bool:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return True
    if len(cleaned) < 5:
        return True
    if any(pattern.match(cleaned) for pattern in LOW_VALUE_PATTERNS):
        return True
    return False


def has_useful_memory_signal(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in USEFUL_SIGNAL_PATTERNS)


def has_first_person_declarative_signal(text: str) -> bool:
    """Recognize broad first-person statements without encoding fact topics."""

    cleaned = " ".join(text.strip().split())
    return bool(cleaned) and FIRST_PERSON_DECLARATIVE_PATTERN.search(cleaned) is not None


def has_durable_memory_signal(text: str) -> bool:
    """Return whether a message merits the bounded memory-classification call.

    This is intentionally a high-precision cost gate. The model still decides
    whether a signaled message is safe and durable enough to store.
    """

    cleaned = text.strip()
    if not cleaned or contains_sensitive_pattern(cleaned) or contains_transient_memory_pattern(
        cleaned
    ):
        return False
    return any(
        (
            has_useful_memory_signal(cleaned),
            has_first_person_declarative_signal(cleaned),
            has_guild_lore_signal(cleaned),
            has_emoji_meaning_signal(cleaned),
            has_guild_shared_configuration_signal(cleaned),
            has_explicit_memory_directive(cleaned),
            has_memory_retraction_signal(cleaned),
        )
    )


def has_guild_lore_signal(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned or contains_sensitive_pattern(cleaned):
        return False
    return any(pattern.search(cleaned) for pattern in GUILD_LORE_SIGNAL_PATTERNS) or (
        has_emoji_meaning_signal(cleaned)
    )


def has_emoji_meaning_signal(text: str) -> bool:
    """Recognize explicit explanations of a custom emoji's server meaning."""

    cleaned = " ".join(text.strip().split())
    if not cleaned or contains_sensitive_pattern(cleaned):
        return False
    if CUSTOM_EMOJI_TOKEN_PATTERN.search(cleaned) is None:
        return False
    return any(pattern.search(cleaned) for pattern in EMOJI_MEANING_SIGNAL_PATTERNS)


def has_guild_shared_configuration_signal(text: str) -> bool:
    """Recognize an explicit instruction meant to affect future guild behavior."""

    cleaned = text.strip()
    if not cleaned or contains_sensitive_pattern(cleaned):
        return False
    return any(pattern.search(cleaned) for pattern in GUILD_SHARED_CONFIGURATION_PATTERNS)


def is_shareable_market_configuration(
    *,
    summary: str,
    source_excerpt: str,
    tags: list[str] | None,
) -> bool:
    """Conservatively identify legacy market-report defaults that may be guild-visible."""

    combined = " ".join((summary, source_excerpt, " ".join(tags or []))).strip()
    if not combined or contains_sensitive_pattern(combined):
        return False
    has_market_scope = bool(
        re.search(r"\b(?:market|stock|stocks|ticker|tickers|watchlist)\b", combined, re.I)
    )
    return has_market_scope and any(
        pattern.search(combined) for pattern in SHAREABLE_MARKET_CONFIGURATION_PATTERNS
    )


def has_explicit_memory_directive(text: str) -> bool:
    cleaned = text.strip()
    return bool(cleaned) and any(
        pattern.search(cleaned) for pattern in EXPLICIT_MEMORY_DIRECTIVE_PATTERNS
    )


def has_explicit_working_memory_directive(text: str) -> bool:
    cleaned = text.strip()
    return bool(cleaned) and any(
        pattern.search(cleaned) for pattern in EXPLICIT_WORKING_MEMORY_PATTERNS
    )


def has_memory_retraction_signal(text: str) -> bool:
    cleaned = text.strip()
    return bool(cleaned) and any(
        pattern.search(cleaned) for pattern in MEMORY_RETRACTION_PATTERNS
    )


def should_skip_memory_extraction(
    text: str,
    *,
    correction_context: bool = False,
) -> tuple[bool, str]:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return True, "empty"
    if contains_sensitive_pattern(cleaned):
        return True, "sensitive"
    if contains_transient_memory_pattern(cleaned):
        return True, "transient"
    if correction_context:
        return False, "explicit_correction"
    if looks_like_low_value_chatter(cleaned) and not has_durable_memory_signal(cleaned):
        return True, "low_value"
    if not has_durable_memory_signal(cleaned):
        return True, "no_durable_signal"
    return False, "candidate"


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]{3,}", text.lower())
    return [token for token in tokens if token not in STOPWORDS]


def lexical_similarity(query: str, candidate: str, tags: list[str] | None = None) -> float:
    query_tokens = tokenize(query)
    candidate_tokens = tokenize(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0

    query_counts = Counter(query_tokens)
    candidate_counts = Counter(candidate_tokens + tokenize(" ".join(tags or [])))
    common = sum(min(query_counts[token], candidate_counts[token]) for token in query_counts)
    magnitude = math.sqrt(sum(value * value for value in query_counts.values())) * math.sqrt(
        sum(value * value for value in candidate_counts.values())
    )
    if magnitude == 0:
        return 0.0
    return common / magnitude
