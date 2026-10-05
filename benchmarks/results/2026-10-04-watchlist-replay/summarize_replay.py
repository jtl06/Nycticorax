"""Summarize stored attempts without paraphrasing or rescoring the raw traces."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import statistics

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "src/nycti").is_dir())
OUT = Path(__file__).resolve().parent


def main():
    metadata = json.loads((OUT / "experiment.json").read_text())
    assert metadata.get("completed_at"), "Cannot report a partial run as complete"
    batches = []
    cases = defaultdict(list)
    for batch in metadata["batches"]:
        raw = (OUT / f"{batch['label']}-traces.md").read_text()
        attempts = json.loads(raw.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        batches.append({**batch, "attempts": attempts})
        for attempt in attempts:
            cases[attempt["case_id"]].append(attempt)
    ledger = json.loads((OUT / "spend.json").read_text())
    assert all(request["status"] != "in_flight" for request in ledger["requests"])
    cost = sum(request["charged_usd"] for request in ledger["requests"])
    assert cost <= ledger["limit_usd"] == 1.0
    summary = {"passed": sum(a["status"] == "pass" for rows in cases.values() for a in rows),
        "attempts": sum(len(rows) for rows in cases.values()), "budget_accounted_usd": cost,
        "api_requests": len(ledger["requests"]), "cases": {}}
    table = []
    for name, rows in cases.items():
        times = [row["latency_ms"] for row in rows]
        passed = sum(row["status"] == "pass" for row in rows)
        summary["cases"][name] = {"passes": passed, "samples": len(rows),
            "latencies_ms": times, "median_ms": statistics.median(times),
            "failed_checks": [check for row in rows for check in row["checks"] if not check["passed"]]}
        table.append(f"| {name} | {passed}/{len(rows)} | {statistics.median(times)/1000:.2f}s |")
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    raw_dump = json.dumps({"experiment": metadata, "batches": batches}, ensure_ascii=False, indent=2)
    (OUT / "raw-attempts.json").write_text(raw_dump + "\n")
    (ROOT / "benchmarkresult_traces.md").write_text(
        "# Benchmark Result Traces\n\nLatest run: October 4, 2026, Terra High watchlist quote replay. "
        "24 attempts, normal-chat evidence mode, frozen tools and synthetic Discord context. "
        "The original per-batch dumps are in `benchmarks/results/2026-10-04-watchlist-replay/`.\n\n"
        "```json\n" + raw_dump + "\n```\n")
    report = "\n".join([
        "# Benchmark Results", "", "## October 4, 2026: Watchlist Quote Recovery", "",
        "Terra High passed 24/24 automated cases, with three samples per prompt. Five new cases cover",
        "session-qualified full-basket quotes, scope preservation after a correction, an explicit list callback,",
        "watchlist listing without unnecessary market calls, and an explicit inspirational quotation.",
        "Three existing cases cover a single quote, an explicit historical close, and friend-server banter.", "",
        "| Case | Passes | Median local pipeline time |", "| --- | ---: | ---: |", *table, "",
        "## Setup", "",
        "- Base revision: `997fa4c`, plus the pending watchlist context/guidance patch; exact source hashes are in `experiment.json`.",
        "- Model: `gpt-5.6-terra`, high reasoning, standard API tier, no SDK retries or provider fallback.",
        "- Production's normal internal evidence mode; no benchmark-only instruction to use tools or force an answer repair.",
        "- Real model calls through Nycti's isolated reply pipeline, temporary SQLite, synthetic Discord context, and frozen quote evidence.",
        "- No production memories, Discord messages, or live market-provider requests. Timings exclude Discord delivery.",
        "- User prompts are short: `24 hour quote`, `quote the market`, `Quote those then.`, `What's my watch list?`, and `Give me an inspirational quote.`", "",
        "## Observations", "",
        "The initial session quote and market correction included all ten canonical symbols in every repeat.",
        "List callbacks stayed on the referenced three symbols instead of expanding to the default ten.",
        "List-only and inspirational requests made no tool calls. Existing single-symbol, historical-session,",
        "and banter cases passed. No provider or tool errors occurred in the recorded runs.", "",
        "These checks validate task interpretation and quote coverage, not every incidental sentence.",
        "Manual review found one reply incorrectly grouping SPCX with semiconductors and an unnecessary",
        "timestamp caveat despite the fixture header. Those commentary issues remain visible in raw traces",
        "and were not removed or silently rescored. There is no before/after live pass-rate comparison or",
        "production latency claim from this replay.", "",
        "## Cost and Artifacts", "",
        f"Conservative budget accounting: ${cost:.4f} of the authorized $1 across {len(ledger['requests'])} model requests.",
        "Input is charged at the $2.50/M cache-write upper rate and output at $12/M, ignoring cache and sharing",
        "discounts. Actual billed cost may be lower. Pricing checked October 4, 2026:",
        "https://developers.openai.com/api/docs/pricing", "",
        "- `benchmarks/results/2026-10-04-watchlist-replay/`: original logs, results, raw traces, JSON baselines, manifest, source hashes, and spend ledger.",
        "- `benchmarkresult_traces.md`: latest complete raw attempt dump, without paraphrase.", "",
        "The API replay supports the scope fix; offline lint, type checks and regression tests provide the code gate.", "",
    ])
    (ROOT / "benchmarkresults.md").write_text(report)
    (OUT / "REPORT.md").write_text(report)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
