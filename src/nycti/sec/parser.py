from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

from nycti.sec.models import SecCompanyRecord, SecFilingSummary, SecQueryIntent


SEC_QUERY_NOISE_PATTERNS = (
    r"\buse\s+sec\b",
    r"\bwhat(?:'s|\s+is)?\b",
    r"\bshow\s+me\b",
    r"\bgive\s+me\b",
    r"\btell\s+me\b",
    r"\bcan\s+you\b",
    r"\bplease\b",
    r"\bthe\s+latest\b",
    r"\blatest\b",
)
SEC_FORM_HINT_PATTERN = re.compile(r"\b(10-q|10-k|8-k|6-k|20-f|earnings|er)\b", re.IGNORECASE)
SEC_TICKER_PATTERN = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b")


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def parse_company_tickers(payload: object) -> dict[str, SecCompanyRecord]:
    records: dict[str, SecCompanyRecord] = {}
    for entry in _iter_company_ticker_entries(payload):
        ticker = _clean_string(entry.get("ticker"))
        company_name = _clean_string(entry.get("title"))
        cik = _coerce_int(entry.get("cik_str", entry.get("cik")))
        if not ticker or not company_name or cik is None:
            continue
        records[ticker] = SecCompanyRecord(ticker=ticker, company_name=company_name, cik=cik)
    return records


def parse_recent_filings(payload: object, *, cik: int, limit: int) -> list[SecFilingSummary]:
    if not isinstance(payload, Mapping):
        return []
    filings = payload.get("filings")
    if not isinstance(filings, Mapping):
        return []
    recent = filings.get("recent")
    if not isinstance(recent, Mapping):
        return []

    accessions = _as_list(recent.get("accessionNumber"))
    forms = _as_list(recent.get("form"))
    filing_dates = _as_list(recent.get("filingDate"))
    primary_documents = _as_list(recent.get("primaryDocument"))
    report_dates = _as_list(recent.get("reportDate"))
    descriptions = _as_list(recent.get("primaryDocDescription"))

    count = min(
        len(accessions),
        len(forms),
        len(filing_dates),
        len(primary_documents),
        limit,
    )
    summaries: list[SecFilingSummary] = []
    for index in range(count):
        accession = _clean_string(accessions[index])
        form = _clean_string(forms[index])
        filing_date = _clean_string(filing_dates[index])
        primary_document = _clean_string(primary_documents[index])
        if not accession or not form or not filing_date or not primary_document:
            continue
        summaries.append(
            SecFilingSummary(
                form=form,
                filing_date=filing_date,
                accession_number=accession,
                primary_document=primary_document,
                primary_doc_url=build_primary_doc_url(
                    cik=cik,
                    accession_number=accession,
                    primary_document=primary_document,
                ),
                report_date=_clean_string(report_dates[index]) if index < len(report_dates) else None,
                description=_clean_string(descriptions[index]) if index < len(descriptions) else None,
            )
        )
    return summaries


def build_primary_doc_url(*, cik: int, accession_number: str, primary_document: str) -> str:
    accession_path = accession_number.replace("-", "")
    document_path = quote(primary_document, safe="")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_path}/{document_path}"


def parse_sec_query_intent(text: str) -> SecQueryIntent:
    raw_query = " ".join(text.split())
    lowered = raw_query.lower()
    filing_hint_match = SEC_FORM_HINT_PATTERN.search(lowered)
    filing_hint = filing_hint_match.group(1).upper() if filing_hint_match else None
    if filing_hint == "ER":
        filing_hint = "EARNINGS"

    cleaned_query = raw_query
    for pattern in SEC_QUERY_NOISE_PATTERNS:
        cleaned_query = re.sub(pattern, " ", cleaned_query, flags=re.IGNORECASE)
    if filing_hint_match is not None:
        cleaned_query = re.sub(SEC_FORM_HINT_PATTERN, " ", cleaned_query)
    cleaned_query = re.sub(r"[^\w.\- ]+", " ", cleaned_query)
    cleaned_query = " ".join(cleaned_query.split())

    explicit_ticker = None
    for match in SEC_TICKER_PATTERN.finditer(raw_query):
        candidate = normalize_ticker(match.group(1))
        if len(candidate) == 1 and "." not in candidate:
            continue
        explicit_ticker = candidate
        break

    if explicit_ticker and explicit_ticker == filing_hint:
        filing_hint = None

    return SecQueryIntent(
        raw_query=raw_query,
        cleaned_query=cleaned_query,
        explicit_ticker=explicit_ticker,
        filing_hint=filing_hint,
    )


def _iter_company_ticker_entries(payload: object) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        if isinstance(payload.get("data"), list):
            for entry in payload["data"]:
                if isinstance(entry, Mapping):
                    yield entry
            return
        for value in payload.values():
            if isinstance(value, Mapping):
                yield value
        return
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, Mapping):
                yield entry


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def _clean_string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_int(value: object) -> int | None:
    cleaned = _clean_string(value)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None
