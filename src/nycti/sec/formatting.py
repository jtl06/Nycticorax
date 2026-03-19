from __future__ import annotations

from nycti.sec.models import SecLatestFilings


def format_latest_filings_message(result: SecLatestFilings) -> str:
    lines = [f"Latest SEC filings for {result.company_name} ({result.ticker})"]
    for filing in result.filings:
        parts = [
            filing.form,
            f"filed {filing.filing_date}",
            f"accession {filing.accession_number}",
            f"primary doc: {filing.primary_doc_url}",
        ]
        if filing.report_date:
            parts.insert(2, f"report date {filing.report_date}")
        if filing.form in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
            parts.append("earnings-related form")
        lines.append("- " + " | ".join(parts))
    if len(lines) == 1:
        lines.append("No recent filings found.")
    return "\n".join(lines)
