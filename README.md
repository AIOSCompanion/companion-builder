# companion-builder

This repository holds build and test workflows and the scripts they run. It holds no Companion code. The
Companion code is in private repositories, which these workflows check out at a pinned commit.

## Threat model

Decided by the owner (15 Nov 2026).

- The goal: nothing private becomes public by accident, and untrusted code never gets signing or publish
  secrets.
- Collaborators who can open a PR already read the private code, so the PR's author is not an outsider.
- A compromised dependency can exfiltrate source on any CI, private CI included. That is an accepted residual,
  not something these workflows try to prevent.

## Trust boundary

- Trusted: the scripts in `scripts/` and the workflow files, as they are on this repository's default branch.
  Before any PR code or dependency install runs, the workflow copies the scripts outside the workspace, records
  their SHA-256, makes them read-only and root-owned, and verifies them again before the suite and before any
  upload.
- Untrusted: the PR's code, its install, and its tests. They run as a separate unprivileged user with a clean
  environment. In the test workflow they run inside a network namespace with loopback only, and the egress probe
  checks that before the suite runs.
- Logs are sealed to the team key. The public log shows the step name, the exit code, the duration, the junit
  counts and the error class (from a fixed list of standard Python exceptions; anything else prints as "other").
- Signing material (the Developer ID certificate and the App Store Connect key) exists only inside the single
  step that signs, notarises and builds, and is deleted when that step ends.

## Workflows

Use this public builder for Companion GitHub CI and release builds from now on. Do not dispatch
private-repository test/release workflows or add private tag-triggered builds as a fallback.
The builder checks out private source without making the source repository public.

From a checkout of this repository:

```sh
python scripts/builder.py doctor --mode test
python scripts/builder.py test aiosxaegra <full-40-character-commit-sha>
python scripts/builder.py test aiosxaegra <engine-sha> --memory-sha <memory-sha>
python scripts/builder.py test companion-memory <memory-sha> --engine-sha <engine-sha>
python scripts/builder.py test aios-desktop-app <desktop-sha> --engine-sha <engine-sha>
python scripts/builder.py test aiosxaegra <engine-sha> --memory-sha <memory-sha> --diagnostic
python scripts/builder.py doctor --mode release
python scripts/builder.py release v1.2.3
python scripts/builder.py doctor --mode release-app
python scripts/builder.py release-app v0.19.0
python scripts/builder.py self-check
```

`doctor` reports configuration names only. Missing configuration prevents the CLI from dispatching a
private-source test or release. It does not certify that a first hosted run or signing verification passed.
The release-signing environment permits the builder's **main branch**; the private version tag is a
workflow input. Sidecar pins are read from that selected private release, not copied from this builder.

Engine and memory suites check out their companion source at an immutable commit inside the approved
source-access job. Omitted peer pins resolve to main once, before any untrusted code runs. Memory tests
also need their historical release commits and the engine sibling. Both suites use the engine's locked
runtime, with memory installed against those constraints, matching the shared in-process host; memory's
standalone lock can pin a different native database version. Tests never resolve packages inside the
network-isolated suite.

Engine shards group every test function and all its parameter cases together.
The partition key excludes parameter IDs: a UUID generated during collection
must not send the same logical case to different workers or leave it untested.

Memory's 116,000-item store measurements run in a separate job on a fresh runner,
as their wall-clock contract requires. The ordinary memory suite excludes only
that benchmark file; the separate job runs the entire file with the same latency
limits. No cases are skipped or retried to hide a timing failure.

Desktop runtime tests run the app's normal dependency installation, including Electron/native modules
and its canonical archive preparation. The separate typecheck lane avoids those scripts. Desktop tests
also receive only the pinned engine registry file needed by model contracts; full cross-repository
live-wire acceptance remains a separate lane requiring its complete runtime.

Suite deadlines run inside the encryption wrapper, so a timed-out test still produces a complete
encrypted log. Installation is limited to 15 minutes; normal suites to 55 minutes, leaving cleanup
time before the job limit. `--diagnostic` uses a ten-minute suite deadline. The dedicated test user's
remaining helpers are reaped when its suite exits, including detached processes holding stdout open.
Timeouts remain failures; diagnostic runs do not bypass the test or release gates.

