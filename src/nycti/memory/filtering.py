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
SENSITIVE_PATTERNS = (
    re.compile(r"\b(password|passcode|api[\s_-]?key|secret|token|private key|seed phrase)\b", re.I),
    re.compile(r"\bssn\b|\bsocial security\b", re.I),
    re.compile(r"\bcredit card\b|\bdebit card\b|\bcvv\b", re.I),
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
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
    re.compile(r"\b(project|deadline|launch|shipping|building|working on)\b", re.I),
    re.compile(r"\b(next week|every friday|recurring|monthly|weekly|daily|every week)\b", re.I),
)


def contains_sensitive_pattern(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in SENSITIVE_PATTERNS)


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


def should_skip_memory_extraction(text: str) -> tuple[bool, str]:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return True, "empty"
    if contains_sensitive_pattern(cleaned):
        return True, "sensitive"
    if looks_like_low_value_chatter(cleaned) and not has_useful_memory_signal(cleaned):
        return True, "low_value"
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
