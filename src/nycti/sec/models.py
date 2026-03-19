from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecCompanyRecord:
    ticker: str
    company_name: str
    cik: int


@dataclass(frozen=True, slots=True)
class SecFilingSummary:
    form: str
    filing_date: str
    accession_number: str
    primary_document: str
    primary_doc_url: str
    report_date: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class SecLatestFilings:
    ticker: str
    company_name: str
    cik: int
    filings: list[SecFilingSummary]


class SecError(Exception):
    pass


class SecUserAgentMissingError(SecError):
    pass


class SecTickerNotFoundError(SecError):
    pass


class SecNoFilingsError(SecError):
    pass


class SecHTTPError(SecError):
    pass


class SecDataError(SecError):
    pass
