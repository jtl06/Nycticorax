from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nycti.chat.run_state import ToolOutcome, ToolStatus
from nycti.chat.tools.schemas import (
    CREATE_REMINDER_TOOL_NAME,
    SEND_CHANNEL_MESSAGE_TOOL_NAME,
)

DEFAULT_MAX_EVIDENCE_ITEMS = 12
DEFAULT_MAX_EXCERPT_CHARS = 360
DEFAULT_MAX_GUIDANCE_CHARS = 6000
NON_EVIDENCE_TOOL_NAMES = frozenset(
    {CREATE_REMINDER_TOOL_NAME, SEND_CHANNEL_MESSAGE_TOOL_NAME}
)

_URL_RE = re.compile(r"https?://[^\s<>\])]+", re.IGNORECASE)
_EVIDENCE_CITATION_RE = re.compile(r"\[(E-[A-Z0-9]{1,64})\]", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?\"'"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    tool_name: str
    source: str
    excerpt: str

    @property
    def is_external(self) -> bool:
        return self.source.casefold().startswith(("https://", "http://"))


@dataclass(frozen=True, slots=True)
class CitationAudit:
    researched: bool
    cited_ids: tuple[str, ...]
    unknown_citation_ids: tuple[str, ...]
    answer_urls: tuple[str, ...]
    unprovenanced_urls: tuple[str, ...]
    lacks_citations: bool

    @property
    def valid(self) -> bool:
        return not (
            self.unknown_citation_ids
            or self.unprovenanced_urls
            or self.lacks_citations
        )


@dataclass(frozen=True, slots=True)
class EvidenceLedger:
    items: tuple[EvidenceItem, ...]

    @classmethod
    def from_outcomes(
        cls,
        outcomes: Sequence[ToolOutcome],
        *,
        max_items: int = DEFAULT_MAX_EVIDENCE_ITEMS,
        max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
    ) -> EvidenceLedger:
        if max_items < 1:
            raise ValueError("max_items must be at least 1")
        if max_excerpt_chars < 40:
            raise ValueError("max_excerpt_chars must be at least 40")

        items: list[EvidenceItem] = []
        seen_sources: set[str] = set()
        for outcome in outcomes:
            if (
                outcome.tool_name in NON_EVIDENCE_TOOL_NAMES
                or outcome.status != ToolStatus.OK
                or not outcome.content.strip()
            ):
                continue
            sources = _outcome_sources(outcome)
            for source in sources:
                source_key = _source_key(source)
                if not source_key or source_key in seen_sources:
                    continue
                seen_sources.add(source_key)
                items.append(
                    EvidenceItem(
                        evidence_id=_stable_evidence_id(source_key),
                        tool_name=outcome.tool_name,
                        source=source,
                        excerpt=_excerpt_for_source(
                            outcome.content,
                            source,
                            max_chars=max_excerpt_chars,
                        ),
                    )
                )
                if len(items) >= max_items:
                    return cls(items=tuple(items))
        return cls(items=tuple(items))

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.items)

    @property
    def provenance_urls(self) -> tuple[str, ...]:
        return tuple(item.source for item in self.items if item.is_external)

    @property
    def researched(self) -> bool:
        return bool(self.provenance_urls)

    def render_model_guidance(
        self,
        *,
        max_chars: int = DEFAULT_MAX_GUIDANCE_CHARS,
    ) -> str:
        if not self.items:
            return "No successful tool evidence is available. Do not invent sources or URLs."
        if max_chars < 200:
            raise ValueError("max_chars must be at least 200")

        lines = [
            "Evidence ledger: cite supporting IDs exactly as `[E-…]` for researched claims.",
            "Use only the URLs listed here; never invent or alter a source URL. If evidence is insufficient or conflicts, say so.",
        ]
        for item in self.items:
            line = (
                f"- [{item.evidence_id}] tool={item.tool_name}; "
                f"source={item.source}; excerpt={item.excerpt}"
            )
            candidate = "\n".join([*lines, line])
            if len(candidate) > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    def render_source_list(
        self,
        citation_ids: Iterable[str] | None = None,
        *,
        include_tool_evidence: bool = False,
    ) -> str:
        selected = {value.upper() for value in citation_ids} if citation_ids is not None else None
        lines: list[str] = []
        for item in self.items:
            if selected is not None and item.evidence_id.upper() not in selected:
                continue
            if item.is_external:
                lines.append(f"- [{item.evidence_id}] [{_source_label(item.source)}]({item.source})")
            elif include_tool_evidence:
                lines.append(f"- [{item.evidence_id}] `{item.tool_name}` tool result")
        if not lines:
            return ""
        return "Sources:\n" + "\n".join(lines)

    def audit_answer(
        self,
        answer: str,
        *,
        researched: bool | None = None,
    ) -> CitationAudit:
        known_ids = {item.evidence_id.upper() for item in self.items}
        mentioned_ids = _dedupe(
            match.group(1).upper() for match in _EVIDENCE_CITATION_RE.finditer(answer)
        )
        cited_ids = tuple(value for value in mentioned_ids if value in known_ids)
        unknown_ids = tuple(value for value in mentioned_ids if value not in known_ids)

        answer_urls = _dedupe(_extract_urls(answer))
        known_url_keys = {_source_key(url) for url in self.provenance_urls}
        unprovenanced_urls = tuple(
            url for url in answer_urls if _source_key(url) not in known_url_keys
        )
        cited_known_url = any(_source_key(url) in known_url_keys for url in answer_urls)
        effective_researched = self.researched if researched is None else researched
        lacks_citations = effective_researched and not (cited_ids or cited_known_url)
        return CitationAudit(
            researched=effective_researched,
            cited_ids=cited_ids,
            unknown_citation_ids=unknown_ids,
            answer_urls=answer_urls,
            unprovenanced_urls=unprovenanced_urls,
            lacks_citations=lacks_citations,
        )


