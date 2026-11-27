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
#   the copy's   SELECTED, not the whole database. The schema of the whole database is copied (pg_dump -s). Data is
#   contents     copied only for the tables the approved commit's migrations read or write (derived from its
#                alembic files, see derive.py), plus the verifier's reads and alembic_version. Tables the migrations
#                only ALTER are copied as schema only, and their production row count and size are reported.
#   the work     a root-only directory under ROOT: the source export, the built image and the probe output.
#   disk         refuses before anything is copied or built unless the data volume has at least
#                2 x the selected data size + 3 GiB free (measured in the container and on the VM), the Docker root
#                (and /var/lib/docker, where it exists) has at least 6 GiB free, and the selection is at most
#                SELECTED_DATA_CEILING. It measures again after the copy is created and before the restore, and
#                drops the copy if the room has gone.
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
# Time bounds, in seconds. The workflow's job timeout must cover their worst-case sum (the test checks it):
# FETCH once; PROD six times (three inventory reads and up to three row counts); HEADS once; BUILD once; SCHEMA once;
# DATA once; MIGRATE three times (the migration, and the re-run's downgrade and upgrade); PROBE three times;
# PG four times (the sweep, the create, the drop and a spare).
FETCH_LIMIT=600
PROD_LIMIT=300
HEADS_LIMIT=300
PG_LIMIT=300
BUILD_LIMIT=1200
SCHEMA_LIMIT=900
DATA_LIMIT=3600
MIGRATE_LIMIT=900
PROBE_LIMIT=600
PG_DATA_DIR=/var/lib/postgresql/data
GIB=$((1024 * 1024 * 1024))
DISK_HEADROOM=$((3 * GIB))
DOCKER_MIN_FREE=$((6 * GIB))
# A selection above this is refused before anything is copied. At the throughput the full 73 GiB copy showed (it
# ran past one hour), 20 GiB is well inside DATA_LIMIT.
SELECTED_DATA_CEILING=$((20 * GIB))

WORK=""
COPY=""
COPY_CREATED=0
IMAGE=""
RUN_NAME=""

note() { echo "relay-migration-dryrun: $*"; }
fail() { echo "::error::relay-migration-dryrun: $*" >&2; exit 1; }

# Bytes as GiB with two decimals, for the messages. Sizes only, never contents.
gib() { printf '%d.%02d' $(( $1 / GIB )) $(( ($1 % GIB) * 100 / GIB )); }

