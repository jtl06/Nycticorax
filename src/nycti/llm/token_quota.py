from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nycti.db.models import DailyModelTokenCounter, ModelTokenReservation

logger = logging.getLogger(__name__)

_ACTIVE = "active"
_SETTLED = "settled"
_RELEASED = "released"
_UNCERTAIN = "uncertain"
_PROVIDER_EXHAUSTED = "provider_exhausted"


@dataclass(frozen=True, slots=True)
class TokenQuotaRoute:
    """The model choice and optional durable reservation for one inference."""

    requested_model: str
    model: str
    provider: str
    reasoning_effort: str | None = None
    usage_day: date | None = None
    reservation_id: str | None = None
    reserved_tokens: int = 0
    fallback_reason: str | None = None

    @property
    def used_fallback(self) -> bool:
        return self.fallback_reason is not None


def estimate_reservation_tokens(
    messages: object,
    tools: object,
    max_tokens: int,
) -> int:
    """Return a deliberately high estimate for all request and output tokens.

    UTF-8 byte length is an inexpensive upper-bound approximation for normal
    tokenizer behavior. It also handles non-ASCII prompts more safely than a
    chars-per-token ratio. Reservations are settled to provider-reported usage,
    so this conservatism affects only concurrent capacity.
    """

    output_tokens = max(0, int(max_tokens))
    request_bytes = _serialized_bytes(messages) + _serialized_bytes(tools)
    structural_overhead = 64 + (16 * _collection_size(messages)) + (
        16 * _collection_size(tools)
    )
    return output_tokens + request_bytes + structural_overhead


