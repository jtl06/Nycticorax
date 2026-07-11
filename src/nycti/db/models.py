from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nycti.memory.visibility import MemoryVisibility


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), default="America/Los_Angeles", nullable=False)
    personal_profile_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    visibility: Mapped[str] = mapped_column(
        String(24),
        default=MemoryVisibility.PRIVATE.value,
        server_default=MemoryVisibility.PRIVATE.value,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    times_retrieved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feature: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="openai", nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DailyModelTokenCounter(Base):
    """Durable UTC-day accounting for a provider/model token allowance."""

    __tablename__ = "daily_model_token_counters"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    usage_day: Mapped[date] = mapped_column(Date, primary_key=True)
    daily_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumed_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    provider_exhausted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    provider_exhausted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class ModelTokenReservation(Base):
    """One durable in-flight reservation against a daily token counter."""

    __tablename__ = "model_token_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "model", "usage_day"],
            [
                "daily_model_token_counters.provider",
                "daily_model_token_counters.model",
                "daily_model_token_counters.usage_day",
            ],
            ondelete="CASCADE",
        ),
        Index(
            "ix_model_token_reservation_active",
            "provider",
            "model",
            "usage_day",
            "status",
        ),
    )

    reservation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    usage_day: Mapped[date] = mapped_column(Date, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ToolCallEvent(Base):
    __tablename__ = "tool_call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AgentStepEvent(Base):
    __tablename__ = "agent_step_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    feature: Mapped[str | None] = mapped_column(String(48), nullable=True)
    requested_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    argument_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    final_status: Mapped[str] = mapped_column(String(24), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    correction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    continuation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class MessageDebugEvent(Base):
    __tablename__ = "message_debug_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    part: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reminder_text: Mapped[str] = mapped_column(Text, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AppState(Base):
    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ResponseDiagnosticSnapshotRecord(Base):
    __tablename__ = "response_diagnostic_snapshots"
    __table_args__ = (
        Index(
            "ix_response_diag_scope_expiry",
            "guild_id",
            "channel_id",
            "expires_at",
            "captured_at",
        ),
    )

    source_message_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_url: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        index=True,
        nullable=False,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context_lines: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    image_context_lines: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    reply_text: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)


class ResponseDiagnosticMessageRecord(Base):
    __tablename__ = "response_diagnostic_messages"

    bot_message_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    source_message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "response_diagnostic_snapshots.source_message_id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )


class BadBotFeedbackRecord(Base):
    """Durable archive for diagnostics explicitly flagged by a server member."""

    __tablename__ = "bad_bot_feedback"
    __table_args__ = (
        Index("ix_bad_bot_feedback_scope_created", "guild_id", "channel_id", "created_at"),
    )

    feedback_message_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=False,
    )
    source_message_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    feedback_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_message_url: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_message_url: Mapped[str] = mapped_column(Text, nullable=False)
    feedback_text: Mapped[str] = mapped_column(Text, nullable=False)
    bundle: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class ChannelAlias(Base):
    __tablename__ = "channel_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    alias: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class MemberAlias(Base):
    __tablename__ = "member_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    alias: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    note: Mapped[str] = mapped_column(String(180), default="", nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LiveBenchmarkAttemptRecord(Base):
    """One isolated attempt from an explicitly requested live-model benchmark."""

    __tablename__ = "live_benchmark_attempts"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "case_id",
            "attempt_index",
            name="uq_live_benchmark_batch_case_attempt",
        ),
        Index(
            "ix_live_benchmark_case_status_created",
            "case_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_live_benchmark_status_created",
            "status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    suite_version: Mapped[str] = mapped_column(String(32), nullable=False)
    case_id: Mapped[str] = mapped_column(String(96), nullable=False)
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    failed_checks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    agent_run_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tools_called: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_artifact_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
