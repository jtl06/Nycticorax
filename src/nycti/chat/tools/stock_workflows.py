from __future__ import annotations

from dataclasses import dataclass
import json
import re

from sqlalchemy import select

from nycti.chat.run_state import ToolExecutionResult, ToolStatus
from nycti.db.models import UserSettings
from nycti.market_reports import build_market_report

MAX_WATCHLIST_SYMBOLS = 40
SYMBOL_RE = re.compile(r"[A-Z^][A-Z0-9.^=/:-]{0,19}")


def normalized_symbols(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_WATCHLIST_SYMBOLS:
        return None
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        symbol = item.strip().removeprefix("$").upper()
        if not SYMBOL_RE.fullmatch(symbol):
            return None
        if symbol not in result:
            result.append(symbol)
    return tuple(result)


def _payload(arguments: str, fields: set[str]) -> dict | None:
    try:
        value = json.loads(arguments)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) and not value.keys() - fields else None


@dataclass(frozen=True)
class WatchlistArguments:
    action: str
    symbols: tuple[str, ...]
    finish: bool = False


def parse_watchlist(arguments: str) -> WatchlistArguments | None:
    value = _payload(arguments, {"action", "symbols", "finish"})
    if value is None or not isinstance(value.get("action"), str) or value["action"] not in {"get", "add", "remove", "replace", "reset"}:
        return None
    raw_symbols = value.get("symbols")
    symbols = normalized_symbols([] if raw_symbols is None else raw_symbols)
    finish = value.get("finish", False)
    if symbols is None or not isinstance(finish, bool):
        return None
    action = value["action"]
    if action == "replace" and raw_symbols is None:
        return None
    if (action in {"get", "reset"} and symbols) or (action in {"add", "remove"} and not symbols):
        return None
    return WatchlistArguments(action, symbols, finish)


@dataclass(frozen=True)
class MarketReportArguments:
    symbols: tuple[str, ...]
    session: str
    finish: bool = False


def parse_market_report(arguments: str) -> MarketReportArguments | None:
    value = _payload(arguments, {"symbols", "session", "finish"})
    if value is None:
        return None
    raw_symbols = value.get("symbols")
    symbols = normalized_symbols([] if raw_symbols is None else raw_symbols)
    session = value.get("session") or "latest"
    finish = value.get("finish", False)
    if symbols is None or not isinstance(finish, bool) or not isinstance(session, str) or session not in {"latest", "regular", "pre", "post", "overnight"}:
        return None
    return MarketReportArguments(symbols, session, finish)


class StockWorkflowMixin:
    async def _handle_watchlist(self, payload: WatchlistArguments, context) -> ToolExecutionResult:
        # Owner identity comes only from the authenticated request, never tool arguments.
        async with self.database.session() as session:
            settings = await session.scalar(
                select(UserSettings).where(UserSettings.user_id == context.user_id).with_for_update()
            )
            if settings is None or not settings.memory_enabled:
                return ToolExecutionResult(
                    "Watchlist storage is disabled. Enable memory with /memory enable:true first.",
                    ToolStatus.ERROR,
                )
            current = await self.memory_service.get_active_market_watchlist(
                session, user_id=context.user_id, guild_id=context.guild_id, memory_enabled=True,
            )
            symbols = current.symbols
            if payload.action == "add":
                symbols = tuple(dict.fromkeys((*symbols, *payload.symbols)))
            elif payload.action == "remove":
                symbols = tuple(symbol for symbol in symbols if symbol not in payload.symbols)
            elif payload.action == "replace":
                symbols = payload.symbols
            if len(symbols) > MAX_WATCHLIST_SYMBOLS:
                return ToolExecutionResult(
                    f"Watchlist unchanged: at most {MAX_WATCHLIST_SYMBOLS} symbols are supported.", ToolStatus.ERROR,
                )
            if payload.action != "get":
                settings.market_watchlist = None if payload.action == "reset" else list(symbols)
                await session.flush()
                confirmed = await self.memory_service.get_active_market_watchlist(
                    session, user_id=context.user_id, guild_id=context.guild_id, memory_enabled=True,
                )
                symbols = confirmed.symbols
                await session.commit()
        verb = "Your watchlist" if payload.action == "get" else "Saved your watchlist"
        reply = f"{verb}: {', '.join(symbols) if symbols else '(empty)'}."
        return ToolExecutionResult(
            reply, ToolStatus.OK,
            metrics={"watchlist_symbol_count": len(symbols), "watchlist_action": payload.action},
            direct_reply=reply,
            terminal=payload.finish,
        )

    async def _handle_market_report(self, payload: MarketReportArguments, context) -> ToolExecutionResult:
        symbols = payload.symbols
        if not symbols:
            async with self.database.session() as session:
                watchlist = await self.memory_service.get_active_market_watchlist(
                    session, user_id=context.user_id, guild_id=context.guild_id,
                )
                symbols = watchlist.symbols
        if not symbols:
            reply = "Your watchlist is empty or disabled. Which symbols should I quote?"
            return ToolExecutionResult(reply, ToolStatus.EMPTY, direct_reply=reply, terminal=payload.finish)
        report = await build_market_report(
            symbols, requested_session=payload.session,
            primary=self.market_data_client, yahoo=self.yahoo_finance_client,
        )
        reply = report.render()
        return ToolExecutionResult(
            reply, ToolStatus.OK if report.success_count else ToolStatus.ERROR,
            metrics={
                "market_report_symbol_count": len(symbols),
                "market_report_success_count": report.success_count,
                "market_report_unavailable_count": len(symbols) - report.success_count,
                "stock_quote_symbols": ", ".join(symbols),
                "stock_quote_success_symbol_count": report.success_count,
            },
            direct_reply=reply,
            terminal=payload.finish,
        )
