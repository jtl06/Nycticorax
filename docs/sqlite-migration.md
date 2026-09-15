# SQLite Migration

Status: production cutover completed on 2026-09-15 at 03:17 UTC (September 14 local
time), using revision `6af32cc`. Python remains the runtime. One bot replica uses
`/data/nycti.db`; the previous Postgres database is retained, unchanged after the
freeze, for pre-cutover recovery only. Post-cutover writes are not mirrored to it.

## Operating Contract

- One bot replica, one shared Database engine, one durable local volume. Do not put
  SQLite on a network filesystem or use ephemeral container storage.
- Use `DATABASE_URL=sqlite+aiosqlite:////data/nycti.db`; the volume directory and
  database must be writable by the deployed bot UID 10001.
- File-backed SQLite uses WAL, synchronous FULL, foreign-key enforcement and a
  five-second SQLite busy timeout. One pooled connection serializes app transactions;
  pool acquisition waits at most thirty seconds. In-memory test databases are unchanged.
- Model and network calls must not hold database transactions open. Check warmed
  request latency under background memory/telemetry activity before accepting cutover.
- Chat context assembly and the memory-search tool release their preflight database
  sessions before generating query embeddings, then recheck access in a new session.
  Administrative and background paths still need observation under live SQLite load;
  this is not a claim that every caller-owned session has been redesigned.
- PostgreSQL pool settings do not affect SQLite. PostgreSQL support remains for rollback.
- Preserve the existing migration ledger in `db/session.py`; the copy utility does
  not add a second schema migration system.

## Rehearsal

Use this revision's code against the matching schema. The utility obtains a read-only
repeatable-read Postgres transaction and copies all known tables in bounded batches.
It refuses unreviewed schema drift, verifies ordered row counts/content hashes, preserves JSON
SQL-NULL versus literal-null semantics and large IDs, normalizes timestamps to UTC,
and checks SQLite integrity/foreign keys. Output is a private 0600 file, published only
after verification; an existing destination is never overwritten.
The reviewed retired `rss_feed_subscriptions` table is preserved as inert archive
data when present. Its PostgreSQL-only defaults are removed from the SQLite DDL;
all row values and its ID high-water mark are copied. This does not enable RSS.
New SQLite schemas use explicit AUTOINCREMENT for generated integer IDs. The copy
preserves source identity high-water marks, including deleted IDs, so old references
cannot silently point at newly inserted rows. Existing SQLite tables are not rebuilt
automatically. ORM timestamp reads remain UTC-aware through a SQLite-specific type.

```sh
python -m nycti.db.sqlite_transfer copy --source-env DATABASE_URL --destination /private/path/rehearsal.db
python -m nycti.db.sqlite_transfer backup --source /private/path/rehearsal.db --destination /private/path/restored.db
```

For Railway `run`, explicitly select the Postgres service and use
`--source-env DATABASE_PUBLIC_URL` when running on a local machine. Do not print the
URL or database rows in logs. Artifacts contain production data and must stay outside git.

The rehearsal is NOT the final copy: writes occurring after its snapshot are absent.
Before cutover, test a restored copy in an isolated bot with mocked providers and no
Discord login, including memories, reminders, diagnostics, quota reservation/finalization,
concurrent telemetry, and ID generation. Compare source/destination row manifests.

## Final Cutover Gate

1. Confirm the latest deployment and matching schema. Record the current database
   configuration securely. Take and verify a Postgres backup.
2. Provision the persistent volume and independent backup destination, verify ownership,
   and document how to retrieve backups if the bot/container/volume is unavailable.
3. Deploy with `NYCTI_MAINTENANCE_MODE=true` and verify the previous deployment has
   stopped and no bot/background writers remain. This parks before DB initialization
   or Discord login while keeping the volume accessible. Take a fresh
   final read-only export and verify it. Do not overlap Postgres and SQLite bot replicas.
4. Restore the verified file on the volume, then change DATABASE_URL, enable backups,
   set `NYCTI_MAINTENANCE_MODE=false` and start one replica.
5. Verify login, memory visibility, outstanding reminders, feedback archives, token quotas,
   and a bounded normal request. Measure resource usage and queue/DB latency under traffic.
6. Keep Postgres intact through an agreed observation window. Disable/remove it only
   after explicit approval; dropping the old service is not part of the copy operation.

Before SQLite accepts writes, rollback is simply restoring the old configuration and
starting the old revision. AFTER SQLite accepts writes, switching back would lose new
state: stop writers, preserve a fresh SQLite backup, and reconcile the changed data first.
No reverse-sync utility is implemented yet. This is an explicit cutover blocker if
lossless post-cutover rollback is required.

## Backup / Restore

