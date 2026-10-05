"""Authorized $1 Terra High replay using the existing budgeted transport."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = next(path for path in Path(__file__).resolve().parents if (path / "src/nycti").is_dir())
OUT = Path(__file__).resolve().parent
CASES = (
    "fixture-watchlist-session-quote", "fixture-watchlist-market-correction",
    "fixture-watchlist-list-callback", "fixture-watchlist-list-only", "fixture-watchlist-saying",
    "fixture-market-quote", "fixture-market-explicit-close", "fixture-social-banter",
)


def bounded_module():
    spec = importlib.util.spec_from_file_location("bounded_watchlist_replay", ROOT / "scripts/run_bounded_market_ab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--child")
    args = parser.parse_args()
    bounded = bounded_module()
    if args.child:
        asyncio.run(bounded.run_child(argparse.Namespace(
            env_file=args.env_file, runner=ROOT / "scripts/run_live_benchmarks.py",
            manifest=OUT / "manifest.json", ledger=OUT / "spend.json", child=args.child,
            normal_chat=True, case_ids=list(CASES), output=OUT,
        )))
        return
    if (OUT / "spend.json").exists():
        raise SystemExit("Refusing to overwrite an existing spend ledger")
    shutil.copyfile(ROOT / "benchmarks/live_cases.json", OUT / "manifest.json")
    (OUT / "spend.json").write_text(json.dumps({
        "limit_usd": 1.0, "authorization": "User approved live Terra replay after the $1 budget request",
        "pricing_source": bounded.PRICING_SOURCE, "pricing_checked": "2026-10-04",
        "input_usd_per_million_upper": 2.5, "output_usd_per_million": 12.0,
        "cache_discount": "ignored", "requests": [],
    }, indent=2))
    metadata = {
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "pending_patch": True, "started_at": datetime.now(timezone.utc).isoformat(),
        "model": "gpt-5.6-terra", "reasoning": "high", "tier": "default", "evidence_mode": "internal",
        "sdk_retries": 0, "fallbacks": "disabled", "repeats": 3, "cases": CASES,
        "execution": "Isolated local Nycti pipeline, synthetic Discord context, frozen market tools, temporary SQLite",
        "source_hashes": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            "src/nycti/chat/context.py", "src/nycti/chat/orchestrator_support.py",
            "src/nycti/live_benchmark_fixture_tools.py", "benchmarks/live_cases.json")},
        "batches": [],
    }
    try:
        for sample in range(1, 4):
            label = f"sample-{sample}"
            print(f"Starting {label}", flush=True)
            with (OUT / f"{label}.log").open("w") as log:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                    "--env-file", str(args.env_file), "--child", label], cwd=ROOT,
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False)
            metadata["batches"].append({"label": label, "exit": result.returncode})
            (OUT / "experiment.json").write_text(json.dumps(metadata, indent=2))
            print(f"Finished {label}: exit={result.returncode}", flush=True)
            if result.returncode:
                raise SystemExit(result.returncode)
        metadata["completed_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        (OUT / "experiment.json").write_text(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
