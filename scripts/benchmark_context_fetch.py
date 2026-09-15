"""Read-only paired Discord context benchmark; stdout contains private raw context.

No gateway connection, Discord posts, database writes, or model calls are made.
Redirect stdout to a private artifact; never commit that artifact.
"""
import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

import discord
import nycti


def load_collector(root, name):
    source_spec = importlib.util.spec_from_file_location(name + "_source", root / "message_context_source.py")
    source = importlib.util.module_from_spec(source_spec)
    source_spec.loader.exec_module(source)
    spec = importlib.util.spec_from_file_location(name + "_context", root / "message_context.py")
    context = importlib.util.module_from_spec(spec)
    original = sys.modules.get("nycti.message_context_source")
    sys.modules["nycti.message_context_source"] = source
    try:
        spec.loader.exec_module(context)
    finally:
        if original is None:
            sys.modules.pop("nycti.message_context_source", None)
        else:
            sys.modules["nycti.message_context_source"] = original
    return context.MessageContextCollector


async def benchmark(args, report):
    baseline = load_collector(args.baseline, "baseline")
    candidate = load_collector(args.candidate, "candidate")
    async with discord.Client(intents=discord.Intents.none()) as client:
        # REST login initializes HTTP only; never call start/connect (the gateway).
        await client.login(os.environ["DISCORD_TOKEN"])
        channel = await client.fetch_channel(args.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Benchmark requires the explicitly selected text channel")
        messages = [message async for message in channel.history(limit=60)]
        current = next((message for message in messages if not message.author.bot and message.reference), None)
        if current is None:
            raise ValueError("No recent human reply found in the test channel")
        report["source_message_id"] = current.id
        report["source_created_at"] = current.created_at.isoformat()
        if args.cases == "older_anchor":
            older = next((item for item in messages[20:] if item.id < current.id and not item.reference), None)
            if older is None:
                raise ValueError("No bounded older anchor fixture available")
            current = copy.copy(current)
            current.reference = discord.MessageReference(message_id=older.id, channel_id=channel.id)
            current.reference.resolved = older
            report["fixture"] = "Synthetic reply to an older real test-channel message; no Discord post"
            report["anchor_message_id"] = older.id
        active_spans = []
        original_request = client.http.request

        async def measured_request(route, **kwargs):
            started = time.perf_counter()
            span = {"method": route.method, "path": route.path, "params": kwargs.get("params")}
            active_spans.append(span)
            try:
                result = await original_request(route, **kwargs)
                if isinstance(result, list):
                    span["returned_ids"] = [row.get("id") for row in result if isinstance(row, dict)]
                return result
            except Exception as error:
                span["error_type"] = type(error).__name__
                raise
            finally:
                span["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)

        client.http.request = measured_request

        async def allowed_channel(channel_id):
            if channel_id != channel.id:
                raise ValueError("Benchmark refuses reads outside the selected channel")
            return channel

        cases = ("older_anchor",) if args.cases == "older_anchor" else ("cold", "warm")
        for cache_mode in cases:
            for pair in range(args.repeats):
                variants = (("baseline", baseline), ("candidate", candidate))
                if pair % 2:
                    variants = tuple(reversed(variants))
                for name, collector_type in variants:
                    cached = list(messages) if cache_mode == "warm" else []
                    bot = SimpleNamespace(cached_messages=cached,
                        get_message=lambda mid: next((m for m in cached if m.id == mid), None),
                        get_channel=lambda cid: channel if cid == channel.id else None,
                        fetch_channel=allowed_channel)
                    collector = collector_type(bot=bot, channel_context_limit=12,
                        max_reply_chain_depth=3, max_linked_message_count=3,
                        max_context_image_count=3, anchor_context_per_side=1)
                    active_spans = []
                    record = {"cache": cache_mode, "pair": pair, "variant": name,
                              "http_spans": active_spans, "phase_ms": {}}
                    report["runs"].append(record)
                    started = time.perf_counter()
                    try:
                        lines, images, image_lines, _ = await collector.build_message_context_with_members(
                            current, timing_metrics=record["phase_ms"])
                        record["context_lines"] = lines
                        record["image_urls"] = images
                        record["image_context_lines"] = image_lines
                        canonical = json.dumps([lines, images, image_lines], sort_keys=True).encode()
                        record["context_sha256"] = hashlib.sha256(canonical).hexdigest()
                    finally:
                        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                        record["http_requests"] = len(active_spans)
                    await asyncio.sleep(1)
        report["summary"] = []
        for cache_mode in cases:
            selected = [run for run in report["runs"] if run["cache"] == cache_mode]
            identical = all(
                len({run["context_sha256"] for run in selected if run["pair"] == pair}) == 1
                for pair in range(args.repeats))
            report["summary"].append({"cache": cache_mode, "identical_context_all_pairs": identical,
                **{name: {"median_ms": statistics.median(run["elapsed_ms"] for run in selected if run["variant"] == name),
                          "requests": [run["http_requests"] for run in selected if run["variant"] == name]}
                   for name in ("baseline", "candidate")}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", type=int, required=True)
    parser.add_argument("--baseline", type=Path, default=Path(nycti.__file__).parent)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--cases", choices=("recent", "older_anchor"), default="recent")
    args = parser.parse_args()
    if args.repeats != 3:
        parser.error("Use three bounded paired samples per cache mode")
    report = {"channel_id": args.channel_id, "runs": [], "model_calls": 0, "posts": 0}
    report["source_sha256"] = {
        name: {file: hashlib.sha256((root / file).read_bytes()).hexdigest()
               for file in ("message_context_source.py", "message_context.py")}
        for name, root in (("baseline", args.baseline), ("candidate", args.candidate))}
    try:
        asyncio.run(asyncio.wait_for(benchmark(args, report), timeout=180))
    except Exception as error:
        report["error_type"] = type(error).__name__
    finally:
        print(json.dumps(report, indent=2))
    if "error_type" in report or not all(row["identical_context_all_pairs"] for row in report["summary"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