Use the backup subcommand, not `cp` of an active `.db`: committed data can be in `-wal`.
It uses SQLite's online backup API, verifies the result, and refuses overwrite. Its
progress callback enforces a sixty-second deadline. A backup on the SAME volume is
not disaster recovery. Set up off-volume retention and rehearse restore before cutover.
Railway volume backups and PITR require Pro; no plan upgrade was made. Instead, the
authorized private `nycti-backups` object-storage bucket is independent of `/data`.
The rehearsal upload and download matched the original SHA-256, preserving all 22
tables and 177 memories. This protects against loss of the bot volume, not deletion
of the whole Railway project/account. Retain an independent local cutover copy too.

`SQLITE_BACKUP_ENABLED=true` starts a daily disposable backup subprocess after DB
initialization. Configuration is documented in README and `.env.example`. A recent
verified object suppresses duplicate startup backups. Failed jobs log
`sqlite_backup_failed` and retry after an hour; successful jobs log `sqlite_backup`
with key, checksum and bytes, never credentials or row data. Backups have a 512 MiB
safety limit and a three-minute process deadline. Shutdown kills and reaps the child.
Monitor failures and snapshot age: daily snapshots can lose up to 24 hours of writes
when healthy, and longer if backups fail. This is not continuous replication/PITR.

Uploads use SQLite's online backup API and private temporary files. Every upload is
downloaded, SHA-256 checked, and integrity/FK checked before it is marked verified
and old snapshots are deleted. Retention defaults to 30 days and only touches generated
snapshot names under the configured prefix, never other objects. Failed or interrupted
verification does not prune old backups. Rehearsal and production use separate prefixes.

Restore using bucket credentials kept outside the volume (Railway's bucket Credentials
view or CLI remains accessible even if the bot container is lost):

```sh
python -m nycti.db.sqlite_archive restore --key nycti/sqlite/production/KEY.db --destination /private/fresh-restored.db
```

Use the exact key from successful backup logs or the bucket object list. Restore refuses
to overwrite a destination and verifies checksum and SQLite integrity before publishing
it. Stop writers before replacing a live database; do not restore over a running WAL DB.
The daily backup worker is deployed and its first production upload/verification
succeeded at 03:17:15 UTC. Production uses the `nycti/sqlite/production/` prefix.

## Preparation Verification

- Combined CI check: Ruff, configured mypy targets, compilation and all 1,049 tests
  passed, with the existing audioop deprecation warning.
- Synthetic checks cover copy/restore, SQL versus JSON nulls, 64-bit Discord IDs,
  preserved deleted-ID high water marks, UTC dates, committed WAL backup, quota
  contention/idempotent settlement, due reminders and requester/guild memory isolation.
- The candidate includes the CI lint/type repairs and a shared `scripts/check_ci.py`
  entry point, used by both local validation and GitHub Actions.
- Delayed-embedding tests verify that an independent SQLite transaction can proceed,
  opt-out during the delay removes private prompt context, and cancellation leaves
  no connection checked out.
- After explicit transfer approval, the read-only production rehearsal passed:
  22 tables, 2,306 rows, including 177 memories and one archived legacy RSS row.
  All row digests and generated-ID high-water marks match after backup/restore.
- The restored copy passed application startup, UTC-aware timestamp reads, memory
  scope counts and 20 concurrent reads. WAL, synchronous FULL and foreign keys were verified.
- A persistent Railway `/data` volume is ready. `/data/rehearsal-20260915.db` is
  owned by UID/GID 10001 with mode 0600; `/data` has mode 0700. Downloading it back
  produced the same SHA-256 and passed integrity/foreign-key checks. Local artifacts
  are private and git-ignored under `.local/maintenance/sqlite-rehearsal/`.
- The final export was taken only after maintenance mode was active, the old deployment
  was removed, and Postgres reported no other client connections. It copied 22 tables
  and 2,261 rows, including all 177 memories. Every table hash and identity high-water
  mark verified. Separate local final and recovery copies remain private and git-ignored.
- The uploaded final file matched SHA-256 `e6ff0c3fd90d9978ea2883b386f063dd75dffe14670fd193356d6ab0b6ac88eb`.
  Integrity/FK checks passed under UID 10001 before activation.
- Live checks verified WAL, FULL sync, foreign keys and the five-second busy timeout;
  all 177 memories remain (168 private, 9 guild-shared), along with 82 feedback archives
  and one stored reminder. Twenty concurrent reads completed in 14 ms with no leaked
  pooled connections. Normal startup retention pruned expired telemetry as before.
- Discord login succeeded. The first automatic production S3 snapshot was downloaded
  and verified by the job (16,224,256 bytes, SHA-256
  `6917eecb57aa52b4eff98848557c78d2dd0139f58e87baa9ece4b2d94bbb78b9`).
- Railway initially reported about 130 MB for Nycti and 85 MB for the retained Postgres
  service. These are startup observations, not a warmed-load performance claim. No
  paid model benchmark was run during the cutover.

References: https://www.sqlite.org/wal.html and https://www.sqlite.org/backup.html.
