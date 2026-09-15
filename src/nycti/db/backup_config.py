"""Validated configuration shared by the bot and its isolated backup process."""
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BackupConfig:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "auto"
    prefix: str = "nycti/sqlite/production/"
    retention_days: int = 30

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("SQLITE_BACKUP_ENDPOINT must be an HTTPS origin")
        if not all((self.bucket, self.access_key, self.secret_key, self.region)):
            raise ValueError("SQLite backup bucket, credentials and region are required")
        if (not self.prefix.startswith("nycti/sqlite/") or not self.prefix.endswith("/")
                or any(part in (".", "..") for part in self.prefix.split("/"))):
            raise ValueError("SQLITE_BACKUP_PREFIX must be a child of nycti/sqlite/ ending in /")
        if not 7 <= self.retention_days <= 90:
            raise ValueError("SQLITE_BACKUP_RETENTION_DAYS must be between 7 and 90")

    @classmethod
    def from_env(cls, env: Mapping[str, str]):
        enabled = env.get("SQLITE_BACKUP_ENABLED", "false").strip().lower()
        if enabled not in ("true", "false", "1", "0"):
            raise ValueError("SQLITE_BACKUP_ENABLED must be true or false")
        if enabled in ("false", "0"):
            return None
        return cls(
            endpoint=env.get("SQLITE_BACKUP_ENDPOINT", "").strip(),
            bucket=env.get("SQLITE_BACKUP_BUCKET", "").strip(),
            access_key=env.get("SQLITE_BACKUP_ACCESS_KEY_ID", "").strip(),
            secret_key=env.get("SQLITE_BACKUP_SECRET_ACCESS_KEY", "").strip(),
            region=env.get("SQLITE_BACKUP_REGION", "auto").strip(),
            prefix=env.get("SQLITE_BACKUP_PREFIX", "nycti/sqlite/production/").strip(),
            retention_days=int(env.get("SQLITE_BACKUP_RETENTION_DAYS", "30")),
        )

    def environment(self):
        return {
            "SQLITE_BACKUP_ENABLED": "true", "SQLITE_BACKUP_ENDPOINT": self.endpoint,
            "SQLITE_BACKUP_BUCKET": self.bucket, "SQLITE_BACKUP_ACCESS_KEY_ID": self.access_key,
            "SQLITE_BACKUP_SECRET_ACCESS_KEY": self.secret_key, "SQLITE_BACKUP_REGION": self.region,
            "SQLITE_BACKUP_PREFIX": self.prefix, "SQLITE_BACKUP_RETENTION_DAYS": str(self.retention_days),
        }
