# Companion Go/Wails app release route

`release-app.yml` builds the new app from reviewed tags in the private
`AIOSCompanion/companion-engine` repository. It is separate from the existing
Electron workflow and updater feed. Every private-source job uses the public
builder and its approved `release-signing` environment. Dispatch is accepted only
from `AIOSCompanion/companion-builder` main.

## Destination configuration

Users cannot download from the private source repository, and today's app's
updater reads `AIOSCompanion/aios-companion-releases`. The new app therefore goes
to a **separate public artifact-only repository** that the owner chooses and
initializes. The workflow does not create it and has no default. One builder
repository variable names it:

| Variable | Required value |
| --- | --- |
| `APP_RELEASE_REPOSITORY` | Exact `AIOSCompanion/<repository>` name, for example `AIOSCompanion/companion-app-releases`. |

Changing the name later is that one setting. The identity job checks it as
configuration only, before any private checkout; its App token is scoped to
`companion-engine` and is never used on the destination. Every publishing step
then reads GitHub's metadata with `PUBLISH_RELEASES_TOKEN` and refuses a
destination that is private, archived, disabled, outside AIOSCompanion, renamed
relative to the variable, not writable by the credential, or one of today's
repositories. Today's updater feed, the builder and the Companion source
repositories are refused by name (in any letter case) and by immutable ID.

The owner initializes the repository with its own public documentation commit
(a README). A draft's tag is created on that commit when the owner publishes;
private source commits are never pushed there.

Reuse the existing reader App; do not create or reinstall another App. Its
installation must include `companion-engine`, with Contents read access. The
`BUILDER_APP_PRIVATE_KEY` remains an environment secret, never a repository secret.
Every job that mints a source token requires `release-signing` approval.

`PUBLISH_RELEASES_TOKEN` in `release-signing` must have Contents write on the
selected new artifact repository. Do not replace a credential used by the old
Electron workflow with one that loses its existing required access. This change
does not alter credentials or settings.

Keep the existing log public-key variables `TEST_LOG_PUBLIC_KEY` and
`TEST_LOG_RECIPIENT`. Signing requires maintained `CSC_LINK` (base64 P12; line
breaks are accepted), `CSC_KEY_PASSWORD`, `APPLE_API_KEY` (P8 text),
`APPLE_API_KEY_ID` and `APPLE_API_ISSUER` environment secrets. Publisher
credentials exist only on draft creation, upload and finalization steps. Apple
secrets exist only on the macOS sign/notarize/staple step after the build
finishes, and no tool receives them in its environment.

## Source and artifact contract

Dispatch takes a version tag `vX.Y.Z` (optionally a prerelease suffix), an optional
expected immutable **tag-object** SHA, and an explicit unsigned macOS test choice.
Annotated tags are peeled to the source commit, and both identities are pinned.
The detached checkout must remain clean and `git describe --tags --always --dirty`
must report exactly that tag.

Go is installed from the pinned source's `go.mod` (its toolchain line, else its go
line, as a complete version) and builds with `GOTOOLCHAIN=local`. Node comes from
`ui/package.json` engines: an exact version is installed exactly; a `>=` floor
installs that floor's major line at its newest patch, never a newer major. The
preflight checks the installed Go and Node against the checkout before building.

The reviewed source scripts run from the repository root with **no arguments**:

| Native row | Source build script | Required source output |
| --- | --- | --- |
| Windows amd64 | `scripts/build-release.ps1` | `dist/Companion_<version>_windows_amd64.exe`, `dist/companion-cli_<version>_windows_amd64.exe`, `dist/SHA256SUMS` |
| macOS arm64 | `scripts/build-release.sh` | `dist/Companion_<version>_darwin_arm64.app` |

`<version>` excludes the leading `v`. The CLI filename is deliberately different
beyond letter case: Windows must retain both executables on a case-insensitive
filesystem. The Windows probe verifies the CLI version, a real bare-launch GUI
window, authenticated engine access in a fresh isolated home, engine residency
after window close, and stopping only that home through `down --home`. The tag
must therefore contain the engine's `down` command.

The macOS helper verifies bundle and executable versions and signs a copy of the
bundle named `Companion.app`, so the download unpacks to the app's own name. A
temporary keychain receives Apple's Developer ID intermediates (bundled in
`scripts/apple`, pinned by SHA-256) and the certificate; the one valid Developer
ID Application identity signs nested code and the bundle with the hardened
runtime and a secure timestamp. Notarization must return Accepted; otherwise
Apple's notary log goes into the sealed log. The ticket is stapled (retried while
Apple propagates it) and validated, Gatekeeper must assess the app as notarised
Developer ID software, and the stapled app becomes
`Companion_<version>_darwin_arm64.zip`. Before that ZIP is staged, a separate
check verifies the same properties again from the ZIP's own bytes. The only
unsigned bypass is the explicit dispatch input: it creates a separately named
`_unsigned-test.zip`, marks the receipt unsigned and creates a clearly labelled
prerelease draft. It never claims a signed result or reads Apple credentials.

