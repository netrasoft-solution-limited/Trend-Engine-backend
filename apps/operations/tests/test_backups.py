"""Arch §11.3 — the backup leaves the box, and it is known to restore.

What is worth testing here is not pg_dump; it is every way this can appear to
work while protecting nothing:

  · an unset BACKUP_TARGET quietly doing nothing
  · a zero-byte or truncated dump shipped and recorded as a success
  · a restore that "succeeds" into an empty database
  · a failed restore leaving its scratch database behind every week
  · a failure that raises and is recorded nowhere

Postgres and S3 are both faked. A test that needed a live bucket would not run
in CI, and the parts that break are the decisions, not the binaries.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.operations import backups
from apps.operations.models import BackupRun

pytestmark = pytest.mark.django_db

TARGET = "s3://backups/trend-engine"


@pytest.fixture
def configured(settings):
    settings.BACKUP_TARGET = TARGET
    settings.BACKUP_S3_ENDPOINT_URL = None
    settings.BACKUP_RETENTION_DAYS = 30


class FakeS3:
    """Just enough bucket to hold objects and hand them back."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.times: dict[str, object] = {}
        self.deleted: list[str] = []

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as handle:
            self.put(key, handle.read())

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 — boto3's casing
        self.put(Key, Body)

    def put(self, key, body, *, when=None):
        self.objects[key] = body
        self.times[key] = when or timezone.now()

    def download_file(self, bucket, key, path):
        with open(path, "wb") as handle:
            handle.write(self.objects[key])

    def get_object(self, *, Bucket, Key):  # noqa: N803
        import io

        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        bucket = self

        class Paginator:
            def paginate(self, **kwargs):
                prefix = kwargs.get("Prefix") or ""
                yield {
                    "Contents": [
                        {"Key": k, "LastModified": bucket.times[k], "Size": len(v)}
                        for k, v in bucket.objects.items()
                        if k.startswith(prefix)
                    ]
                }

        return Paginator()


@pytest.fixture
def s3(monkeypatch):
    bucket = FakeS3()
    monkeypatch.setattr(backups, "_client", lambda: bucket)
    return bucket


@pytest.fixture
def pg(monkeypatch):
    """Stands in for pg_dump/pg_restore/psql, recording what was asked of it."""

    calls: list[list[str]] = []

    class Fake:
        dump_bytes = b"PGDMP fake dump payload"
        rows = "3"

        def __call__(self, argv, *, timeout, what):
            calls.append(argv)
            if argv[0] == "pg_dump":
                path = argv[argv.index("--file") + 1]
                with open(path, "wb") as handle:
                    handle.write(self.dump_bytes)
                return ""
            if argv[0] == "psql":
                return f"{self.rows}\n"
            return ""

    fake = Fake()
    fake.calls = calls
    monkeypatch.setattr(backups, "_run", fake)
    return fake


# ── Not configured is a finding, not a no-op ────────────────────────────────


def test_an_unset_target_records_a_skipped_run_rather_than_doing_nothing(settings):
    """The whole point of the row: an unconfigured backup must not look like a
    working one."""
    settings.BACKUP_TARGET = ""

    run = backups.run_backup()

    assert run.outcome == BackupRun.Outcome.SKIPPED
    assert "BACKUP_TARGET" in run.detail
    assert BackupRun.latest(BackupRun.Kind.BACKUP) == run


def test_an_unset_target_also_records_the_restore_test_as_skipped(settings):
    settings.BACKUP_TARGET = ""

    assert backups.run_restore_test().outcome == BackupRun.Outcome.SKIPPED


def test_a_malformed_target_is_refused(settings, s3, pg):
    settings.BACKUP_TARGET = "/var/backups"

    with pytest.raises(backups.BackupError, match="s3://"):
        backups.run_backup()

    assert BackupRun.latest(BackupRun.Kind.BACKUP).outcome == BackupRun.Outcome.FAILED


# ── The happy path, and what it must leave behind ───────────────────────────


def test_a_backup_ships_the_dump_and_its_checksum(configured, s3, pg):
    run = backups.run_backup()

    keys = sorted(s3.objects)
    assert len(keys) == 2
    assert keys[0].endswith(".dump") and keys[1].endswith(".dump.sha256")
    assert keys[0].startswith("trend-engine/trend-engine-")

    expected = hashlib.sha256(pg.dump_bytes).hexdigest()
    assert run.sha256 == expected
    assert expected.encode() in s3.objects[keys[1]], "the checksum travels with the dump"


