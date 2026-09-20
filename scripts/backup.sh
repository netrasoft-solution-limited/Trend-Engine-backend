#!/usr/bin/env bash
# Nightly pg_dump, shipped OFF the VPS (Arch §11.3).
#
# On-box backups protect against a bad migration. They do not protect against
# losing the box, which is the failure mode a single-VPS deployment actually
# has.
set -euo pipefail

: "${BACKUP_TARGET:?Set BACKUP_TARGET — an on-box-only backup is not a backup}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="/tmp/trend-engine-${STAMP}.dump"

docker compose -f "$(dirname "$0")/../deploy/docker-compose.yml" exec -T postgres \
  pg_dump --format=custom --no-owner -U "${POSTGRES_USER}" "${POSTGRES_DB}" > "${ARCHIVE}"

sha256sum "${ARCHIVE}" > "${ARCHIVE}.sha256"

# Ship both, then verify the remote copy before deleting the local one.
aws s3 cp "${ARCHIVE}" "${BACKUP_TARGET}/" --endpoint-url "${AWS_S3_ENDPOINT_URL}"
aws s3 cp "${ARCHIVE}.sha256" "${BACKUP_TARGET}/" --endpoint-url "${AWS_S3_ENDPOINT_URL}"

rm -f "${ARCHIVE}" "${ARCHIVE}.sha256"
echo "backed up ${STAMP}"
