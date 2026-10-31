"""Frozen providers around the production market-report handler and renderer."""
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import datetime, timezone
from types import SimpleNamespace
import re
import math

from nycti.chat.run_state import ToolExecutionResult, ToolStatus
from nycti.chat.tools.stock_workflows import StockWorkflowMixin, parse_market_report
from nycti.live_benchmark_fixture_tools import _MARKET_QUOTES
from nycti.yahoo.models import YahooMarketSnapshot


class _Providers:
    def __init__(self, symbols: tuple[str, ...], snapshots=()):
        self.symbols = symbols
        self.snapshots = {snapshot.symbol: snapshot for snapshot in snapshots}

    @asynccontextmanager
    async def session(self):
        yield None

    async def get_active_market_watchlist(self, _session, **_kwargs):
        return SimpleNamespace(symbols=self.symbols)

    async def get_market_snapshot(self, symbol):
        if symbol in self.snapshots:
            snapshot = self.snapshots[symbol]
            # The production handler uses wall time; retain frozen values and
            # relative timestamp spacing without making every fixture stale.
            latest = snapshot.extended_timestamp or snapshot.regular_timestamp
            shift = int(datetime.now(timezone.utc).timestamp()) - 1 - latest if latest else 0
            return replace(snapshot,
                regular_timestamp=snapshot.regular_timestamp + shift if snapshot.regular_timestamp else None,
                extended_timestamp=snapshot.extended_timestamp + shift if snapshot.extended_timestamp else None)
        if self.snapshots:
            raise KeyError(symbol)
        _name, price, move = _MARKET_QUOTES[symbol]
        value = float(price.replace("$", "").replace(",", ""))
        percent = float(re.search(r"[+-][\d.]+", move)[0])
        stamp = int(datetime.now(timezone.utc).timestamp())
        return YahooMarketSnapshot(symbol=symbol, currency="USD", regular_price=value / (1 + percent / 100),
            regular_timestamp=stamp - 3600, extended_price=value, extended_timestamp=stamp - 1,
            extended_session="overnight")


def parse_market_snapshot(raw: object) -> YahooMarketSnapshot:
    if not isinstance(raw, dict) or set(raw).difference(field.name for field in fields(YahooMarketSnapshot)):
        raise ValueError("market_snapshot must contain YahooMarketSnapshot fields only")
    if not isinstance(raw.get("symbol"), str) or not raw["symbol"].strip():
        raise ValueError("market_snapshot requires a symbol")
    text_fields = {"symbol", "currency", "exchange_name", "timezone_name", "market_state", "extended_session"}
    for key, value in raw.items():
        if value is None:
            continue
        if key in text_fields:
            if not isinstance(value, str):
                raise ValueError("market_snapshot text fields must be strings")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("market_snapshot numeric fields must be finite numbers")
    return YahooMarketSnapshot(**raw)


async def execute_market_report(arguments: str, *, user_id: int, symbols: tuple[str, ...], snapshots=()):
    payload = parse_market_report(arguments)
    if payload is None:
        return ToolExecutionResult("Invalid market_report arguments", ToolStatus.ERROR)
    providers = _Providers(symbols, snapshots)
    handler = StockWorkflowMixin()
    handler.database = providers
    handler.memory_service = providers
    handler.market_data_client = None
    handler.yahoo_finance_client = providers
    return await handler._handle_market_report(payload, SimpleNamespace(user_id=user_id, guild_id=None))
