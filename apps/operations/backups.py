"""Off-box database backup and the restore test that proves it — Arch §11.3.

Two claims are being defended, and they are different claims:

  · The dump leaves the box. An on-box backup survives a bad migration and
    nothing else; a single-VPS deployment's real failure mode is losing the VPS.
  · The dump can be restored. "An untested backup is not a backup, and this is
    an explicit acceptance criterion" (Arch §11.3). A backup job that has never
    been restored is a belief, not a control.

WHY THIS IS PYTHON AND NOT `scripts/backup.sh`. The shell scripts remain, and
remain correct, as the manual path a human runs from the host. They cannot be
what the scheduler calls: they drive `docker compose exec`, and the worker that
would run them is itself inside a container with no Docker socket. Running
`pg_dump` over the network from the worker removes that inversion.

It also makes backup health queryable. Every attempt — including one that
refuses because nothing is configured — writes a `BackupRun`, so "did last
night's backup run?" is a database question rather than an SSH session. A
failure recorded nowhere is indistinguishable from a success until the day it
matters.

The accepted cost: a backup now depends on Celery and Redis being up. That is
visible in the `BackupRun` table as a missing row, which is the thing the
Operations screen alerts on.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from .models import BackupRun

logger = logging.getLogger(__name__)

#: pg_dump streams, but a very large database on a small droplet should not be
#: allowed to run until the next night's job starts on top of it.
DUMP_TIMEOUT_SECONDS = 60 * 50
RESTORE_TIMEOUT_SECONDS = 60 * 50


class BackupError(RuntimeError):
    """The backup could not be taken, or could not be proven restorable."""


@dataclass(frozen=True)
class Target:
    bucket: str
    prefix: str

    def key(self, filename: str) -> str:
        return f"{self.prefix}/{filename}" if self.prefix else filename


def parse_target(raw: str) -> Target:
    """`s3://bucket/some/prefix` → Target('bucket', 'some/prefix')."""
    parsed = urlparse(raw)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise BackupError(
            f"BACKUP_TARGET must look like s3://bucket/prefix, not {raw!r}."
        )
    return Target(bucket=parsed.netloc, prefix=parsed.path.strip("/"))


def _client():
    """Deliberately lazy. boto3 arrives with django-storages, but importing it
    at module scope would make `manage.py check` depend on it."""
    import boto3

    return boto3.client("s3", endpoint_url=settings.BACKUP_S3_ENDPOINT_URL)


def _pg_env() -> dict[str, str]:
    db = settings.DATABASES["default"]
    env = dict(os.environ)
    env["PGPASSWORD"] = db.get("PASSWORD") or ""
    return env


def _pg_args() -> list[str]:
    db = settings.DATABASES["default"]
    return [
        "--host", db.get("HOST") or "localhost",
        "--port", str(db.get("PORT") or 5432),
        "--username", db.get("USER") or "postgres",
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: list[str], *, timeout: int, what: str) -> str:
    """A failed subprocess must say what failed. `check=True` alone gives a
    return code and swallows the stderr that explains it."""
    result = subprocess.run(
        argv, env=_pg_env(), capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise BackupError(f"{what} failed ({result.returncode}): {result.stderr.strip()[:2000]}")
    return result.stdout


def _record(kind: str, started, outcome: str, **fields) -> BackupRun:
    return BackupRun.objects.create(
        kind=kind,
        outcome=outcome,
        started_at=started,
        finished_at=timezone.now(),
        **fields,
    )


# ── Nightly backup ──────────────────────────────────────────────────────────


def run_backup() -> BackupRun:
    """Dump, checksum, ship off the box, prune. Returns the recorded run.

    Never raises for an unconfigured target: a stack with no bucket yet should
    boot and run, and the refusal is recorded as a SKIPPED run so it shows up
    on the Operations screen as the gap it is.
    """
    started = timezone.now()
    kind = BackupRun.Kind.BACKUP

    if not settings.BACKUP_TARGET:
        logger.warning("BACKUP_TARGET is unset — no off-box backup is being taken")
        return _record(
            kind,
            started,
            BackupRun.Outcome.SKIPPED,
            detail=(
                "BACKUP_TARGET is not set. Nothing is being shipped off the VPS, "
                "so losing the box loses the database."
            ),
        )

    db = settings.DATABASES["default"]
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    filename = f"trend-engine-{stamp}.dump"

    try:
        target = parse_target(settings.BACKUP_TARGET)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / filename

            # --format=custom so pg_restore can be selective, --no-owner so the
            # dump restores into a database whose roles differ from production's.
            _run(
                ["pg_dump", *_pg_args(), "--format=custom", "--no-owner",
                 "--file", str(archive), db["NAME"]],
                timeout=DUMP_TIMEOUT_SECONDS,
                what="pg_dump",
            )

            size = archive.stat().st_size
            if size == 0:
                raise BackupError("pg_dump produced an empty file")
            checksum = _sha256(archive)

            client = _client()
            client.upload_file(str(archive), target.bucket, target.key(filename))
            # The checksum travels beside the dump so the restore test can
            # verify the download without trusting this process's memory of it.
            client.put_object(
                Bucket=target.bucket,
                Key=target.key(f"{filename}.sha256"),
                Body=f"{checksum}  {filename}\n".encode(),
            )

        pruned = _prune(target)
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        logger.exception("Nightly backup failed")
        _record(kind, started, BackupRun.Outcome.FAILED, artifact=filename, detail=str(exc)[:4000])
        raise

    logger.info("Backed up %s (%d bytes); pruned %d old object(s)", filename, size, pruned)
    return _record(
        kind,
        started,
        BackupRun.Outcome.SUCCEEDED,
        artifact=filename,
        size_bytes=size,
        sha256=checksum,
        detail=f"Pruned {pruned} object(s) older than {settings.BACKUP_RETENTION_DAYS} days.",
    )


def _prune(target: Target) -> int:
    """Delete dumps past the retention window.

    Bounded on purpose: without this the bucket grows without limit and the
    bill is the first thing anyone notices. Bucket versioning (Arch §11.3) is
    the separate protection against a malicious or mistaken delete.
    """
    cutoff = timezone.now() - timedelta(days=settings.BACKUP_RETENTION_DAYS)
    client = _client()
    removed = 0
    for obj in _list_objects(client, target):
        if obj["LastModified"] < cutoff:
            client.delete_object(Bucket=target.bucket, Key=obj["Key"])
            removed += 1
    return removed


def _list_objects(client, target: Target) -> list[dict]:
    paginator = client.get_paginator("list_objects_v2")
    found: list[dict] = []
    for page in paginator.paginate(Bucket=target.bucket, Prefix=target.prefix):
        found.extend(page.get("Contents", []))
    return found


# ── Weekly restore test ─────────────────────────────────────────────────────


def run_restore_test() -> BackupRun:
    """Restore the newest dump into a disposable database and assert it holds data.

    Restoring without error is necessary and not sufficient: a dump of an empty
    database restores perfectly. The row assertion is the actual test.
    """
    started = timezone.now()
    kind = BackupRun.Kind.RESTORE_TEST

    if not settings.BACKUP_TARGET:
        return _record(
            kind,
            started,
            BackupRun.Outcome.SKIPPED,
            detail="BACKUP_TARGET is not set, so there is nothing to restore from.",
        )

    scratch = f"restore_test_{started:%Y%m%d%H%M%S}"
    latest = ""
    try:
        target = parse_target(settings.BACKUP_TARGET)
        client = _client()

        dumps = sorted(
            (o for o in _list_objects(client, target) if o["Key"].endswith(".dump")),
            key=lambda o: o["LastModified"],
        )
        if not dumps:
            raise BackupError(f"No .dump objects under {settings.BACKUP_TARGET}")
        latest = dumps[-1]["Key"]

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / Path(latest).name
            client.download_file(target.bucket, latest, str(archive))

            expected = (
                client.get_object(Bucket=target.bucket, Key=f"{latest}.sha256")["Body"]
                .read()
                .decode()
                .split()[0]
            )
            actual = _sha256(archive)
            if actual != expected:
                raise BackupError(
                    f"Checksum mismatch on {latest}: expected {expected}, got {actual}. "
                    f"The stored dump is corrupt or truncated."
                )

            rows = _restore_and_count(archive, scratch)
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        logger.exception("Restore test failed")
        _record(kind, started, BackupRun.Outcome.FAILED, artifact=latest, detail=str(exc)[:4000])
        raise

    logger.info("Restore test passed — %d organisations recovered from %s", rows, latest)
    return _record(
        kind,
        started,
        BackupRun.Outcome.SUCCEEDED,
        artifact=latest,
        detail=f"{rows} organisation(s) recovered into {scratch}, which was then dropped.",
    )


def _restore_and_count(archive: Path, scratch: str) -> int:
    """Create, restore, count, drop. The drop runs even when the count fails —
    otherwise a failing restore test leaves a database behind every week until
    the disk fills."""
    db = settings.DATABASES["default"]
    _run(["createdb", *_pg_args(), scratch], timeout=120, what="createdb")
    try:
        _run(
            ["pg_restore", *_pg_args(), "--no-owner", "--dbname", scratch, str(archive)],
            timeout=RESTORE_TIMEOUT_SECONDS,
            what="pg_restore",
        )
        out = _run(
            ["psql", *_pg_args(), "--tuples-only", "--no-align",
             "--dbname", scratch, "--command", "SELECT count(*) FROM tenancy_organization"],
            timeout=120,
            what="psql row count",
        )
        rows = int(out.strip() or 0)
        if rows < 1:
            raise BackupError(
                f"Restored {db['NAME']} has no organisations. The dump restored "
                f"without error but contains no tenants, which is not a usable backup."
            )
        return rows
    finally:
        try:
            _run(["dropdb", *_pg_args(), "--if-exists", scratch], timeout=120, what="dropdb")
        except Exception:  # noqa: BLE001
            logger.exception("Could not drop the scratch database %s", scratch)
