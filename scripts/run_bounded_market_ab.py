"""Bounded, sequential Terra market-fixture comparison; no production access."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time

CASES = (
    "fixture-market-overnight-session", "fixture-market-explicit-close",
    "fixture-active-watchlist", "fixture-overnight-watchlist",
)
# Standard short-context rates, fetched 2026-09-22. Input includes the cache-write
# premium, with no cache discount. Unknown/failed requests retain their full reserve.
INPUT_USD_PER_M = 2.5
OUTPUT_USD_PER_M = 12.0
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"


class BudgetStop(BaseException):
    """Never convert a budget stop into a model retry or answer-repair call."""


class SpendLedger:
    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label

    def update(self, mutate):
        with self.path.open("r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            data = json.load(stream)
            value = mutate(data)
            stream.seek(0)
            json.dump(data, stream, indent=2)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            return value

    def reserve(self, request: dict) -> int:
        raw = json.dumps(request, ensure_ascii=False)
        if request.get("model") != "gpt-5.6-terra" or request.get("service_tier") != "default":
            raise BudgetStop("Only priced standard-tier Terra requests are permitted")
        if request.get("previous_response_id") or "input_image" in raw or "data:image" in raw:
            raise BudgetStop("Only fully visible text input can be bounded")
        if any(tool.get("type") != "function" for tool in request.get("tools", [])):
            raise BudgetStop("Provider-hosted tools are not budgeted")
        output_cap = request.get("max_output_tokens", request.get("max_completion_tokens", request.get("max_tokens")))
        if not isinstance(output_cap, int) or not 0 < output_cap <= 8192:
            raise BudgetStop("Missing or excessive output cap")
        # UTF-8 bytes upper-bound ordinary text tokens; add generous protocol/schema
        # framing headroom. Reject large requests before long-context prices apply.
        input_bound = len(raw.encode("utf-8")) + 16384
        if input_bound > 100_000:
            raise BudgetStop("Request exceeds conservative short-context bound")
        reserve = (input_bound * INPUT_USD_PER_M + output_cap * OUTPUT_USD_PER_M) / 1_000_000

        def mutate(data):
            charged = sum(row["charged_usd"] for row in data["requests"])
            if charged + reserve > data["limit_usd"]:
                raise BudgetStop("Insufficient budget for the next request's worst case")
            index = len(data["requests"])
            data["requests"].append({
                "index": index, "label": self.label, "model": request["model"],
                "input_token_bound": input_bound, "output_cap": output_cap,
                "reserved_usd": reserve, "charged_usd": reserve, "status": "in_flight",
            })
            return index

        return self.update(mutate)

    def finish(self, index: int, response: object | None, error: str, elapsed_ms: int) -> None:
        def mutate(data):
            row = data["requests"][index]
            row.update(status=error or "ok", elapsed_ms=elapsed_ms)
            usage = getattr(response, "usage", None)
            inp = getattr(usage, "input_tokens", getattr(usage, "prompt_tokens", None))
            out = getattr(usage, "output_tokens", getattr(usage, "completion_tokens", None))
            if isinstance(inp, int) and isinstance(out, int) and inp >= 0 and out >= 0:
                if inp > row["input_token_bound"] or out > row["output_cap"]:
                    raise BudgetStop("Usage exceeded its preflight bound; stop and audit")
                row.update(input_tokens=inp, output_tokens=out,
                           charged_usd=(inp * INPUT_USD_PER_M + out * OUTPUT_USD_PER_M) / 1_000_000)

        self.update(mutate)


def install_budget_transport(ledger: SpendLedger) -> None:
    from nycti.llm.transport import OpenAISDKTransport

    original = OpenAISDKTransport._run

    # Patch the public transport methods, leaving each arm's own timeout behavior intact.
    for name in ("create_response", "create_chat_completion"):
        method = getattr(OpenAISDKTransport, name)

        def guarded_method(method):
            async def guarded(self, *, client, request, timeout_seconds, max_retries):
                index = ledger.reserve(request)
                started = time.perf_counter()
                response = None
                error = ""
                try:
                    response = await method(self, client=client, request=request,
                                            timeout_seconds=timeout_seconds, max_retries=0)
                    return response
                except BaseException as exc:
                    error = type(exc).__name__
                    raise
                finally:
                    ledger.finish(index, response, error, round((time.perf_counter() - started) * 1000))
            return guarded

        setattr(OpenAISDKTransport, name, guarded_method(method))
    assert OpenAISDKTransport._run is original


async def run_child(args) -> None:
    from dotenv import dotenv_values
    from nycti.config import Settings
    from nycti.live_benchmarks import load_live_benchmark_manifest

    env = dict(dotenv_values(args.env_file))
    env.update(DATABASE_URL="sqlite+aiosqlite:///:memory:", SQLITE_BACKUP_ENABLED="false")
    settings = Settings.from_env(env)
    if (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1":
        raise BudgetStop("Unexpected primary provider")
    settings = replace(settings, openai_chat_model="gpt-5.6-terra", openai_reasoning_effort="high",
                       openai_service_tier="default", openai_fallback_api_key=None,
                       openai_fallback_base_url=None, openai_fallback_chat_model=None,
                       openai_chat_model_fallbacks=(), openai_quick_model=None, openai_deep_model=None,
                       openai_embedding_model=None, openai_vision_model=None)
    spec = importlib.util.spec_from_file_location("benchmark_runner", args.runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_live_benchmark_manifest = lambda: load_live_benchmark_manifest(args.manifest)
    module._load_settings_with_database = lambda url: replace(settings, database_url=url)
    install_budget_transport(SpendLedger(args.ledger, args.child))
    if args.normal_chat:
        from nycti.chat.orchestrator import ChatOrchestrator
        from nycti.chat.run_state import EvidenceMode

        original_run = ChatOrchestrator.run_chat_with_tools

        async def normal_chat(self, **kwargs):
            kwargs["evidence_mode"] = EvidenceMode.INTERNAL
            return await original_run(self, **kwargs)

        ChatOrchestrator.run_chat_with_tools = normal_chat
    child_args = argparse.Namespace(
        case_ids=args.case_ids or list(CASES), mode="fixtures", repeats=1, model=None,
        reasoning_effort=None, service_tier=None,
        results=args.output / f"{args.child}-results.md",
        traces=args.output / f"{args.child}-traces.md",
        write_baseline=args.output / f"{args.child}-baseline.json", compare_baseline=None,
        latency_tolerance_percent=15.0,
    )
    await module._run(child_args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, help="Reuse this request's existing spend ledger; never reset it.")
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument("--normal-chat", action="store_true", help="Use production's internal evidence mode in both arms.")
    parser.add_argument("--child")
    parser.add_argument("--runner", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    args.ledger = args.ledger or args.output / "spend.json"
    if args.child:
        asyncio.run(run_child(args))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    ledger_path = args.ledger
    if ledger_path.exists():
        data = json.loads(ledger_path.read_text())
        if data.get("limit_usd") != 2.0:
            raise SystemExit("Unexpected existing budget limit")
    else:
        ledger_path.write_text(json.dumps({
        "limit_usd": 2.0, "authorization": "User approved up to $2 for this comparison",
        "pricing_source": PRICING_SOURCE, "pricing_checked_utc": "2026-09-22",
        "input_usd_per_million_upper": INPUT_USD_PER_M, "output_usd_per_million": OUTPUT_USD_PER_M,
        "cache_discount": "ignored", "retries": 0, "fallback": "disabled in both arms",
        "requests": [],
        }, indent=2))
    manifest_snapshot = args.output / "manifest.json"
    if manifest_snapshot.exists():
        raise SystemExit("Refusing to overwrite an existing experiment")
    shutil.copyfile(args.candidate / "benchmarks/live_cases.json", manifest_snapshot)
    (args.output / "experiment.json").write_text(json.dumps({
        "evidence_mode": "internal" if args.normal_chat else "cited",
        "model": "gpt-5.6-terra", "reasoning": "high", "tier": "default",
        "fallback": "disabled", "pairs": 3, "cases": args.case_ids or list(CASES),
        "spend_ledger": os.path.relpath(ledger_path, args.output),
    }, indent=2))
    for pair in range(1, 4):
        for arm in (("baseline", "candidate") if pair % 2 else ("candidate", "baseline")):
            source = getattr(args, arm)
            label = f"pair-{pair}-{arm}"
            command = [
                sys.executable, str(Path(__file__).resolve()),
                "--baseline", str(args.baseline), "--candidate", str(args.candidate),
                "--env-file", str(args.env_file), "--output", str(args.output),
                "--child", label, "--runner", str(args.candidate / "scripts/run_live_benchmarks.py"),
                "--manifest", str(manifest_snapshot), "--ledger", str(ledger_path),
            ]
            for case_id in args.case_ids or ():
                command.extend(("--case-id", case_id))
            if args.normal_chat:
                command.append("--normal-chat")
            env = {**os.environ, "PYTHONPATH": str(source / "src")}
            print(f"Starting {label}", flush=True)
            with (args.output / f"{label}.log").open("w") as log:
                result = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=300, check=False)
            print(f"Finished {label}: exit={result.returncode}", flush=True)
            if result.returncode:
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
