#!/usr/bin/env bash
# Remote half of relay-migration-dryrun.yml. Runs as root on aiosserver-v2 through the relay's vm-run action,
# which exports DRYRUN_REVISION (the exact aiosxaegra commit), DRYRUN_SOURCE_URL and GH_PULL_TOKEN (the job's
# read-only token, used only to fetch that commit).
#
# What it may touch, and nothing else:
#   production   READ ONLY. pg_dump runs inside the production postgres container (aiosxaegra-postgres-1) and
#                streams into pg_restore, which writes a NEW database on that server. The serving checkout, its
#                .env, the aegra container and every other container are never read or changed. The database
#                login is read in memory from the postgres container's environment and is never printed.
#   the copy     relay_migration_dryrun_<timestamp>. Every migration statement runs against it. The probe opens it
#                read-only and refuses any other database.
#   the work     a root-only directory under ROOT: the source export, the built image and the probe output.
#
# Every exit path removes the containers it started, drops the copy (a failed drop is a failed run), removes the
# built image and the work directory. The migrations are the code under test; this program is the trusted harness.
#
# The counts are printed to the step log, and the check verdicts as annotations: counts and check names only, never
# a row, an identifier value, a key or a token. vm-run returns no output channel, so the job summary cannot carry them.
set -euo pipefail
umask 077

for name in DRYRUN_REVISION DRYRUN_SOURCE_URL GH_PULL_TOKEN; do
  if [ -z "${!name:-}" ]; then
    echo "relay-migration-dryrun: $name is empty; nothing was changed." >&2
    exit 64
  fi
done
if [[ ! "$DRYRUN_REVISION" =~ ^[a-f0-9]{40}$ ]]; then
  echo 'relay-migration-dryrun: an exact 40-character commit SHA is required; nothing was changed.' >&2
  exit 64