class DailyTokenQuota:
    """Fail-closed daily token routing backed by durable SQL reservations.

    A configured model gets an independent allowance per UTC calendar day.
    Calls without an allowance pass through untouched. Calls that cannot reserve
    capacity, including calls made while the database is unavailable, route to
    the configured unmetered fallback.
    """

    def __init__(
        self,
        database_or_session_factory: object,
        budgets: Mapping[str, int],
        fallback_model: str,
        fallback_reasoning_effort: str = "high",
        *,
        provider: str = "openai",
        reservation_ttl: timedelta = timedelta(minutes=30),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        session_factory = getattr(
            database_or_session_factory,
            "session_factory",
            database_or_session_factory,
        )
        if not callable(session_factory):
            raise TypeError("database_or_session_factory must provide session_factory")
        if not fallback_model.strip():
            raise ValueError("fallback_model must not be blank")
        if not provider.strip():
            raise ValueError("provider must not be blank")
        if reservation_ttl <= timedelta(0):
            raise ValueError("reservation_ttl must be positive")

        normalized_budgets: dict[str, int] = {}
        for model, limit in budgets.items():
            normalized_model = str(model).strip()
            normalized_limit = int(limit)
            if not normalized_model:
                raise ValueError("budget model must not be blank")
            if normalized_limit < 0:
                raise ValueError("daily token limits must not be negative")
            normalized_budgets[normalized_model] = normalized_limit

        self._session_factory: Callable[[], AsyncSession] = session_factory
        self._budgets = normalized_budgets
        self.fallback_model = fallback_model.strip()
        self.fallback_reasoning_effort = fallback_reasoning_effort.strip() or "high"
        self.provider = provider.strip()
        self._reservation_ttl = reservation_ttl
        self._now = now or (lambda: datetime.now(timezone.utc))

    def is_limited(self, model: str) -> bool:
        return model in self._budgets and model != self.fallback_model

    async def route(
        self,
        model: str,
        messages: object = (),
        tools: object = (),
        max_tokens: int = 0,
    ) -> TokenQuotaRoute:
        """Reserve capacity for a model call or return the safe fallback route."""

        if not self.is_limited(model):
            return self._direct_route(model)

        now = self._utc_now()
        usage_day = now.date()
        reserved_tokens = estimate_reservation_tokens(messages, tools, max_tokens)
        daily_limit = self._budgets[model]
        if reserved_tokens > daily_limit:
            return self._fallback_route(
                model,
                reason="request_exceeds_daily_limit",
                usage_day=usage_day,
            )

        try:
            reservation_id, unavailable_reason = await self._reserve(
                model=model,
                usage_day=usage_day,
                daily_limit=daily_limit,
                reserved_tokens=reserved_tokens,
                now=now,
            )
        except Exception:
            logger.warning(
                "Token quota reservation failed; routing %s to fallback",
                model,
                exc_info=True,
            )
            return self._fallback_route(
                model,
                reason="database_error",
                usage_day=usage_day,
            )

        if reservation_id is None:
            return self._fallback_route(
                model,
                reason=unavailable_reason or "daily_limit_reached",
                usage_day=usage_day,
            )
        return TokenQuotaRoute(
            requested_model=model,
            model=model,
            provider=self.provider,
            usage_day=usage_day,
            reservation_id=reservation_id,
            reserved_tokens=reserved_tokens,
        )

    async def settle(self, route: TokenQuotaRoute, total_tokens: int) -> bool:
        """Replace a reservation with provider-reported total token usage."""

        return await self._finalize(
            route,
            status=_SETTLED,
            charged_tokens=max(0, int(total_tokens)),
        )

    async def release(self, route: TokenQuotaRoute) -> bool:
        """Release a call known to have failed before provider inference began."""

        return await self._finalize(route, status=_RELEASED, charged_tokens=0)

    async def charge_uncertain(self, route: TokenQuotaRoute) -> bool:
        """Charge the full reservation when provider execution is ambiguous."""

        return await self._finalize(
            route,
            status=_UNCERTAIN,
            charged_tokens=route.reserved_tokens,
        )

    async def mark_exhausted(
        self,
        model: str,
        route: TokenQuotaRoute | None = None,
    ) -> bool:
        """Persist a provider-declared daily exhaustion signal.

        When the rejected call has a reservation, it is released in the same
        transaction because a quota rejection is a pre-inference failure.
        """

        if not self.is_limited(model):
            return False
        now = self._utc_now()
        usage_day = (
            route.usage_day
            if route is not None and route.usage_day is not None
            else now.date()
        )
        daily_limit = self._budgets[model]

        for attempt in range(2):
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        counter = await session.get(
                            DailyModelTokenCounter,
                            {
                                "provider": self.provider,
                                "model": model,
                                "usage_day": usage_day,
                            },
                        )
                        if counter is None:
                            counter = DailyModelTokenCounter(
                                provider=self.provider,
                                model=model,
                                usage_day=usage_day,
                                daily_limit=daily_limit,
                                consumed_tokens=0,
                                reserved_tokens=0,
                                provider_exhausted=True,
                                provider_exhausted_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                            session.add(counter)
                            await session.flush()
                        else:
                            counter.daily_limit = daily_limit
                            counter.provider_exhausted = True
                            counter.provider_exhausted_at = now
                            counter.updated_at = now

                        if route is not None and route.reservation_id:
                            await self._release_for_exhaustion(
                                session,
                                route=route,
                                model=model,
                                usage_day=usage_day,
                                now=now,
                            )
                return True
            except IntegrityError:
                if attempt == 0:
                    continue
                logger.warning("Could not persist provider quota exhaustion", exc_info=True)
                return False
            except Exception:
                logger.warning("Could not persist provider quota exhaustion", exc_info=True)
                return False
        return False

    async def _reserve(
        self,
        *,
        model: str,
        usage_day: date,
        daily_limit: int,
        reserved_tokens: int,
        now: datetime,
    ) -> tuple[str | None, str | None]:
        reservation_id = str(uuid4())
        expires_at = now + self._reservation_ttl

        for attempt in range(2):
            try:
                async with self._session_factory() as session:
                    async with session.begin():
                        result = await session.execute(
                            update(DailyModelTokenCounter)
                            .where(
                                DailyModelTokenCounter.provider == self.provider,
                                DailyModelTokenCounter.model == model,
                                DailyModelTokenCounter.usage_day == usage_day,
                                DailyModelTokenCounter.provider_exhausted.is_(False),
                                DailyModelTokenCounter.consumed_tokens
                                + DailyModelTokenCounter.reserved_tokens
                                + reserved_tokens
                                <= daily_limit,
                            )
                            .values(
                                daily_limit=daily_limit,
                                reserved_tokens=DailyModelTokenCounter.reserved_tokens
                                + reserved_tokens,
                                updated_at=now,
                            )
                        )
                        if _rowcount(result) == 1:
                            session.add(
                                self._reservation(
                                    reservation_id=reservation_id,
                                    model=model,
                                    usage_day=usage_day,
                                    reserved_tokens=reserved_tokens,
                                    now=now,
                                    expires_at=expires_at,
                                )
                            )
                            await session.flush()
                            return reservation_id, None

                        counter = await session.get(
                            DailyModelTokenCounter,
                            {
                                "provider": self.provider,
                                "model": model,
                                "usage_day": usage_day,
                            },
                        )
                        if counter is not None:
                            counter.daily_limit = daily_limit
                            counter.updated_at = now
                            reason = (
                                "provider_exhausted"
                                if counter.provider_exhausted
                                else "daily_limit_reached"
                            )
                            return None, reason

                        session.add(
                            DailyModelTokenCounter(
                                provider=self.provider,
                                model=model,
                                usage_day=usage_day,
                                daily_limit=daily_limit,
                                consumed_tokens=0,
                                reserved_tokens=reserved_tokens,
                                provider_exhausted=False,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        session.add(
                            self._reservation(
                                reservation_id=reservation_id,
                                model=model,
                                usage_day=usage_day,
                                reserved_tokens=reserved_tokens,
                                now=now,
                                expires_at=expires_at,
                            )
                        )
                        await session.flush()
                        return reservation_id, None
            except IntegrityError:
                if attempt == 0:
                    continue
                raise
        return None, "database_error"

    async def _finalize(
        self,
        route: TokenQuotaRoute,
        *,
        status: str,
        charged_tokens: int,
    ) -> bool:
        if route.reservation_id is None:
            return True
        now = self._utc_now()
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    reservation = await session.scalar(
                        select(ModelTokenReservation)
                        .where(
                            ModelTokenReservation.reservation_id
                            == route.reservation_id
                        )
                        .with_for_update()
                    )
                    if reservation is None:
                        return False
                    if reservation.status != _ACTIVE:
                        return True

                    claim = await session.execute(
                        update(ModelTokenReservation)
                        .where(
                            ModelTokenReservation.reservation_id
                            == route.reservation_id,
                            ModelTokenReservation.status == _ACTIVE,
                        )
                        .values(
                            status=status,
                            actual_tokens=charged_tokens,
                            finalized_at=now,
                        )
                    )
                    if _rowcount(claim) != 1:
                        return True

                    counter_update = await session.execute(
                        update(DailyModelTokenCounter)
                        .where(
                            DailyModelTokenCounter.provider == reservation.provider,
                            DailyModelTokenCounter.model == reservation.model,
                            DailyModelTokenCounter.usage_day
                            == reservation.usage_day,
                        )
                        .values(
                            reserved_tokens=case(
                                (
                                    DailyModelTokenCounter.reserved_tokens
                                    >= reservation.reserved_tokens,
                                    DailyModelTokenCounter.reserved_tokens
                                    - reservation.reserved_tokens,
                                ),
                                else_=0,
                            ),
                            consumed_tokens=DailyModelTokenCounter.consumed_tokens
                            + charged_tokens,
                            updated_at=now,
                        )
                    )
                    if _rowcount(counter_update) != 1:
                        raise RuntimeError("quota reservation has no daily counter")
            return True
        except Exception:
            logger.warning("Could not finalize token quota reservation", exc_info=True)
            return False

    async def _release_for_exhaustion(
        self,
        session: AsyncSession,
        *,
        route: TokenQuotaRoute,
        model: str,
        usage_day: date,
        now: datetime,
    ) -> None:
        reservation = await session.scalar(
            select(ModelTokenReservation)
            .where(
                ModelTokenReservation.reservation_id == route.reservation_id,
                ModelTokenReservation.provider == self.provider,
                ModelTokenReservation.model == model,
                ModelTokenReservation.usage_day == usage_day,
            )
            .with_for_update()
        )
        if reservation is None or reservation.status != _ACTIVE:
            return
        claim = await session.execute(
            update(ModelTokenReservation)
            .where(
                ModelTokenReservation.reservation_id == route.reservation_id,
                ModelTokenReservation.status == _ACTIVE,
            )
            .values(
                status=_PROVIDER_EXHAUSTED,
                actual_tokens=0,
                finalized_at=now,
            )
        )
        if _rowcount(claim) != 1:
            return
        await session.execute(
            update(DailyModelTokenCounter)
            .where(
                DailyModelTokenCounter.provider == self.provider,
                DailyModelTokenCounter.model == model,
                DailyModelTokenCounter.usage_day == usage_day,
            )
            .values(
                reserved_tokens=case(
                    (
                        DailyModelTokenCounter.reserved_tokens
                        >= reservation.reserved_tokens,
                        DailyModelTokenCounter.reserved_tokens
                        - reservation.reserved_tokens,
                    ),
                    else_=0,
                ),
                updated_at=now,
            )
        )

    def _reservation(
        self,
        *,
        reservation_id: str,
        model: str,
        usage_day: date,
        reserved_tokens: int,
        now: datetime,
        expires_at: datetime,
    ) -> ModelTokenReservation:
        return ModelTokenReservation(
            reservation_id=reservation_id,
            provider=self.provider,
            model=model,
            usage_day=usage_day,
            reserved_tokens=reserved_tokens,
            actual_tokens=None,
            status=_ACTIVE,
            created_at=now,
            expires_at=expires_at,
            finalized_at=None,
        )

    def _direct_route(self, model: str) -> TokenQuotaRoute:
        return TokenQuotaRoute(
            requested_model=model,
            model=model,
            provider=self.provider,
        )

    def _fallback_route(
        self,
        requested_model: str,
        *,
        reason: str,
        usage_day: date,
    ) -> TokenQuotaRoute:
        return TokenQuotaRoute(
            requested_model=requested_model,
            model=self.fallback_model,
            provider=self.provider,
            reasoning_effort=self.fallback_reasoning_effort,
            usage_day=usage_day,
            fallback_reason=reason,
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _serialized_bytes(value: object) -> int:
    if value is None or value == () or value == [] or value == {}:
        return 0
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError, OverflowError):
        serialized = repr(value)
    return len(serialized.encode("utf-8", errors="replace"))


def _collection_size(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    return 1


def _json_default(value: Any) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", -1))
