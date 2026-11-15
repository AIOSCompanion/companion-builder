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

- `release.yml`: release builds for a tag on the private repository. Dispatch only, from this repository's
  default branch, into the `release-signing` environment, which needs an approval.
- `test.yml`: PR tests for a PR head SHA on a private repository. Dispatch only. No `pull_request` trigger.
- Hosted runners only. No self-hosted runner.

## Residuals

- The publish token (PUBLISH_RELEASES_TOKEN) is in the signing-and-build step's environment, because electron-builder
  publishes the bundles with it. That build runs our own tagged code, so it falls under the dependency residual
  below. The token is not in any test step, and the test user cannot read it.

- A dependency that runs code during a release build can read the signing material while the build runs. It
  cannot read the team log key, and its output is sealed.
- The runner's home directory is locked to mode 711 where the image allows, so the test user can traverse it but not
  list it. The probe reports the result either way. The runner's install-directory files (.runner, .credentials,
  .credentials_rsaparams) and the worker process's environment are report-only, not gated: each is reported as
  absent, denied, or READABLE, and a READABLE item prints a warning. The first run decides whether any of them
  becomes a gate. The gpg keyring is protected by gpg's own 0700.
- The first run of each workflow on GitHub has not yet been made. The namespace, the test-user lines, and gpg on
  the macOS and Windows runners are unverified until it has.
