from nycti.llm.provider_policy import ProviderErrorKind, classify_provider_error


def test_daily_quota_error_is_distinct_from_temporary_rate_limit() -> None:
    assert classify_provider_error(
        Exception("429 insufficient_quota: daily token limit reached")
    ) == ProviderErrorKind.QUOTA_EXHAUSTED
    assert classify_provider_error(
        Exception("429 rate limit: model is busy")
    ) == ProviderErrorKind.RATE_LIMIT
