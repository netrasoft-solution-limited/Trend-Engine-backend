# Restoring Trend Engine

Written for whoever is doing the recovery — possibly not the person who built
this, possibly at three in the morning. Every command is meant to be pasted.

PRD §14 requires a documented restore procedure. Arch §11.3 requires that it is
tested rather than believed: the weekly restore test exists so that this
document is known to work on a week when nobody needed it.

---

## First, what kind of bad day is this?

| Symptom | Go to |
|---|---|
| Bad migration or bad data; the box is fine | [Restore into a scratch database](#a-restore-into-a-scratch-database) |
| The droplet is gone, or unreachable and not coming back | [Rebuild from nothing](#b-rebuild-from-nothing) |
| "Is the backup even working?" | [Check first](#check-first) |

Do not start by restoring over the live database. Restore beside it, look at
it, then swap. The one irreversible step in this document is the one in
[section C](#c-swap-the-restored-database-in).

---

## Check first

```bash
docker compose exec worker-default python manage.py backup status
```

Three answers and what each means:

- **`succeeded at <recent>`** — there is a dump from that night. Proceed.
- **`skipped`** — `BACKUP_TARGET` is unset. **There is no off-box backup.** If
  the box is gone, stop reading; recovery is from whatever else exists.
- **`never run`** — beat has not fired, or the worker cannot reach Redis. Check
  `docker compose ps` and `docker compose logs beat`.

If the box itself is gone, ask the bucket instead:

```bash
aws s3 ls "$BACKUP_TARGET/" --endpoint-url "$AWS_S3_ENDPOINT_URL" | tail -5
```

Dumps are named `trend-engine-<UTC timestamp>.dump`, each with a `.sha256`
beside it.

---

## A. Restore into a scratch database

This is the safe case and the common one. Nothing live is touched.

```bash
# 1. Point at the newest dump (or pick an older one from the listing above).
export KEY=$(aws s3 ls "$BACKUP_TARGET/" --endpoint-url "$AWS_S3_ENDPOINT_URL" \
             | grep '\.dump$' | sort | tail -1 | awk '{print $4}')

# 2. Fetch it and its checksum, and verify before trusting it.
aws s3 cp "$BACKUP_TARGET/$KEY"        /tmp/ --endpoint-url "$AWS_S3_ENDPOINT_URL"
aws s3 cp "$BACKUP_TARGET/$KEY.sha256" /tmp/ --endpoint-url "$AWS_S3_ENDPOINT_URL"
(cd /tmp && sha256sum -c "$KEY.sha256")   # must print "OK"

# 3. Restore beside the live database, not over it.
docker compose exec -T postgres createdb -U "$POSTGRES_USER" recovery
docker compose exec -T postgres pg_restore --no-owner -U "$POSTGRES_USER" \
  -d recovery < "/tmp/$KEY"

# 4. Look at what you got before believing it.
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d recovery -c "
  SELECT (SELECT count(*) FROM tenancy_organization)  AS orgs,
         (SELECT count(*) FROM evidence_contentitem)  AS items,
         (SELECT count(*) FROM enrichment_claim)      AS claims,
         (SELECT count(*) FROM publication_publication
           WHERE unpublished_at IS NULL)              AS live_publications;"
```

A restore that completes without error but shows zero organisations is **not a
successful restore** — a dump of an empty database restores perfectly. That row
count is the test.

If you only needed to read something, stop here and
`dropdb -U "$POSTGRES_USER" recovery` when finished.

---

## B. Rebuild from nothing

The droplet is gone. You need a new one plus the bucket.

1. **New host.** Ubuntu 22.04+, Docker and the compose plugin. Point the DNS A
   records for both `<domain>` and `ops.<domain>` at it — Caddy will not get a
   certificate for a name that does not resolve to it.

2. **The repository and the environment file.**
   ```bash
   git clone <backend repo> && cd backend
   cp deploy/.env.example deploy/.env
   ```
   Fill in `deploy/.env`. These are the ones that make recovery fail silently if
   wrong, so check them twice:

   | Variable | Why it matters here |
   |---|---|
   | `DJANGO_SECRET_KEY` | A new value invalidates every existing session and password-reset link. That is acceptable; a *blank* one will not boot. |
   | `CREDENTIALS_ENCRYPTION_KEY` | **Must be the value the old deployment used.** Vendor API keys in the dump are encrypted with it. Lose it and every provider credential must be re-entered by hand. |
   | `BACKUP_TARGET`, `AWS_*` | Needed before the first night, or the new box is as unprotected as the old one was. |
   | `POSTGRES_*` | Must match what the dump expects for the role name, or restore with `--no-owner` as below. |

3. **Bring up only the database**, so nothing starts writing to an empty one:
   ```bash
   docker compose -f deploy/docker-compose.yml up -d postgres redis
   ```

4. **Restore**, following [section A](#a-restore-into-a-scratch-database) steps
   1–2 to fetch and verify, then restore directly into the real database — it is
   empty, so there is nothing to lose:
   ```bash
   docker compose exec -T postgres pg_restore --no-owner -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" < "/tmp/$KEY"
   ```

5. **Start everything and reconcile the schema:**
   ```bash
   docker compose -f deploy/docker-compose.yml up -d
   docker compose exec web-ops python manage.py migrate
   docker compose exec web-ops python manage.py migrate --settings=config.settings.portal
   ```
   `migrate` is expected to say "No migrations to apply" when the dump came from
   the same release. If it applies something, the dump predates the running
   code — which is fine and is why this step is here.

6. **Verify, in this order.** Each line below has failed independently before:
   ```bash
   docker compose exec web-ops python manage.py backup status      # the new box backs up
   curl -sSI https://<domain>/portal/ | head -1                    # TLS and the portal bundle
   curl -sSI https://ops.<domain>/login | head -1                  # the second origin
   ```
   Then sign in to the client portal as a real Org Viewer and open a published
   output. The API answering 200 does not prove the portal renders — those are
   different failures with the same shape.

---

## C. Swap the restored database in

Only after [section A](#a-restore-into-a-scratch-database) and only once you
have looked at the row counts. **This step is not reversible without another
restore.**

```bash
# Stop everything that writes. Leave postgres up.
docker compose stop web-ops web-portal worker-ingest worker-enrich worker-default beat

# Keep the current database rather than dropping it — it is the only copy of
# whatever happened since the dump, and you may need to reconcile against it.
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c \
  "ALTER DATABASE \"$POSTGRES_DB\" RENAME TO ${POSTGRES_DB}_broken_$(date -u +%Y%m%d);"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c \
  "ALTER DATABASE recovery RENAME TO \"$POSTGRES_DB\";"

docker compose up -d
docker compose exec web-ops python manage.py migrate
```

Delete `${POSTGRES_DB}_broken_*` only once the system has run normally for a
day and someone has confirmed the gap is understood.

---

## What a restore does not bring back

Know this before telling anyone the system is recovered.

- **Everything since the last dump.** Backups run at 03:00 UTC, so the worst
  case is almost 24 hours of collection, enrichment and any output approved or
  published in that window. Collection re-polls and recovers on its own;
  **approvals and publications do not** — they were operator actions and have to
  be redone.
- **Object storage.** The dump is Postgres only. Anything in the media bucket is
  covered by that bucket's own versioning, not by this.
- **Redis.** Queued-but-unstarted jobs are lost. Beat will re-dispatch the
  scheduled ones within 15–30 minutes; anything triggered by hand must be
  re-triggered.
- **Vendor credentials, if `CREDENTIALS_ENCRYPTION_KEY` changed.** They are in
  the dump but undecryptable without the original key, and they fail at use
  rather than at startup — the symptom is every connector erroring at once.
  Re-enter them at `https://ops.<domain>/sources/providers/`.

---

## Running the test on purpose

You do not have to wait for Sunday:

```bash
docker compose exec worker-default python manage.py backup now
docker compose exec worker-default python manage.py backup restore-test
```

`restore-test` downloads the newest dump, verifies its checksum, restores it
into a disposable database, asserts it contains organisations, and drops it. It
is the same code the weekly schedule runs — a green result here is the same
evidence.

Run both after any change to the database, the bucket, the credentials, or the
Postgres version. The version in particular: `deploy/Dockerfile` installs
`postgresql-client-16` to match the `pgvector/pgvector:pg16` server, and
`pg_dump` refuses outright to dump a server newer than itself. Upgrading one
without the other breaks backups silently, on a schedule nobody is watching.