- `self-check.yml`: tests this public builder only, with no private-source credentials or signing material.
- `bridge/`: authenticated GitHub App event receiver for private pushes, same-repo PR heads
  and new desktop version tags. Deployment and live delivery require the configuration in
  [bridge/README.md](bridge/README.md); until then, dispatch remains manual.
- `bridge-deploy.yml`: deploys that receiver from this public repository, behind its own
  protected environment. It installs the receiver on an existing SSH host with a persistent SQLite outbox.

- `release.yml`: release builds for a tag on the private repository. Dispatch only, from this repository's
  default branch, into the `release-signing` environment, which needs an approval.
- `release-app.yml`: the separate Go/Wails app route for reviewed `companion-engine` tags. It requires
  a separate public artifact repository, named by the one builder variable `APP_RELEASE_REPOSITORY`
  (never today's `aios-companion-releases`). Builds run without signing/publish
  credentials; publication steps upload only allowlisted executables, ZIPs, checksums and receipts.
  Both native rows must pass before final draft verification. It always leaves an unpublished draft.
  See [the new app release contract](docs/APP_RELEASE.md) for configuration and outstanding acceptance gates.
- `relay-deploy.yml`: deploys one exact `aiosxaegra` commit to the relay VM, with the relay's own `vm-run`
  action and `deploy_remote.sh` from that commit, then checks `/health`. Dispatch only, from main. It needs
  the `source-access` approval (read token) and the `relay-deploy` approval (deploy identity). It delivers no
  dotenv: the VM keeps its serving `.env`. Changes to the production dotenv still go through the relay's own
  `deploy.yml`.
- `relay-migration-dryrun.yml`: dry-runs one exact `aiosxaegra` commit's alembic migrations on a selective copy of
  the production database. The VM copies the whole schema, and the data of only the tables that commit's migrations
  read or write, into a new `relay_migration_dryrun_<timestamp>` database (`pg_dump` reads production, `pg_restore`
  writes only the copy). It runs `alembic upgrade head` from that commit's image against the copy, re-runs the data
  rewrite, checks the account rows, and drops the copy on every exit path. Dispatch only, from main, behind the
  `relay-deploy` approval. It writes nothing to production.
- `portal-deploy.yml`: builds, tests, gates and deploys one exact `AIOS_Commercial` commit with the steps of its
  `deploy-portal.yml`. `mode: preview` publishes a preview channel; `mode: live` publishes
  `app.aioscompanion.com`. Dispatch only, from main. Both modes need the `source-access` and `portal-deploy`
  approvals.
- `test.yml`: PR tests for a PR head SHA on a private repository. Dispatch only. No `pull_request` trigger.
- `catch-up.yml`: expands a truncated push into individual test dispatches, after
  `source-access` owner approval. The external bridge cannot read private commit history.
- All private-source App token jobs require `source-access` or `release-signing` approval.
  The private-source App key is environment-only. The deployed bridge uses its own
  builder-only dispatch App; it does not hold the reader App key.
- Hosted runners only. No self-hosted runner.

## Residuals

- In the existing Electron `release.yml`, the publish token (PUBLISH_RELEASES_TOKEN) is in the signing-and-build step's environment, because electron-builder
  publishes the bundles with it. That build runs our own tagged code, so it falls under the dependency residual
  below. The token is not in any test step, and the test user cannot read it.

- In that existing Electron route, a dependency that runs code during a release build can read the signing material while the build runs. It
  cannot read the team log key, and its output is sealed.
- The runner's home directory is locked to mode 711 where the image allows, so the test user can traverse it but not
  list it. The probe reports the result either way. The runner's install-directory files (.runner, .credentials,
  .credentials_rsaparams) and the worker process's environment are report-only, not gated: each is reported as
  absent, denied, or READABLE. A READABLE item blocks private-source work. Runtime protection removes other-user
  access before the probe; undiscoverable runner processes also block the suite. The gpg keyring is protected by 0700.
- The builder self-check has passed on hosted Ubuntu, macOS and Windows, including calibrated
  isolation and real encryption/decryption using disposable keys. Private checkout, real product tests,
  signing/notarisation, installer checks and release publication remain separate acceptance gates.
  Do not treat synthetic checks as proof those integrations are operational.
