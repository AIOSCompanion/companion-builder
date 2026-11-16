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
python scripts/builder.py doctor --mode release
python scripts/builder.py release v1.2.3
python scripts/builder.py self-check
```

`doctor` reports configuration names only. Missing configuration prevents the CLI from dispatching a
private-source test or release. It does not certify that a first hosted run or signing verification passed.
The release-signing environment permits the builder's **main branch**; the private version tag is a
workflow input. Sidecar pins are read from that selected private release, not copied from this builder.

- `self-check.yml`: tests this public builder only, with no private-source credentials or signing material.
- `bridge/`: authenticated GitHub App event receiver for private pushes, same-repo PR heads
  and new desktop version tags. Deployment and live delivery require the configuration in
  [bridge/README.md](bridge/README.md); until then, dispatch remains manual.
- `bridge-deploy.yml`: deploys that receiver from this public repository, behind its own
  protected environment. It never enables acceptance before the Worker secrets exist.

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
  absent, denied, or READABLE. A READABLE item blocks private-source work. Runtime protection removes other-user
  access before the probe; undiscoverable runner processes also block the suite. The gpg keyring is protected by 0700.
- The builder self-check has passed on hosted Ubuntu, macOS and Windows, including calibrated
  isolation and real encryption/decryption using disposable keys. Private checkout, real product tests,
  signing/notarisation, installer checks and release publication remain separate acceptance gates.
  Do not treat synthetic checks as proof those integrations are operational.