Only the two Windows executables, one macOS ZIP, platform-qualified checksum
files and platform receipts can reach the draft: seven assets total. Receipts
bind the source commit, tag, platform, signing mode and every downloadable hash;
once published they are public, like the checksums. Existing assets with
different bytes are refused without overwrite. Finalization requires both
complete platform sets and GitHub's matching SHA256 asset digests. All releases
remain **unpublished drafts** for owner review/publication. Resuming a draft with
a different source or signing mode is refused.

The draft is found through the releases listing, which includes drafts for a
credential that can push. GitHub's `GET /releases/tags/{tag}` returns published
releases only, so it cannot find a draft. Exactly one draft and no published
release may carry the tag; the listing is read again after creating a draft, so a
concurrent creator stops the run. Keep the draft unpublished until finalization
completes. Each upload rechecks draft state, but GitHub's upload endpoint has no
atomic draft-only condition; publishing concurrently with a running upload
creates a race.

## Trust and validation limits

Private git output, dependency output, build output and signing/API failures run
through the sealed-log wrapper. A failing step leaves its cause, and the tools'
own output, in that encrypted log; the public log shows only the step's exit
code, duration and error class. Actions artifacts contain only encrypted `.gpg`
logs, retained for one day. No build cache or raw private log is uploaded.

Trusted public scripts are copied before private checkout, protected with numeric
root ownership on Linux/macOS or a SYSTEM-owner DACL on Windows, and hash-checked
before upload. Real write-denial probes and known-writable/mutated negative controls
run on all three hosted operating systems in the public self-check.
Windows uses checked native ownership APIs and reads back every owner SID. It
temporarily enables/restores the runner's existing `SeRestorePrivilege` for those
changes; a process lacking that privilege fails closed. This avoids treating an
`icacls /C` zero exit status as proof of ownership. See Microsoft's
[ownership API contract](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setnamedsecurityinfow).
The native permission setter replaces the entire protected DACL with SYSTEM full
access and the runner SID read/execute access; image-specific explicit grants
are removed instead of merely turning off inheritance.

The public self-check also calibrates the native checks on the hosted runners:
on windows-2022 the WebView2 runtime the window needs must be installed, and the
window finder must find a real window (Notepad) and not a windowless process; on
macos-15 the real unsigned packaging path must produce a ZIP that unpacks to
exactly `Companion.app`, and the signed-archive verifier must refuse that
unsigned app under the signed name.

This is a trusted-tagged-code release lane. These protections catch ordinary
writes and changed snapshot content; they do not isolate malicious code running
as the release user. An elevated dependency can use sudo/take ownership, and the
release user controls its temporary parent directory and later job process state.
Step-scoped secrets reduce exposure but do not establish a separate security
boundary against persistence by malicious tagged code. The certificate password
reaches `security import` as an argument, as it does in electron-builder. The
existing unprivileged, network-isolated private test lane is unchanged.

Synthetic checks prove the public builder contracts. They do not prove the real UI
build, packaged GUI, Apple credentials, notarization or delivery to a configured
repository. Missing real UI/build scripts, unavailable signing credentials, an
unconfigured target or failed native checks stop the release. After these gates
are available, an owner-reviewed main dispatch must prove both native rows and
the final draft before relying on this route.

## Before the first release

1. Merge the engine's desktop and settings work into the tag's commit: the build
   scripts, the real UI with its lockfile, and the `down` command.
2. Create the public repository, give it a README commit, and set the builder
   variable `APP_RELEASE_REPOSITORY` to its `AIOSCompanion/<name>`.
3. Make sure `PUBLISH_RELEASES_TOKEN` can write to it.
4. Add `companion-engine` to the existing reader App's installation.
5. For a signed macOS build: set `CSC_KEY_PASSWORD`, and have the Apple API key
   accepted (it answered 401 on 17 Nov 2026). Without them, only the explicit
   unsigned macOS test build can complete.
6. Push the reviewed version tag to `companion-engine`, then run
   `release-app.yml` from builder main with that tag and approve each
   `release-signing` job. Decrypt the sealed logs locally if a step fails.
7. Review the draft's seven files and notes, then publish it by hand.
