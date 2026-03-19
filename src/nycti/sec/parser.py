from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

from nycti.sec.models import SecCompanyRecord, SecFilingSummary


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