fi
if [[ ! "$DRYRUN_SOURCE_URL" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo 'relay-migration-dryrun: DRYRUN_SOURCE_URL must be a github.com https repository URL.' >&2
  exit 64
fi

ROOT="${RELAY_DRYRUN_STATE:-/var/snap/docker/common/companion-migration-dryrun}"
PG_CONTAINER=aiosxaegra-postgres-1
PG_VOLUME=aiosxaegra_postgres_data
PG_USER=user
PG_PRODUCTION_DB=aegra
START_REVISION=w20261031_usage_turn_ts
HEAD_REVISION=c20261126_sign_out_everywhere
PRIOR_REVISION=a20261126_receipts_owner
COPY_PATTERN='^relay_migration_dryrun_[0-9]{14}$'
BUILD_LIMIT=2400
DUMP_LIMIT=3600
MIGRATE_LIMIT=1800
PROBE_LIMIT=900

WORK=""
COPY=""
COPY_CREATED=0
IMAGE=""
RUN_NAME=""

note() { echo "relay-migration-dryrun: $*"; }
fail() { echo "::error::relay-migration-dryrun: $*" >&2; exit 1; }

# One psql call against the production server's maintenance database, bounded. Only DROP/CREATE of our own
# copy and read-only catalog queries go through it.
pg() {
  timeout --kill-after=30 120 docker exec "$PG_CONTAINER" \
    psql -X -q -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres "$@"
}

drop_copy() {
  local name="$1"
  if [[ ! "$name" =~ $COPY_PATTERN ]]; then
    return 1
  fi
  pg -c "DROP DATABASE IF EXISTS \"$name\" WITH (FORCE)" >/dev/null
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ -n "$RUN_NAME" ]; then
    docker rm -f "$RUN_NAME" >/dev/null 2>&1
  fi
  if [ "$COPY_CREATED" = 1 ]; then
    if drop_copy "$COPY"; then
      note "dropped the copy"
    else
      echo "::error::relay-migration-dryrun: the copy $COPY was NOT dropped; drop it by hand, the next run also sweeps copies of this name" >&2
      [ "$status" -ne 0 ] || status=1
    fi
  fi
  if [ -n "$IMAGE" ]; then
    docker image rm -f "$IMAGE" >/dev/null 2>&1
  fi
  if [ -n "$WORK" ]; then
    rm -rf -- "$WORK"
  fi
  exit "$status"
}
trap cleanup EXIT

# Read-only precondition: the container that owns production's data is running on the expected volume.
running="$(docker inspect --format '{{.State.Running}}' "$PG_CONTAINER")" \
  || fail "the production postgres container cannot be inspected; nothing was copied"
[ "$running" = true ] || fail "the production postgres container is not running; nothing was copied"
volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$PG_CONTAINER")"
[ "$volume" = "$PG_VOLUME" ] || fail "the postgres data volume is not $PG_VOLUME; nothing was copied"

if [ ! -d "$ROOT" ]; then
  install -d -m 0700 "$ROOT"
fi
WORK="$(mktemp -d "$ROOT/run.XXXXXXXX")"

# Remove what an earlier, killed run could leave behind: our containers, and copies of our name.
stale="$(docker ps -aq --filter name=relay-dryrun- 2>/dev/null || true)"
if [ -n "$stale" ]; then
  # shellcheck disable=SC2086 # one container id per word, by design
  docker rm -f $stale >/dev/null 2>&1 || true
fi
olds="$(pg -tA -c "SELECT datname FROM pg_database WHERE datname ~ '^relay_migration_dryrun_[0-9]{14}\$'")"
while IFS= read -r old; do
  [ -n "$old" ] || continue
  drop_copy "$old" || fail "could not drop a copy left by an earlier run"
  note "dropped a copy left by an earlier run"
done <<< "$olds"

# Fetch the one approved commit with the job's token, through a credential helper that reads it from the
# environment: never argv, never a file. Same mechanism as deploy_remote.sh.
CACHE="$ROOT/source.git"
[ -d "$CACHE" ] || git init --bare --quiet "$CACHE"
export GH_PULL_TOKEN
# shellcheck disable=SC2016 # expanded by git's helper shell, at request time
HELPER='!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=${GH_PULL_TOKEN}"; }; f'
git -c credential.helper= -c "credential.helper=$HELPER" --git-dir="$CACHE" \
  fetch --quiet --no-tags --depth=1 "$DRYRUN_SOURCE_URL" "$DRYRUN_REVISION"
unset GH_PULL_TOKEN HELPER
FETCHED="$(git --git-dir="$CACHE" rev-parse --verify 'FETCH_HEAD^{commit}')"
[ "$FETCHED" = "$DRYRUN_REVISION" ] || fail "the fetched commit does not match the approved revision"
SRC="$WORK/src"
mkdir -p "$SRC"
git --git-dir="$CACHE" archive "$DRYRUN_REVISION" | tar -x -C "$SRC"

# The image is built from the approved commit's full tree with the deploy's own Dockerfile. The chain-import
# migration imports react_agent.link.chain, which ships in that image as a package, so the alembic folder alone
# cannot run it.
IMAGE="relay-migration-dryrun:${DRYRUN_REVISION:0:12}"
note "building the image for the approved commit with the deploy Dockerfile"
timeout --kill-after=60 "$BUILD_LIMIT" docker build --quiet \
  -f "$SRC/deployments/docker/Dockerfile" -t "$IMAGE" "$SRC" >/dev/null

heads="$(timeout --kill-after=30 300 docker run --rm "$IMAGE" alembic heads)" \
  || fail "alembic cannot read the revisions of the approved commit"
if [ "$(printf '%s\n' "$heads" | grep -c '(head)')" != 1 ] \
   || ! printf '%s\n' "$heads" | grep -Fxq "$HEAD_REVISION (head)"; then
  fail "the approved commit's alembic head is not $HEAD_REVISION; this dry run tests the migration set it was written for"
fi

# Read the login from the postgres container's environment, in memory. Build the connection string with the
# password URL-encoded. Nothing here is printed.
PG_PASSWORD="$(docker exec "$PG_CONTAINER" printenv POSTGRES_PASSWORD)" \
  || fail "the database login cannot be read from the postgres container"
[ -n "$PG_PASSWORD" ] || fail "the postgres container has no database login"
ENDPOINT="$(RELAY_DRYRUN_PASSWORD="$PG_PASSWORD" RELAY_DRYRUN_USER="$PG_USER" python3 -I -B -c \
  'import os, urllib.parse as u; print(u.quote(os.environ["RELAY_DRYRUN_USER"], safe="") + ":" + u.quote(os.environ["RELAY_DRYRUN_PASSWORD"], safe="") + "@127.0.0.1:5432/")')"
unset PG_PASSWORD

COPY="relay_migration_dryrun_$(date -u +%Y%m%d%H%M%S)"
[[ "$COPY" =~ $COPY_PATTERN ]] || fail "the copy name is not valid"
pg -c "CREATE DATABASE \"$COPY\"" >/dev/null
COPY_CREATED=1
note "created the copy; copying production into it (production is read only)"

# pg_dump runs in production's container and reads it. pg_restore writes only into the copy.
timeout --kill-after=60 "$DUMP_LIMIT" docker exec "$PG_CONTAINER" \
  pg_dump -U "$PG_USER" -d "$PG_PRODUCTION_DB" -Fc --no-owner --no-acl \
  | timeout --kill-after=60 "$DUMP_LIMIT" docker exec -i "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$COPY" --no-owner --no-acl --exit-on-error
note "the copy is restored"

export RELAY_DRYRUN_DB="$COPY"
export RELAY_DRYRUN_DSN="postgresql://${ENDPOINT}${COPY}"
export DATABASE_URL="postgresql+asyncpg://${ENDPOINT}${COPY}"
export RELAY_DRYRUN_START="$START_REVISION"
export RELAY_DRYRUN_HEAD="$HEAD_REVISION"
unset ENDPOINT

cat > "$WORK/probe.py" <<'PROBE'
"""Counts-only probe for the relay migration dry run. Runs inside the built image, against the COPY only.

python -I -B - <before|after> < probe.py

It reads RELAY_DRYRUN_DSN, RELAY_DRYRUN_DB, RELAY_DRYRUN_START and RELAY_DRYRUN_HEAD from the environment, opens
the database read-only, refuses any database other than RELAY_DRYRUN_DB, and prints one JSON document with counts
and check results. It never prints a row, an identifier value, a key or a token.
"""
import glob
import importlib.util
import json
import os
import sys

import psycopg
from react_agent.link.account_registry import MAX_REGISTRY_DEVICES
from react_agent.link.chain import ChainError


def refuse(code, message):
    print(f"probe: {message}", file=sys.stderr)
    raise SystemExit(code)


PHASE = sys.argv[1] if len(sys.argv) > 1 else ""
if PHASE not in ("before", "after"):
    refuse(2, "the phase must be before or after")
DSN = os.environ["RELAY_DRYRUN_DSN"]
EXPECTED_DB = os.environ["RELAY_DRYRUN_DB"]
START = os.environ["RELAY_DRYRUN_START"]
HEAD = os.environ["RELAY_DRYRUN_HEAD"]


def load_chain_import():
    # The migration's own fold, loaded from the approved commit's file, so the counts use the code under test.
    matches = glob.glob("alembic/versions/*_chain_devices_to_account_rows.py")
    if len(matches) != 1:
        refuse(2, "the chain import migration is missing or ambiguous")
    spec = importlib.util.spec_from_file_location("chain_import_under_test", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


chain_import = load_chain_import()

# Every transaction of this session is read-only from its first statement (the connect option, not a SET).
with psycopg.connect(DSN, options="-c default_transaction_read_only=on") as conn:
    cur = conn.cursor()

    def scalar(sql, params=()):
        return cur.execute(sql, params).fetchone()[0]

    database = scalar("SELECT current_database()")
    if database != EXPECTED_DB:
        refuse(3, "connected to a database that is not the dry-run copy; nothing was read")
    versions = [row[0] for row in cur.execute("SELECT version_num FROM alembic_version").fetchall()]
    expected = START if PHASE == "before" else HEAD
    if versions != [expected]:
        refuse(3, f"the copy is not at the expected {PHASE} revision")

    counts = {
        "account_device_owner_rows": scalar("SELECT count(*) FROM account_device_owner"),
        "account_devices_total": scalar("SELECT count(*) FROM account_devices"),
        "account_devices_active": scalar("SELECT count(*) FROM account_devices WHERE revoked_at IS NULL"),
        "account_devices_revoked": scalar("SELECT count(*) FROM account_devices WHERE revoked_at IS NOT NULL"),
        "account_devices_from_chain_migration": scalar(
            "SELECT count(*) FROM account_devices WHERE source = 'chain-migration'"),
        "link_account_chain_rows": scalar("SELECT count(*) FROM link_account_chain"),
        "link_state_admission_rows": scalar("SELECT count(*) FROM link_state_admission"),
        "phone_push_subscription_total": scalar("SELECT count(*) FROM phone_push_subscription"),
        "phone_push_subscription_account_scope": scalar(
            "SELECT count(*) FROM phone_push_subscription WHERE chain_id = 'account'"),
        "phone_push_subscription_other_scope": scalar(
            "SELECT count(*) FROM phone_push_subscription WHERE chain_id <> 'account'"),
    }

    existing = {}
    for owner, device, revoked_at in cur.execute(
            "SELECT owner_uid, device_id, revoked_at FROM account_devices").fetchall():
        existing[(owner, device)] = revoked_at is not None

    chains_verified = chains_skipped = 0
    chain_active = chain_removed = 0
    active_without_row = active_with_revoked_row = removed_with_active_row = 0
    for owner, chain_json, overlay_json in cur.execute(
            "SELECT owner_uid, chain_json, account_revoked_json FROM link_account_chain").fetchall():
        try:
            folded = chain_import._fold(owner, json.loads(chain_json))
        except (ChainError, ValueError, TypeError, KeyError):
            chains_skipped += 1
            continue
        chains_verified += 1
        try:
            overlay = set(json.loads(overlay_json or "{}"))
        except (TypeError, ValueError):
            overlay = set()
        active = {device for device in folded["devices"] if device not in overlay}
        removed = (set(folded["revoked"]) | overlay) - active
        chain_active += len(active)
        chain_removed += len(removed)
        for device in active:
            if (owner, device) not in existing:
                active_without_row += 1
            elif existing[(owner, device)]:
                active_with_revoked_row += 1
        for device in removed:
            if existing.get((owner, device)) is False:
                removed_with_active_row += 1

    counts.update({
        "link_accounts_verified": chains_verified,
        "link_accounts_unverified_skipped": chains_skipped,
        "chain_devices_active": chain_active,
        "chain_devices_removed": chain_removed,
        "chain_active_devices_without_row": active_without_row,
        "chain_active_devices_with_revoked_row": active_with_revoked_row,
        "chain_removed_devices_with_active_row": removed_with_active_row,
        "accounts_over_device_cap": scalar(
            "SELECT count(*) FROM (SELECT owner_uid FROM account_devices WHERE revoked_at IS NULL "
            "GROUP BY owner_uid HAVING count(*) > %s) AS over", (MAX_REGISTRY_DEVICES,)),
        "max_active_devices_per_account": scalar(
            "SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM account_devices "
            "WHERE revoked_at IS NULL GROUP BY owner_uid) AS per_account"),
        "phone_push_subscriptions_without_account_row": scalar(
            "SELECT count(*) FROM phone_push_subscription p WHERE NOT EXISTS (SELECT 1 FROM account_devices a "
            "WHERE a.owner_uid = p.owner_uid AND a.device_id = p.device_id)"),
        "phone_push_subscriptions_for_revoked_device": scalar(
            "SELECT count(*) FROM phone_push_subscription p JOIN account_devices a "
            "ON a.owner_uid = p.owner_uid AND a.device_id = p.device_id WHERE a.revoked_at IS NOT NULL"),
    })

checks = {}
if PHASE == "after":
    def check(name, count):
        checks[name] = {"pass": count == 0, "count": count}

    check("active_chain_devices_have_an_account_row", counts["chain_active_devices_without_row"])
    check("removed_chain_devices_are_not_active", counts["chain_removed_devices_with_active_row"])
    check("no_account_over_device_cap", counts["accounts_over_device_cap"])
    check("no_orphan_push_subscriptions", counts["phone_push_subscriptions_without_account_row"])
    check("push_subscriptions_scoped_to_account", counts["phone_push_subscription_other_scope"])

print(json.dumps({
    "phase": PHASE,
    "database": EXPECTED_DB,
    "alembic_version": versions[0],
    "counts": counts,
    "checks": checks,
}, sort_keys=True))
PROBE

cat > "$WORK/summary.py" <<'SUMMARY'
"""Counts-only summary of the relay migration dry run. Exit 0 only when every expected check passed, the copy
was at the head, and the second run gave the same counts as the first. Missing, empty or malformed evidence is a
failure, never zero findings."""
import json
import sys

EXPECTED_CHECKS = {
    "active_chain_devices_have_an_account_row",
    "removed_chain_devices_are_not_active",
    "no_account_over_device_cap",
    "no_orphan_push_subscriptions",
    "push_subscriptions_scoped_to_account",
}


def load(path, phase):
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"summary: the {phase} evidence is missing or malformed ({exc.__class__.__name__})")
    if not isinstance(doc, dict) or not isinstance(doc.get("counts"), dict) or not doc["counts"]:
        raise SystemExit(f"summary: the {phase} evidence has no counts")
    if not isinstance(doc.get("checks"), dict):
        raise SystemExit(f"summary: the {phase} evidence has no check list")
    return doc


def say(line):
    # Counts go to the step log as plain lines. Annotations are capped per step, so only the verdict is annotated.
    print(line)


before_path, after_path, again_path, migrate_ms, rerun_ms = sys.argv[1:6]
before = load(before_path, "before")
after = load(after_path, "after")
again = load(again_path, "re-run")
problems = []

say(f"migration to {after.get('alembic_version')}: {int(migrate_ms) / 1000:.1f} s")
say(f"re-run (downgrade one revision set, then upgrade head): {int(rerun_ms) / 1000:.1f} s")
for key in sorted(after["counts"]):
    say(f"  {key}: before={before['counts'].get(key, '-')} after={after['counts'][key]}")

missing = EXPECTED_CHECKS - set(after["checks"])
if missing:
    problems.append("checks not reported: " + ", ".join(sorted(missing)))
for name in sorted(after["checks"]):
    result = after["checks"][name]
    if not isinstance(result, dict) or not isinstance(result.get("pass"), bool):
        problems.append(f"check {name} is malformed")
        continue
    if result["pass"]:
        say(f"  check {name}: PASS")
    else:
        print(f"::notice title=relay-migration-dryrun::check {name}: FAIL ({result.get('count')})")
        problems.append(name)

if after["alembic_version"] != "c20261126_sign_out_everywhere":
    problems.append("the copy did not reach the head")
if again["counts"] != after["counts"]:
    problems.append("the re-run changed the counts")
    say("re-run: counts differ from the first run")
else:
    say("re-run: the same counts as the first run")

if problems:
    print("::error title=relay-migration-dryrun::FAILED: " + "; ".join(problems))
    raise SystemExit(1)
print("::notice title=relay-migration-dryrun::RESULT: PASS (every check passed on the copy; production was read only)")
SUMMARY

note "probe: counts before the migration"
RUN_NAME="relay-dryrun-probe"
timeout --kill-after=30 "$PROBE_LIMIT" docker run --rm -i --name "$RUN_NAME" \
  --network "container:$PG_CONTAINER" \
  -e RELAY_DRYRUN_DSN -e RELAY_DRYRUN_DB -e RELAY_DRYRUN_START -e RELAY_DRYRUN_HEAD \
  "$IMAGE" python -I -B - before < "$WORK/probe.py" > "$WORK/before.json"
RUN_NAME=""

run_alembic() {
  local limit="$1"
  shift
  RUN_NAME="relay-dryrun-alembic"
  timeout --kill-after=60 "$limit" docker run --rm --name "$RUN_NAME" \
    --network "container:$PG_CONTAINER" -e DATABASE_URL "$IMAGE" alembic "$@"
  RUN_NAME=""
}

note "alembic upgrade head, against the copy only"
t0="$(date +%s%3N)"
run_alembic "$MIGRATE_LIMIT" upgrade head
t1="$(date +%s%3N)"
MIGRATE_MS=$((t1 - t0))

note "counts and checks after the migration"
RUN_NAME="relay-dryrun-probe"
timeout --kill-after=30 "$PROBE_LIMIT" docker run --rm -i --name "$RUN_NAME" \
  --network "container:$PG_CONTAINER" \
  -e RELAY_DRYRUN_DSN -e RELAY_DRYRUN_DB -e RELAY_DRYRUN_START -e RELAY_DRYRUN_HEAD \
  "$IMAGE" python -I -B - after < "$WORK/probe.py" > "$WORK/after.json"
RUN_NAME=""

note "re-run: downgrade to $PRIOR_REVISION, then upgrade head again, to prove the rewrite is idempotent"
run_alembic "$MIGRATE_LIMIT" downgrade "$PRIOR_REVISION"
t2="$(date +%s%3N)"
run_alembic "$MIGRATE_LIMIT" upgrade head
t3="$(date +%s%3N)"
RERUN_MS=$((t3 - t2))

RUN_NAME="relay-dryrun-probe"
timeout --kill-after=30 "$PROBE_LIMIT" docker run --rm -i --name "$RUN_NAME" \
  --network "container:$PG_CONTAINER" \
  -e RELAY_DRYRUN_DSN -e RELAY_DRYRUN_DB -e RELAY_DRYRUN_START -e RELAY_DRYRUN_HEAD \
  "$IMAGE" python -I -B - after < "$WORK/probe.py" > "$WORK/after2.json"
RUN_NAME=""

python3 -I -B "$WORK/summary.py" "$WORK/before.json" "$WORK/after.json" "$WORK/after2.json" \
  "$MIGRATE_MS" "$RERUN_MS"