# Run a measuring command and accept only a plain non-negative integer from its last line (df prints a header
# first). Anything else (an error, an empty answer, text) is a failed measurement, never a zero: the caller refuses.
read_int() {
  local out
  out="$("$@")" || return 1
  out="$(printf '%s\n' "$out" | tail -n 1 | tr -d '[:space:]')"
  [[ "$out" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$out"
}

# Milliseconds since the epoch, and a phase line with its elapsed seconds (tenths).
now_ms() { date +%s%3N; }
phase() {
  local ms=$(( $(now_ms) - $2 ))
  note "phase $1: $(( ms / 1000 )).$(( (ms % 1000) / 100 )) s"
}

# Every production read goes through here: one psql session, read-only from its first statement, against the
# production database. Callers pass only SELECTs (and SET statement_timeout) after it.
prod_psql() {
  timeout --kill-after=30 "$PROD_LIMIT" docker exec "$PG_CONTAINER" \
    psql -X -q -v ON_ERROR_STOP=1 -U "$PG_USER" -d "$PG_PRODUCTION_DB" \
    -c "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" "$@"
}

# Disk guard. The copy holds the selected data again on the data volume (pg_restore writes a second copy on the
# same server), and the image build writes under the Docker root. Refuse unless:
#   free on the data volume, measured inside the postgres container AND on the VM, >= 2 x selected data + 3 GiB;
#   free under the Docker root >= 6 GiB (and under /var/lib/docker too, where that directory exists).
# SELECTED_BYTES is the total size of the selected data tables. $1 is the stage, $2 what a refusal leaves behind.
# Prints sizes only.
check_disk() {
  local stage="$1" outcome="$2"
  local db_bytes="$SELECTED_BYTES" container_free host_free volume_mount docker_root docker_root_free lib_free=""
  local required problems=""

  container_free="$(read_int docker exec "$PG_CONTAINER" df -B1 --output=avail "$PG_DATA_DIR")" \
    || fail "could not read free space on the postgres data directory inside the container ($stage); $outcome"
  volume_mount="$(docker volume inspect --format '{{.Mountpoint}}' "$PG_VOLUME")" || volume_mount=""
  [[ "$volume_mount" == /* ]] || fail "could not read the data volume's mount point on the VM ($stage); $outcome"
  host_free="$(read_int df -B1 --output=avail "$volume_mount")" \
    || fail "could not read free space on the data volume on the VM ($stage); $outcome"
  docker_root="$(docker info --format '{{.DockerRootDir}}')" \
    || fail "could not read the Docker root directory ($stage); $outcome"
  [[ "$docker_root" == /* ]] || fail "the Docker root directory is not an absolute path ($stage); $outcome"
  docker_root_free="$(read_int df -B1 --output=avail "$docker_root")" \
    || fail "could not read free space under the Docker root ($stage); $outcome"
  if [ -d /var/lib/docker ]; then
    lib_free="$(read_int df -B1 --output=avail /var/lib/docker)" \
      || fail "could not read free space under /var/lib/docker ($stage); $outcome"
  fi

  required=$(( 2 * db_bytes + DISK_HEADROOM ))
  local summary="selected data ${db_bytes} bytes ($(gib "$db_bytes") GiB); data volume needs $(gib "$required") GiB free (2 x selected data + 3 GiB); free on the data volume: in the container $(gib "$container_free") GiB, on the VM $(gib "$host_free") GiB; free under the Docker root $(gib "$docker_root_free") GiB"
  if [ -n "$lib_free" ]; then
    summary="$summary, under /var/lib/docker $(gib "$lib_free") GiB"
  fi
  note "disk check ($stage): $summary"

  if (( container_free < required )); then
    problems="$problems data volume (in the container) below $(gib "$required") GiB;"
  fi
  if (( host_free < required )); then
    problems="$problems data volume (on the VM) below $(gib "$required") GiB;"
  fi
  if (( docker_root_free < DOCKER_MIN_FREE )); then
    problems="$problems Docker root below 6 GiB;"
  fi
  if [ -n "$lib_free" ] && (( lib_free < DOCKER_MIN_FREE )); then
    problems="$problems /var/lib/docker below 6 GiB;"
  fi
  if [ -n "$problems" ]; then
    fail "refusing ($stage):$problems $summary; $outcome"
  fi
}

# One psql call against the production server's maintenance database, bounded. Only DROP/CREATE of our own
# copy and read-only catalog queries go through it.
pg() {
  timeout --kill-after=30 "$PG_LIMIT" docker exec "$PG_CONTAINER" \
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
    local drop_started
    drop_started="$(now_ms)"
    if drop_copy "$COPY"; then
      note "dropped the copy"
      phase "drop" "$drop_started"
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

# umask 077 (set above) makes this directory 0700 on the VM. The work directory, with the cache inside it, is removed
# on every exit, so a refusal below leaves nothing behind.
mkdir -p "$ROOT"
WORK="$(mktemp -d "$ROOT/run.XXXXXXXX")"

# Fetch the one approved commit with the job's token, through a credential helper that reads it from the
# environment: never argv, never a file. Same mechanism as deploy_remote.sh, into a cache of this run only.
T_FETCH="$(now_ms)"
CACHE="$WORK/source.git"
git init --bare --quiet "$CACHE"
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
phase "fetch" "$T_FETCH"

# Production, read only: the server version, the whole database's size (information only), and every table's size.
T_INV="$(now_ms)"
SERVER_VERSION="$(prod_psql -tA -c "SELECT current_setting('server_version') || ' (' || current_setting('server_version_num') || ')'")" \
  || fail "could not read the production server version; nothing was copied"
PROD_BYTES="$(read_int prod_psql -tA -c "SELECT pg_database_size(current_database())")" \
  || fail "could not read the production database size; nothing was copied"
prod_psql -tA -F "$(printf '\t')" -c "SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' ORDER BY 1" \
  > "$WORK/inventory.tsv" || fail "could not read the production table list; nothing was copied"
phase "production inventory" "$T_INV"
note "production: PostgreSQL $SERVER_VERSION; the whole database is $(gib "$PROD_BYTES") GiB (information only; the copy is selected)"

# The selection comes from the approved commit's own migration files: derive.py reads the revisions from the start
# revision (exclusive) to the head, and classifies each production table they name.
cat > "$WORK/derive.py" <<'DERIVE'
"""Derive the dry run's table selection from the approved commit's migration files.

python -I -B - <src> <start-revision> <head-revision> <inventory.tsv> <out-dir> < derive.py

The chain of revisions runs from <start-revision> (exclusive) to <head-revision> (inclusive). Every production table
the chain names is classified by the statement that names it:
  data         read or written by a statement (FROM, JOIN, INTO, UPDATE, DELETE FROM, REFERENCES, TRUNCATE,
               LOCK TABLE, VACUUM, ANALYZE, to_regclass);
  schema only  altered or created by DDL only (ALTER TABLE, DROP or CREATE TABLE, CREATE INDEX ... ON);
  prose        named only in a comment or docstring: neither copied nor counted, but listed.
The verifier's reads and alembic_version are always data. A table an ALTER or CREATE INDEX names that production
does not have refuses the selection, and so does a table name the chain builds at run time: a name that cannot be
read cannot be classified. Writes data-tables.txt, schema-tables.tsv (name, bytes, planner estimate),
selected-bytes.txt and plan.json into out-dir. Prints table names and sizes only.
"""
import json
import re
import sys
from pathlib import Path

SRC, START, HEAD, INVENTORY, OUT = sys.argv[1:6]
NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
REVISION = re.compile(r'^revision\s*=\s*"([^"]+)"', re.M)
DOWN = re.compile(r'^down_revision\s*=\s*(?:"([^"]+)"|None)', re.M)
ALWAYS_DATA = {"alembic_version"}
VERIFIER_READS = {"account_device_owner", "account_devices", "link_account_chain",
                  "link_state_admission", "phone_push_subscription"}
MAX_SCHEMA_ONLY = 3
DYNAMIC = re.compile(r"(?:alter\s+table|\bfrom|\bjoin|\binto|\bupdate|\breferences|\bon|\btable)\s*\{", re.I)
ALTER_TARGET = re.compile(r'\balter\s+table\s+"?([A-Za-z_][A-Za-z0-9_]*)', re.I)
INDEX_TARGET = re.compile(r'\bcreate\s+(?:unique\s+)?index\b[^;"]*?\bon\s+"?([A-Za-z_][A-Za-z0-9_]*)', re.I)


def fail(message):
    print(f"derive: {message}", file=sys.stderr)
    raise SystemExit(1)


tables = {}
for raw in Path(INVENTORY).read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    parts = raw.split("\t")
    if len(parts) != 3:
        fail("the production table list is malformed")
    name, size, estimate = parts
    if not NAME.match(name):
        fail("a production table name cannot be quoted safely; nothing was selected")
    if not size.isdigit() or not re.fullmatch(r"-?[0-9]+", estimate):
        fail("the production table list has a non-numeric size")
    tables[name] = {"bytes": int(size), "estimate": int(estimate)}

missing = (VERIFIER_READS | ALWAYS_DATA) - set(tables)
if missing:
    fail("production has no table " + ", ".join(sorted(missing)) + "; the verifier cannot run")

files = {}
for path in sorted(Path(SRC, "alembic", "versions").glob("*.py")):
    text = path.read_text(encoding="utf-8")
    revision = REVISION.search(text)
    down = DOWN.search(text)
    if revision and down:
        files[revision.group(1)] = {"name": path.name, "text": text, "down": down.group(1)}

chain = []
current = HEAD
while current != START:
    if current is None or current not in files or len(chain) > len(files):
        fail(f"the head does not descend from {START}")
    chain.append(current)
    current = files[current]["down"]
chain.reverse()
if not chain:
    fail("there are no revisions between the start and the head")

for revision in chain:
    if DYNAMIC.search(files[revision]["text"]):
        fail(f"{files[revision]['name']} builds a table name at run time; the selection cannot classify it")

data_names, schema_names, prose_names = set(), set(), set()
for revision in chain:
    text = files[revision]["text"]
    for target in ALTER_TARGET.findall(text) + INDEX_TARGET.findall(text):
        if target not in tables:
            fail(f"{files[revision]['name']} alters a table production does not have")
    for name in tables:
        if not re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", text):
            continue
        quoted = rf'"?{re.escape(name)}"?(?![a-z0-9_])'
        read = (re.search(rf"\b(?:from|join|into|update|references|truncate|lock\s+table|vacuum|analyze)\s+(?:only\s+)?{quoted}", text, re.I)
                or re.search(rf"to_regclass\(\s*'{re.escape(name)}'\s*\)", text))
        ddl = (re.search(rf"\b(?:alter\s+table|drop\s+table|create\s+table)\s+(?:if\s+(?:not\s+)?exists\s+)?{quoted}", text, re.I)
               or re.search(rf'\bcreate\s+(?:unique\s+)?index\b[^;"]*?\bon\s+{quoted}', text, re.I))
        if read:
            data_names.add(name)
        elif ddl:
            schema_names.add(name)
        else:
            prose_names.add(name)

data = (data_names | VERIFIER_READS | ALWAYS_DATA) & set(tables)
schema_only = schema_names - data
if len(schema_only) > MAX_SCHEMA_ONLY:
    fail(f"{len(schema_only)} tables are schema only; the row-count budget covers {MAX_SCHEMA_ONLY}")

data_sorted = sorted(data)
schema_sorted = sorted(schema_only)
selected_bytes = sum(tables[name]["bytes"] for name in data_sorted)
out = Path(OUT)
LF = {"encoding": "utf-8", "newline": "\n"}  # bash reads these files: LF endings on every platform
(out / "data-tables.txt").write_text("".join(f"{name}\n" for name in data_sorted), **LF)
(out / "schema-tables.tsv").write_text(
    "".join(f"{name}\t{tables[name]['bytes']}\t{tables[name]['estimate']}\n" for name in schema_sorted), **LF)
(out / "selected-bytes.txt").write_text(str(selected_bytes), **LF)
(out / "plan.json").write_text(json.dumps({
    "revisions": [files[revision]["name"] for revision in chain],
    "data": data_sorted,
    "schema_only": schema_sorted,
    "prose_only_not_copied": sorted(prose_names - data - schema_only),
    "selected_bytes": selected_bytes,
}, sort_keys=True), **LF)

print("revision files: " + ", ".join(files[revision]["name"] for revision in chain))
print(f"data tables ({len(data_sorted)}): " + ", ".join(data_sorted))
print("schema-only tables: " + (", ".join(schema_sorted) or "none"))
prose = sorted(prose_names - data - schema_only)
if prose:
    print("named only in comments or docstrings, not copied: " + ", ".join(prose))
DERIVE
python3 -I -B "$WORK/derive.py" "$SRC" "$START_REVISION" "$HEAD_REVISION" "$WORK/inventory.tsv" "$WORK" \
  || fail "the table selection could not be derived from the approved commit; nothing was copied"
mapfile -t DATA_TABLES < "$WORK/data-tables.txt"
SELECTED_BYTES="$(read_int cat "$WORK/selected-bytes.txt")" || fail "the selected size could not be read; nothing was copied"
(( SELECTED_BYTES <= SELECTED_DATA_CEILING )) \
  || fail "refusing: the selected data is $(gib "$SELECTED_BYTES") GiB, above the $(gib "$SELECTED_DATA_CEILING") GiB ceiling; nothing was copied or built"
note "selected data: ${#DATA_TABLES[@]} tables, $(gib "$SELECTED_BYTES") GiB including indexes"

# Disk guard, before anything is copied or built.
check_disk "before anything is copied or built" "nothing was copied or built"

# Schema-only tables: production's row count and total size, read-only. A count that cannot be read in time says so;
# it is never reported as zero.
note "schema-only tables (altered by the migrations, not read or written): production rows and total size"
while IFS=$'\t' read -r table bytes estimate; do
  rows="$(read_int prod_psql -tA -c "SET statement_timeout = '240s'" -c "SELECT count(*) FROM public.\"$table\"" < /dev/null)" \
    || rows="not read (over 240 s or failed; planner estimate $estimate)"
  note "  $table: production rows $rows; total size $(gib "$bytes") GiB"
done < "$WORK/schema-tables.tsv"

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

# The image is built from the approved commit's full tree with the deploy's own Dockerfile. The chain-import
# migration imports react_agent.link.chain, which ships in that image as a package, so the alembic folder alone
# cannot run it.
IMAGE="relay-migration-dryrun:${DRYRUN_REVISION:0:12}"
note "building the image for the approved commit with the deploy Dockerfile"
timeout --kill-after=60 "$BUILD_LIMIT" docker build --quiet \
  -f "$SRC/deployments/docker/Dockerfile" -t "$IMAGE" "$SRC" >/dev/null

heads="$(timeout --kill-after=30 "$HEADS_LIMIT" docker run --rm "$IMAGE" alembic heads)" \
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
note "created the copy; checking free space again before the restore"

# The build has run since the first check. A shortfall now drops the copy (cleanup does it on this exit).
check_disk "before the restore" "the copy is dropped"

# The schema of the whole database, with no rows: every ALTER and foreign key the migrations touch is the real DDL.
# pg_dump runs in production's container and reads it; pg_restore writes only into the copy.
T_SCHEMA="$(now_ms)"
timeout --kill-after=60 "$SCHEMA_LIMIT" docker exec "$PG_CONTAINER" \
  pg_dump -U "$PG_USER" -d "$PG_PRODUCTION_DB" -s -Fc --no-owner --no-acl \
  | timeout --kill-after=60 "$SCHEMA_LIMIT" docker exec -i "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$COPY" --no-owner --no-acl --exit-on-error
phase "schema copy (pg_dump -s streamed into pg_restore)" "$T_SCHEMA"

# Rows for the selected tables only. --disable-triggers loads them without firing or checking foreign keys on the
# copy. The copy's rows are production's rows as they were when the snapshot was taken, so they were valid then.
TABLE_ARGS=()
for table in "${DATA_TABLES[@]}"; do
  TABLE_ARGS+=(-t "public.$table")
done
T_DATA="$(now_ms)"
timeout --kill-after=60 "$DATA_LIMIT" docker exec "$PG_CONTAINER" \
  pg_dump -U "$PG_USER" -d "$PG_PRODUCTION_DB" -a -Fc --no-owner --no-acl "${TABLE_ARGS[@]}" \
  | timeout --kill-after=60 "$DATA_LIMIT" docker exec -i "$PG_CONTAINER" \
  pg_restore -U "$PG_USER" -d "$COPY" -a --disable-triggers --no-owner --no-acl --exit-on-error
phase "data copy (pg_dump of the selected tables streamed into pg_restore)" "$T_DATA"

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
phase "migration (alembic upgrade head)" "$t0"

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
phase "re-run (downgrade to $PRIOR_REVISION, then upgrade head)" "$t2"

RUN_NAME="relay-dryrun-probe"
timeout --kill-after=30 "$PROBE_LIMIT" docker run --rm -i --name "$RUN_NAME" \
  --network "container:$PG_CONTAINER" \
  -e RELAY_DRYRUN_DSN -e RELAY_DRYRUN_DB -e RELAY_DRYRUN_START -e RELAY_DRYRUN_HEAD \
  "$IMAGE" python -I -B - after < "$WORK/probe.py" > "$WORK/after2.json"
RUN_NAME=""

python3 -I -B "$WORK/summary.py" "$WORK/before.json" "$WORK/after.json" "$WORK/after2.json" \
  "$MIGRATE_MS" "$RERUN_MS"
