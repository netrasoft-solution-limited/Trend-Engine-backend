#!/usr/bin/env bash
# Weekly restore test into a disposable database — Arch §11.3.
#
# "An untested backup is not a backup, and this is an explicit acceptance
#  criterion." A backup job that has never been restored is a belief, not a
#  control. This is the script that turns it into one.
#
# Fails loudly. A silent failure here would leave the team believing they have
# recovery they do not have, which is worse than having no script at all.
set -euo pipefail

: "${BACKUP_TARGET:?}"

SCRATCH="restore_test_$(date -u +%Y%m%d)"
LATEST="$(aws s3 ls "${BACKUP_TARGET}/" --endpoint-url "${AWS_S3_ENDPOINT_URL}" \
          | grep '\.dump$' | sort | tail -1 | awk '{print $4}')"

aws s3 cp "${BACKUP_TARGET}/${LATEST}" "/tmp/${LATEST}" --endpoint-url "${AWS_S3_ENDPOINT_URL}"
aws s3 cp "${BACKUP_TARGET}/${LATEST}.sha256" "/tmp/" --endpoint-url "${AWS_S3_ENDPOINT_URL}"
(cd /tmp && sha256sum -c "${LATEST}.sha256")

COMPOSE="$(dirname "$0")/../deploy/docker-compose.yml"
docker compose -f "${COMPOSE}" exec -T postgres createdb -U "${POSTGRES_USER}" "${SCRATCH}"
docker compose -f "${COMPOSE}" exec -T postgres \
  pg_restore --no-owner -U "${POSTGRES_USER}" -d "${SCRATCH}" < "/tmp/${LATEST}"

# Restoring without error is necessary but not sufficient — assert the data is
# actually there. A dump of an empty database restores perfectly.
ROWS="$(docker compose -f "${COMPOSE}" exec -T postgres psql -tAc \
  "SELECT count(*) FROM tenancy_organization" -U "${POSTGRES_USER}" -d "${SCRATCH}")"

docker compose -f "${COMPOSE}" exec -T postgres dropdb -U "${POSTGRES_USER}" "${SCRATCH}"
rm -f "/tmp/${LATEST}" "/tmp/${LATEST}.sha256"

if [ "${ROWS}" -lt 1 ]; then
  echo "RESTORE TEST FAILED: restored database has no organizations" >&2
  exit 1
fi

echo "restore test passed — ${ROWS} organizations recovered from ${LATEST}"
