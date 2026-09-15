"""Private S3 snapshots; only verified, namespaced backups are eligible for retention."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from nycti.db.backup_config import BackupConfig
from nycti.db.sqlite_transfer import backup_sqlite, new_destination, _verify_sqlite

MAX_BYTES = 512 * 1024 * 1024
DAY = 86400
KEY_PATTERN = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{32}\.db")


def make_client(config):
    # Kept out of the long-running bot process to avoid retaining the SDK at idle.
    import boto3
    from botocore.config import Config

    return boto3.client("s3", endpoint_url=config.endpoint, region_name=config.region,
                        aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
                        config=Config(connect_timeout=10, read_timeout=30,
                                      retries={"max_attempts": 2, "mode": "standard"},
                                      s3={"addressing_style": "virtual"}))


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def managed_key(config, key):
    return key.startswith(config.prefix) and KEY_PATTERN.fullmatch(key[len(config.prefix):]) is not None


def snapshots(client, config):
    result = []
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=config.bucket, Prefix=config.prefix)
    for page_number, page in enumerate(pages):
        if page_number >= 5:
            raise ValueError("Backup namespace exceeds bounded listing size")
        result.extend(item for item in page.get("Contents", ()) if managed_key(config, item["Key"]))
    return result


def restore(client, config, key, destination):
    if not managed_key(config, key):
        raise ValueError("Not a managed backup key")
    with new_destination(destination) as temporary:
        response = client.get_object(Bucket=config.bucket, Key=key)
        with closing(response["Body"]) as body:
            checksum = response.get("Metadata", {}).get("sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("Backup has no valid checksum")
            if response.get("ContentLength", MAX_BYTES + 1) > MAX_BYTES:
                raise ValueError("Backup exceeds size limit")
            size = 0
            with temporary.open("wb") as output:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Backup exceeds size limit")
                    output.write(chunk)
        if size != response["ContentLength"] or digest(temporary) != checksum:
            raise ValueError("Backup content verification failed")
        _verify_sqlite(temporary)
    return {"key": key, "sha256": checksum, "bytes": size, "verified": True}


def archive(client, config, source, *, force=False, now=None):
    now = now or datetime.now(timezone.utc)
    existing = snapshots(client, config)
    # Only a completed upload with a separate verification marker may suppress work.
    for item in sorted(existing, key=lambda row: row["LastModified"], reverse=True)[:1]:
        age = (now - item["LastModified"]).total_seconds()
        if not force and 0 <= age < DAY:
            marker = client.head_object(Bucket=config.bucket, Key=item["Key"])
            if marker.get("Metadata", {}).get("verified") == "true":
                return {"skipped": True, "next_seconds": max(60, int(DAY - age))}
    with tempfile.TemporaryDirectory(prefix="nycti-backup-") as directory:
        local = Path(directory) / "snapshot.db"
        backup_sqlite(source, local)
        size, checksum = local.stat().st_size, digest(local)
        if size > MAX_BYTES:
            raise ValueError("Snapshot exceeds 512 MiB safety limit")
        key = config.prefix + now.strftime("%Y%m%dT%H%M%S%fZ-") + uuid4().hex + ".db"
        with local.open("rb") as body:
            client.put_object(Bucket=config.bucket, Key=key, Body=body, ContentLength=size,
                              ContentType="application/vnd.sqlite3", Metadata={"sha256": checksum})
        verified = restore(client, config, key, Path(directory) / "download.db")
        if verified["sha256"] != checksum:
            raise ValueError("Uploaded backup differs from local snapshot")
        # Mark the object only after full download, checksum and SQLite checks succeed.
        client.copy_object(Bucket=config.bucket, Key=key,
                           CopySource={"Bucket": config.bucket, "Key": key},
                           MetadataDirective="REPLACE", ContentType="application/vnd.sqlite3",
                           Metadata={"sha256": checksum, "verified": "true"})
        cutoff = now - timedelta(days=config.retention_days)
        pruned = 0
        for item in existing:
            if item["LastModified"] < cutoff:
                client.delete_object(Bucket=config.bucket, Key=item["Key"])
                pruned += 1
        return {**verified, "pruned": pruned, "next_seconds": DAY}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("--source", required=True, type=Path)
    upload.add_argument("--force", action="store_true")
    download = commands.add_parser("restore")
    download.add_argument("--key", required=True)
    download.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = BackupConfig.from_env(os.environ)
        if config is None:
            raise ValueError("Backups are disabled")
        with closing(make_client(config)) as client:
            report = (archive(client, config, args.source, force=args.force)
                      if args.command == "upload" else restore(client, config, args.key, args.destination))
    except Exception as error:
        # SDK exceptions and HTTP debug output can expose signed URLs or credentials.
        parser.exit(1, f"sqlite_backup_failed error_type={type(error).__name__}\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
