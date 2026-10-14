"""Frozen providers around the production market-report handler and renderer."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import re

from nycti.chat.run_state import ToolExecutionResult, ToolStatus
from nycti.chat.tools.stock_workflows import StockWorkflowMixin, parse_market_report
from nycti.live_benchmark_fixture_tools import _MARKET_QUOTES
from nycti.yahoo.models import YahooMarketSnapshot


class _Providers:
    def __init__(self, symbols: tuple[str, ...]):
        self.symbols = symbols

    @asynccontextmanager
    async def session(self):
        yield None

    async def get_active_market_watchlist(self, _session, **_kwargs):
        return SimpleNamespace(symbols=self.symbols)

    async def get_market_snapshot(self, symbol):
        _name, price, move = _MARKET_QUOTES[symbol]
        value = float(price.replace("$", "").replace(",", ""))
        percent = float(re.search(r"[+-][\d.]+", move)[0])
        stamp = int(datetime.now(timezone.utc).timestamp())
        return YahooMarketSnapshot(symbol=symbol, currency="USD", regular_price=value / (1 + percent / 100),
            regular_timestamp=stamp - 3600, extended_price=value, extended_timestamp=stamp - 1,
            extended_session="overnight")


async def execute_market_report(arguments: str, *, user_id: int, symbols: tuple[str, ...]):
    payload = parse_market_report(arguments)
    if payload is None:
        return ToolExecutionResult("Invalid market_report arguments", ToolStatus.ERROR)
    providers = _Providers(symbols)
    handler = StockWorkflowMixin()
    handler.database = providers
    handler.memory_service = providers
    handler.market_data_client = None
    handler.yahoo_finance_client = providers
    return await handler._handle_market_report(payload, SimpleNamespace(user_id=user_id, guild_id=None))
