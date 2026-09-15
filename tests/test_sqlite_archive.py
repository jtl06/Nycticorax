import asyncio
from datetime import datetime, timedelta, timezone
import io
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nycti.db.backup_config import BackupConfig
from nycti.db.backup_worker import SQLiteBackupWorker
from nycti.db.sqlite_archive import archive, restore


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.corrupt = False

    def get_paginator(self, _):
        return self

    def paginate(self, **kwargs):
        return [{"Contents": [{"Key": key, "LastModified": value[2]}
                              for key, value in self.objects.items() if key.startswith(kwargs["Prefix"])]}]

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = (kwargs["Body"].read(), kwargs["Metadata"], NOW)

    def get_object(self, **kwargs):
        body, metadata, _ = self.objects[kwargs["Key"]]
        return {"Body": io.BytesIO(body + b"broken" if self.corrupt else body),
                "ContentLength": len(body), "Metadata": metadata}

    def head_object(self, **kwargs):
        return {"Metadata": self.objects[kwargs["Key"]][1]}

    def copy_object(self, **kwargs):
        body, _, created = self.objects[kwargs["Key"]]
        self.objects[kwargs["Key"]] = (body, kwargs["Metadata"], created)

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs["Key"])
        del self.objects[kwargs["Key"]]


@pytest.fixture
def config():
    return BackupConfig("https://storage.example", "test", "fake-access", "fake-secret")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO facts VALUES (1, 'fixture')")
    return path


def test_verified_roundtrip_and_daily_skip(config, source, tmp_path):
    client = FakeS3()
    report = archive(client, config, source, now=NOW)
    destination = tmp_path / "restored.db"
    restored = restore(client, config, report["key"], destination)
    assert restored["sha256"] == report["sha256"]
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM facts").fetchone() == ("fixture",)
    assert archive(client, config, source, now=NOW + timedelta(hours=2))["skipped"]
    assert len(client.objects) == 1
    with pytest.raises(FileExistsError):
        restore(client, config, report["key"], destination)


def test_failed_verification_does_not_prune_or_publish(config, source, tmp_path):
    client = FakeS3()
    previous = archive(client, config, source, now=NOW)["key"]
    client.corrupt = True
    with pytest.raises(ValueError):
        archive(client, config, source, now=NOW + timedelta(days=40))
    assert not client.deleted
    assert previous in client.objects
    destination = tmp_path / "bad.db"
    with pytest.raises(ValueError):
        restore(client, config, previous, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".nycti-copy-*"))


def test_prunes_only_old_managed_keys_after_new_verification(config, source):
    client = FakeS3()
    old = archive(client, config, source, now=NOW)["key"]
    unrelated = config.prefix + "manual-keep.db"
    client.objects[unrelated] = (b"manual", {}, NOW)
    report = archive(client, config, source, now=NOW + timedelta(days=40))
    assert report["verified"] and client.deleted == [old]
    assert unrelated in client.objects


def test_unverified_upload_does_not_suppress_backup(config, source):
    client = FakeS3()
    first = archive(client, config, source, now=NOW)["key"]
    body, _, created = client.objects[first]
    client.objects[first] = (body, {}, created)
    assert archive(client, config, source, now=NOW)["verified"]
    assert len(client.objects) == 2


def test_backup_config_is_opt_in_and_secrets_not_in_repr(config):
    assert BackupConfig.from_env({}) is None
    assert "fake-secret" not in repr(config) and "fake-access" not in repr(config)
    assert BackupConfig.from_env(config.environment()) == config
    with pytest.raises(ValueError):
        BackupConfig.from_env({**config.environment(), "SQLITE_BACKUP_ENDPOINT": "http://insecure"})
    with pytest.raises(ValueError):
        BackupConfig.from_env({**config.environment(), "SQLITE_BACKUP_PREFIX": "other-app/"})


def test_worker_never_starts_for_postgres(config):
    worker = SQLiteBackupWorker(SimpleNamespace(sqlite_backup=config, database_url="postgresql://host/db"))
    worker.start()
    assert worker.task is None


def test_cancelled_worker_kills_and_reaps_child(config):
    async def run():
        started = asyncio.Event()

        async def communicate():
            started.set()
            await asyncio.Event().wait()

        process = SimpleNamespace(communicate=communicate, returncode=None, wait=AsyncMock())
        killed = []
        process.kill = lambda: killed.append(True)
        worker = SQLiteBackupWorker(SimpleNamespace(sqlite_backup=config, database_url="sqlite:///file.db"))
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            worker.start()
            await started.wait()
            await worker.close()
        assert killed == [True]
        process.wait.assert_awaited_once()
        assert worker.task is None

    asyncio.run(run())
