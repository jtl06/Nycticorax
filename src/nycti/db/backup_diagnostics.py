"""Allowlisted backup diagnostics; never forward SDK messages or HTTP bodies."""
import re

PROVIDER_CODES = {
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "NoSuchBucket",
    "NoSuchKey", "ExpiredToken", "InvalidToken", "RequestTimeTooSkewed",
    "AuthorizationHeaderMalformed", "InvalidRequest", "InvalidArgument",
    "NotImplemented", "SlowDown", "ServiceUnavailable", "InternalError",
}
OPERATIONS = {"ListObjectsV2", "HeadObject", "PutObject", "GetObject", "CopyObject", "DeleteObject"}


def safe_backup_failure(payload: object) -> dict:
    result = {"error_type": "ChildProcessError"}
    if not isinstance(payload, dict):
        return result
    kind = payload.get("error_type")
    if isinstance(kind, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", kind):
        result["error_type"] = kind
    code, operation = payload.get("provider_code"), payload.get("operation")
    if isinstance(code, str) and code in PROVIDER_CODES:
        result["provider_code"] = code
    if isinstance(operation, str) and operation in OPERATIONS:
        result["operation"] = operation
    status = payload.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    return result


def backup_failure(error: Exception) -> dict:
    response = getattr(error, "response", {})
    response = response if isinstance(response, dict) else {}
    detail = response.get("Error", {})
    metadata = response.get("ResponseMetadata", {})
    return safe_backup_failure({
        "error_type": type(error).__name__,
        "provider_code": detail.get("Code") if isinstance(detail, dict) else None,
        "http_status": metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None,
        "operation": getattr(error, "operation_name", None),
    })


class BackupProcessError(RuntimeError):
    def __init__(self, payload: object):
        self.details = safe_backup_failure(payload)
        super().__init__(self.details["error_type"])
