#!/usr/bin/env bash
# INTERIM nightly backup, kept ON THE SERVER until an off-site bucket exists.
#
# This protects against a bad migration or a mistaken delete. It does NOT
# protect against losing the server — that needs `scripts/backup.sh` or the
# Celery `nightly_backup` task with BACKUP_TARGET set, which is waiting on a
# storage bucket. Installed by cron (see docs/HANDOVER.md):
#
#   0 3 * * *  ~/trend-engine/backend/scripts/onbox_backup.sh >> ~/backups/backup.log 2>&1
#
# Every dump is checked as a readable archive (`pg_restore --list`) before the
# old ones are pruned, so a broken dump never replaces a good one.
set -euo pipefail

DEST="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
CONTAINER="${PG_CONTAINER:-trend-engine-postgres-1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$DEST/trend-engine-$STAMP.dump"

mkdir -p "$DEST"
docker exec "$CONTAINER" sh -c 'pg_dump --format=custom --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$ARCHIVE"

tables="$(docker exec -i "$CONTAINER" pg_restore --list < "$ARCHIVE" | grep -c "TABLE DATA" || true)"
if [ "${tables:-0}" -lt 10 ]; then
  echo "$STAMP FAILED: dump has ${tables:-0} tables of data; keeping older dumps" >&2
  exit 1
fi
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

find "$DEST" -name 'trend-engine-*.dump*' -mtime +"$KEEP_DAYS" -delete
echo "$STAMP ok: $(du -h "$ARCHIVE" | cut -f1), $tables tables of data, kept $KEEP_DAYS days (on-box only)"