def build_evidence_ledger(
    outcomes: Sequence[ToolOutcome],
    *,
    max_items: int = DEFAULT_MAX_EVIDENCE_ITEMS,
    max_excerpt_chars: int = DEFAULT_MAX_EXCERPT_CHARS,
) -> EvidenceLedger:
    return EvidenceLedger.from_outcomes(
        outcomes,
        max_items=max_items,
        max_excerpt_chars=max_excerpt_chars,
    )


def _outcome_sources(outcome: ToolOutcome) -> tuple[str, ...]:
    sources: list[str] = []
    seen_url_keys: set[str] = set()
    for value in outcome.provenance:
        cleaned = value.strip().rstrip(_TRAILING_URL_PUNCTUATION)
        normalized = _normalize_url(cleaned)
        if normalized is None or normalized in seen_url_keys:
            continue
        seen_url_keys.add(normalized)
        sources.append(cleaned)
    if sources:
        return tuple(sources)
    argument_digest = hashlib.sha256(
        _canonical_arguments(outcome.arguments).encode()
    ).hexdigest()[:10]
    return (f"tool:{outcome.tool_name}:{argument_digest}",)


def _stable_evidence_id(source_key: str) -> str:
    digest = hashlib.sha256(source_key.encode()).hexdigest()[:10].upper()
    return f"E-{digest}"


def _source_key(source: str) -> str:
    return _normalize_url(source) or source.strip().casefold()


def _normalize_url(value: str) -> str | None:
    cleaned = value.strip().rstrip(_TRAILING_URL_PUNCTUATION)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    scheme = parsed.scheme.casefold()
    try:
        hostname = parsed.hostname.casefold()
        port = parsed.port
    except (AttributeError, ValueError):
        return None
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, hostname, path, query, ""))


def _extract_urls(text: str) -> tuple[str, ...]:
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        cleaned = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        if _normalize_url(cleaned) is not None:
            urls.append(cleaned)
    return tuple(urls)


def _excerpt_for_source(content: str, source: str, *, max_chars: int) -> str:
    blocks = [block.strip() for block in content.split("\n\n") if block.strip()]
    matching_block = next(
        (
            block
            for block in blocks
            if source in block
            or any(_source_key(url) == _source_key(source) for url in _extract_urls(block))
        ),
        content,
    )
    compact = " ".join(matching_block.split())
    if len(compact) <= max_chars:
        return compact
    shortened = compact[: max_chars - 3].rsplit(" ", 1)[0].rstrip()
    return f"{shortened or compact[: max_chars - 3].rstrip()}..."


def _source_label(source: str) -> str:
    parsed = urlsplit(source)
    path = parsed.path.rstrip("/")
    label = f"{parsed.hostname or source}{path}"
    if len(label) > 72:
        return f"{label[:69].rstrip()}..."
    return label


def _canonical_arguments(arguments: str) -> str:
    cleaned = arguments.strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    deduplicated: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduplicated.append(value)
    return tuple(deduplicated)