def test_a_successful_backup_is_recorded_with_its_size(configured, s3, pg):
    run = backups.run_backup()

    assert run.outcome == BackupRun.Outcome.SUCCEEDED
    assert run.size_bytes == len(pg.dump_bytes)
    assert run.finished_at is not None
    assert run.duration_seconds >= 0


def test_the_dump_is_portable_between_hosts(configured, s3, pg):
    """`--no-owner`, or the dump only restores where the same roles exist —
    which is exactly not the case on the disposable host used for recovery."""
    backups.run_backup()

    dump_argv = next(a for a in pg.calls if a[0] == "pg_dump")
    assert "--no-owner" in dump_argv
    assert "--format=custom" in dump_argv


# ── Ways a backup can lie ───────────────────────────────────────────────────


def test_an_empty_dump_is_a_failure_not_a_backup(configured, s3, pg):
    """pg_dump can exit 0 and write nothing. Shipping that would replace a real
    backup with a file that restores into an empty database."""
    pg.dump_bytes = b""

    with pytest.raises(backups.BackupError, match="empty"):
        backups.run_backup()

    assert s3.objects == {}, "nothing should have been uploaded"
    assert BackupRun.latest(BackupRun.Kind.BACKUP).outcome == BackupRun.Outcome.FAILED


def test_a_failed_backup_is_recorded_before_it_raises(configured, s3, pg, monkeypatch):
    """A failure nobody can see later is the same as no backup."""

    def explode(argv, *, timeout, what):
        raise backups.BackupError("pg_dump failed (1): could not connect to server")

    monkeypatch.setattr(backups, "_run", explode)

    with pytest.raises(backups.BackupError):
        backups.run_backup()

    run = BackupRun.latest(BackupRun.Kind.BACKUP)
    assert run.outcome == BackupRun.Outcome.FAILED
    assert "could not connect" in run.detail


def test_old_dumps_are_pruned_and_recent_ones_are_not(configured, s3, pg):
    now = timezone.now()
    s3.put("trend-engine/trend-engine-old.dump", b"x", when=now - timedelta(days=60))
    s3.put("trend-engine/trend-engine-recent.dump", b"x", when=now - timedelta(days=2))

    backups.run_backup()

    assert "trend-engine/trend-engine-old.dump" in s3.deleted
    assert "trend-engine/trend-engine-recent.dump" not in s3.deleted


# ── The restore test is the actual control ──────────────────────────────────


def test_a_restore_test_verifies_the_checksum_before_trusting_the_dump(configured, s3, pg):
    backups.run_backup()
    key = next(k for k in s3.objects if k.endswith(".dump"))
    s3.objects[key] = b"truncated"  # a half-finished upload

    with pytest.raises(backups.BackupError, match="corrupt or truncated"):
        backups.run_restore_test()

    assert BackupRun.latest(BackupRun.Kind.RESTORE_TEST).outcome == BackupRun.Outcome.FAILED


def test_a_restore_that_produces_no_tenants_fails(configured, s3, pg):
    """A dump of an empty database restores perfectly. Counting rows is what
    makes this a test rather than a syntax check."""
    backups.run_backup()
    pg.rows = "0"

    with pytest.raises(backups.BackupError, match="no organisations"):
        backups.run_restore_test()


def test_a_passing_restore_test_is_recorded(configured, s3, pg):
    backups.run_backup()

    run = backups.run_restore_test()

    assert run.outcome == BackupRun.Outcome.SUCCEEDED
    assert "3 organisation(s)" in run.detail


def test_the_scratch_database_is_dropped_even_when_the_restore_fails(configured, s3, pg):
    """Otherwise a weekly failure leaves a database behind every week until the
    disk fills — a second outage caused by the detector of the first."""
    backups.run_backup()
    pg.rows = "0"

    with pytest.raises(backups.BackupError):
        backups.run_restore_test()

    assert any(a[0] == "dropdb" for a in pg.calls), "the scratch database was left behind"


def test_the_newest_dump_is_the_one_tested(configured, s3, pg):
    now = timezone.now()
    s3.put("trend-engine/trend-engine-ancient.dump", b"old", when=now - timedelta(days=9))
    backups.run_backup()

    run = backups.run_restore_test()

    assert "ancient" not in run.artifact


def test_an_empty_bucket_fails_rather_than_passing_vacuously(configured, s3, pg):
    with pytest.raises(backups.BackupError, match="No .dump objects"):
        backups.run_restore_test()
