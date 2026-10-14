"""Carry actual answers and public receipts between isolated benchmark turns."""
from dataclasses import replace


def case_with_scenario_history(case, history):
    from nycti.live_benchmarks import LiveBenchmarkDiscordMessage, MAX_LIVE_BENCHMARK_DISCORD_MESSAGES
    if not case.scenario_id or case.scenario_turn <= 1 or not history:
        return case
    messages = [(m.author, m.content, m.report_receipts_json) for m in case.discord.recent_messages]
    messages.extend((item[0], item[1], item[2] if len(item) > 2 else "") for item in history)
    messages = messages[-MAX_LIVE_BENCHMARK_DISCORD_MESSAGES:]
    recent = tuple(LiveBenchmarkDiscordMessage(
        author=author, content=content[:4000], minutes_ago=len(messages) - index,
        report_receipts_json=receipt,
    ) for index, (author, content, receipt) in enumerate(messages))
    return replace(case, discord=replace(case.discord, recent_messages=recent))
