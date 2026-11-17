# Companion Go/Wails app release route

`release-app.yml` builds the new app from reviewed tags in the private
`AIOSCompanion/companion-engine` repository. It is separate from the existing
Electron workflow and updater feed. Every private-source job uses the public
builder and its approved `release-signing` environment. Dispatch is accepted only
from `AIOSCompanion/companion-builder` main.

## Destination configuration

The owner must choose and initialize a **separate public artifact-only repository**
under AIOSCompanion. This workflow does not create it or select a default.
Configure these builder repository variables after that decision:

| Variable | Required value |
| --- | --- |
| `APP_RELEASE_REPOSITORY` | Exact approved `AIOSCompanion/<repository>` name. |
| `APP_RELEASE_REPOSITORY_ID` | Its immutable numeric GitHub repository ID. |

The owner must initialize that repository with its own public documentation
commit. Draft tags point at that public commit; private source commits are never
pushed there. A repository rename, ID mismatch, private/archived/disabled target,
missing configuration or the legacy updater repository blocks the workflow.
The old updater repository is refused by both name and immutable ID.

Reuse the existing reader App; do not create or reinstall another App. Its current
installation must include `companion-engine`, with Contents read access. The
`BUILDER_APP_PRIVATE_KEY` remains an environment secret, never a repository secret.
Every job that mints a source token requires `release-signing` approval. Public
destination metadata must also be readable by the identity job's installation
token; a rejected metadata read blocks before checkout.

`PUBLISH_RELEASES_TOKEN` in `release-signing` must have Contents write on the
selected new artifact repository. Do not replace a credential used by the old
Electron workflow with one that loses its existing required access. Scope the
maintained credential appropriately, or provide an environment configuration that
meets both routes' needs. This change does not alter credentials or settings.

Keep the existing log public-key variables `TEST_LOG_PUBLIC_KEY` and
`TEST_LOG_RECIPIENT`. Signing requires maintained `CSC_LINK` (base64 P12),
`CSC_KEY_PASSWORD`, `APPLE_API_KEY` (P8 text), `APPLE_API_KEY_ID`, and
`APPLE_API_ISSUER` environment secrets. Publisher credentials exist only on draft
creation, upload and finalization steps. Apple secrets exist only on the macOS
sign/notarize/staple step after the build finishes.

## Source and artifact contract

Dispatch takes a version tag `vX.Y.Z` (optionally a prerelease suffix), an optional
expected immutable **tag-object** SHA, and an explicit unsigned macOS test choice.
Annotated tags are peeled to the source commit, and both identities are pinned.
The detached checkout must remain clean and `git describe --tags --always --dirty`
must report exactly that tag. Go is pinned to 1.27.4; Node is 22.23.4.

The reviewed source scripts run from the repository root with **no arguments**:

| Native row | Source build script | Required source output |
| --- | --- | --- |
| Windows amd64 | `scripts/build-release.ps1` | `dist/Companion_<version>_windows_amd64.exe`, `dist/companion-cli_<version>_windows_amd64.exe`, `dist/SHA256SUMS` |
| macOS arm64 | `scripts/build-release.sh` | `dist/Companion_<version>_darwin_arm64.app` |

`<version>` excludes the leading `v`. The CLI filename is deliberately different
beyond letter case: Windows must retain both executables on a case-insensitive
filesystem. The Windows probe verifies the CLI version, a real bare-launch GUI
window, authenticated engine access in a fresh isolated home, engine residency
after window close, and stopping only that home through `down --home`.

The macOS helper verifies bundle and executable versions, signs nested binaries
and the bundle with a Developer ID Application certificate, requires Accepted
notarization, staples/validates the ticket, runs Gatekeeper assessment and then
creates `Companion_<version>_darwin_arm64.zip`. The only unsigned bypass is the
explicit dispatch input: it creates a separately named `_unsigned-test.zip`, marks
the receipt unsigned and creates a clearly labelled prerelease draft. It never
claims a signed result or reads Apple credentials.

Only the two Windows executables, one macOS ZIP, platform-qualified checksum
files and platform receipts can reach the draft: seven assets total. Receipts
bind the source commit, tag, platform, signing mode and every downloadable hash.
Existing assets with different bytes are refused without overwrite. Finalization
requires both complete platform sets and GitHub's matching SHA256 asset digests.
All releases remain **unpublished drafts** for owner review/publication. Resuming
a draft with a different source or signing mode is refused.
Keep the draft unpublished until finalization completes. Each upload rechecks
draft state, but GitHub's upload endpoint has no atomic draft-only condition;
publishing concurrently with a running upload creates a race.

## Trust and validation limits

Private git output, dependency output, build output and signing/API failures run
through the sealed-log wrapper. Actions artifacts contain only encrypted `.gpg`
logs, retained for one day. No build cache or raw private log is uploaded.

Trusted public scripts are copied before private checkout, protected with numeric
root ownership on Linux/macOS or a SYSTEM-owner DACL on Windows, and hash-checked
before upload. Real write-denial probes and known-writable/mutated negative controls
run on all three hosted operating systems in the public self-check.

This is a trusted-tagged-code release lane. These protections catch ordinary
writes and changed snapshot content; they do not isolate malicious code running
as the release user. An elevated dependency can use sudo/take ownership, and the
release user controls its temporary parent directory and later job process state.
Step-scoped secrets reduce exposure but do not establish a separate security
boundary against persistence by malicious tagged code. The existing unprivileged,
network-isolated private test lane is unchanged.

Synthetic checks prove the public builder contracts. They do not prove the real UI
build, packaged GUI, Apple credentials, notarization or delivery to a configured
repository. Missing real UI/build scripts, unavailable signing credentials, an
unconfigured target or failed native checks stop the release. After these gates
are available, an owner-reviewed main dispatch must prove both native rows and
the final draft before relying on this route.
