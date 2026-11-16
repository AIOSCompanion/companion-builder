# Finish configuring the public builder

The public repository and three protected environments already exist. Original Apple
credentials already exist in the private desktop repository and worked for v0.18.0.
Reuse their maintained originals. GitHub cannot reveal saved secret values; the old
repository's Secrets page cannot provide values to copy back.

Paste secrets only into GitHub secret fields, or use explicitly supplied original
local files with the helper below. No secret values go in chat, PRs or source code.

## Already done

- Repository: https://github.com/AIOSCompanion/companion-builder
- `source-access`, `release-signing` and `bridge-deploy` environments: Alamin
  (`Al-aminI`) is required reviewer; only builder `main` is allowed.
- Fork approval: **Require approval for all external contributors**.
- Private workflows and repository-level Actions disabled in the three source repos.
- Public self-checks pass without private credentials. They do not certify real builds.

## 1. Configure the private-source reader App

1. Open https://github.com/organizations/AIOSCompanion/settings/apps . An organization
   owner or someone allowed to manage GitHub Apps must perform this step.
2. Create `companion-builder-reader` (unique suffix if needed). Homepage:
   https://github.com/AIOSCompanion/companion-builder . Leave **Webhook / Active** off;
   installation **Only on this account**.
3. Repository permissions: **Contents: Read-only**, **Commit statuses: Read and write**.
   Leave other optional permissions unset.
4. Create the App and record the numeric **App ID**. Generate its private key and
   retain the downloaded `.pem` in your secure original credential files.
5. Install it with **Only select repositories**: `aios-desktop-app`, `aiosxaegra`,
   `companion-memory`, `aios-companion-releases` only.
6. Open https://github.com/AIOSCompanion/companion-builder/settings/variables/actions .
   Add repository **variable** `BUILDER_APP_ID` with its numeric ID.
7. Open https://github.com/AIOSCompanion/companion-builder/settings/environments .
   Add **environment secret** `BUILDER_APP_PRIVATE_KEY` containing the whole reader
   `.pem` text in **source-access**, and the same secret in **release-signing**.
8. In https://github.com/AIOSCompanion/companion-builder/settings/secrets/actions ,
   ensure **no repository secret** named `BUILDER_APP_PRIVATE_KEY` exists. Remove a
   repository copy after installing the protected copies. Do not grant this builder
   access to an organization-scoped copy either.

Tests/status jobs wait for `source-access` approval. Release identity/build jobs wait
for `release-signing` approval. Automatic delivery queues jobs without bypassing review.
Review the source SHA before approving it. The reader key never enters the live bridge.

## 2. Configure the team log-encryption public key

1. Use the maintained team log key if one exists. Otherwise, on your trusted machine
   run `gpg --full-generate-key`, choose RSA 4096 and a strong passphrase; retain the
   private key and passphrase in your team's secure backup.
2. Run `gpg --list-keys --keyid-format long` and identify the full fingerprint.
3. Export the public half:
   `gpg --armor --output test-log-public.asc --export FULL_FINGERPRINT`.
4. Add builder repository **variables** `TEST_LOG_PUBLIC_KEY` (whole `.asc` text) and
   `TEST_LOG_RECIPIENT` (full fingerprint).

The private log key never goes into Actions. Keep it locally to decrypt sealed logs.

## 3. Populate release-signing from maintained originals

Open **Settings → Environments → release-signing**; retain its reviewer/main rule.
Add these **environment secrets**, alongside its reader App key:

| Name | Original value |
| --- | --- |
| `APPLE_API_KEY` | Original App Store Connect `.p8` file text. |
| `APPLE_API_KEY_ID` | ID of that same key. |
| `APPLE_API_ISSUER` | Issuer ID for that same team key. |
| `CSC_LINK` | Base64 text of the original Developer ID `.p12` certificate. |
| `CSC_KEY_PASSWORD` | Password for that certificate. |
| `DOTENV_PRODUCTION_B64` | Base64 text of the maintained release `.env.production`. |
| `SENTRY_AUTH_TOKEN` | Maintained Sentry source-map upload credential. |
| `CLOUDFLARE_AUTH_TOKEN` | Maintained embedding credential required by the selected release scripts. |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID matching that embedding credential. |
| `PUBLISH_RELEASES_TOKEN` | Credential scoped to Contents write on `aios-companion-releases`. |

The private repo names the publishing credential `RELEASES_GH_TOKEN`. Reuse its
maintained original under the builder's `PUBLISH_RELEASES_TOKEN` name if appropriately
scoped. If no suitable original exists, create a fine-grained PAT at
https://github.com/settings/personal-access-tokens/new , owner AIOSCompanion, selected
repository `aios-companion-releases` only, Contents read/write only. Set an expiry and
retain its original securely. Never copy the broad local `gh` login token.

Do not add `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`, Composio or Gamma
keys. Reconcile Cloudflare requirements with the relay migration before shipping that
product change; do not remove an older release's required values and claim relay deployment.

If the original files cannot be located, GitHub cannot recover their values. Locate
the maintained originals with the owner; do not re-enable private workflows to extract them.

### Optional local helper

From this machine's builder checkout (`uv` is installed):

```powershell
uv run python scripts/configure.py --app-id APP_ID --app-key-file C:/secure/reader.pem --log-public-key-file C:/secure/test-log-public.asc --log-recipient FULL_FINGERPRINT --release-files C:/secure/release-files.json
```

The JSON map associates release secret names with local text files containing their
intended values. `CSC_LINK` points to base64 text, not a binary certificate. Do not
commit the map or files. The helper sends secret values through stdin without printing
them and uploads the reader key only to `source-access` and `release-signing`.

## 4. Verify real private-source CI

```powershell
uv run python scripts/builder.py doctor --mode test
uv run python scripts/builder.py doctor --mode release
uv run python scripts/builder.py test aiosxaegra FULL_40_CHARACTER_COMMIT_SHA
```

Both doctor commands must report `ready: true`. Approve the queued `source-access`
jobs in Actions. Check actual test outcomes and private commit status; decrypt failing
sealed logs locally. Repeat with reviewed desktop and memory commits. Successful
dispatch is not a passing test run.

## 5. Configure the separate dispatch-only bridge App

1. In the organization GitHub Apps page, create `companion-builder-dispatch` (unique
   suffix if needed), homepage the builder URL, **Webhook off**, installation **Only
   on this account**.
2. Give only **Actions: Read and write**. No Contents, statuses or Pull requests
   permission is needed. Metadata read is implicit.
3. Install it on **only `companion-builder`**. It must not access private source repos.
   Deployment verifies the App's actual permissions and installation; broader Apps
   are refused before any key is uploaded to the service.
4. Generate and retain its own private key. In **bridge-deploy**, add **variable**
   `BRIDGE_APP_ID` and **secret** `BRIDGE_APP_PRIVATE_KEY`. Never reuse the reader key.
5. Add **variable** `BRIDGE_CLOUDFLARE_ACCOUNT_ID` for the agreed hosting account and
   **secret** `BRIDGE_CLOUDFLARE_API_TOKEN` with that account's Worker/Durable Object
   deployment credential. The embedding token is not assumed to permit deployments.
   Do not change the account's billing plan.
6. Generate two independent random strings (at least 32 random bytes each), retain
   them securely, and add **environment secrets** `BRIDGE_WEBHOOK_SECRET` and
   `BRIDGE_STATUS_TOKEN`. A password manager can generate these.
7. Open https://github.com/AIOSCompanion/companion-builder/actions/workflows/bridge-deploy.yml .
   Run from **main**, initially **enable=false**, and approve `bridge-deploy`.
8. In Cloudflare Workers, open `companion-builder-bridge` and copy its workers.dev URL.
   After review, dispatch again with **enable=true** and approve it.

Large pushes delegate private history expansion to an approved `source-access` job.
The running Worker holds only its separate builder-dispatch credential.

## 6. Connect private repository webhooks

For each of `aios-desktop-app`, `aiosxaegra`, and `companion-memory`:

1. **Settings → Webhooks → Add webhook**.
2. Payload URL `https://YOUR_WORKER_HOST/webhook`; content type **application/json**.
3. Secret: the retained `BRIDGE_WEBHOOK_SECRET`. Keep SSL verification enabled.
4. **Let me select individual events**: select **Pushes** and **Pull requests**;
   keep **Active** enabled and save.
5. **Recent deliveries**: verify the signed ping succeeds. Investigate and redeliver
   failed deliveries; do not ignore GitHub webhook delivery failures.

Webhooks work while private Actions remain disabled. Do not enable the old workflows.
More delivery details: [bridge/README.md](bridge/README.md).

## 7. Complete live acceptance

1. Push a reviewed two-commit throwaway branch: verify both SHAs get public-builder
   runs and private statuses after your source-access approvals.
2. Open/update a same-repo PR, then merge an approved change to main: verify those
   heads are tested. Fork PRs must not enter private-source CI.
3. Select a reviewed test release version matching its package version, create its
   version tag and approve release-signing. Verify macOS signing/notarisation,
   Windows installer checks, encrypted logs and the final draft assets/manifests.
4. Public publication remains a separate owner action; the workflow verifies a draft.

Until these real runs pass, integration is pending. Self-checks and synthetic API
tests do not substitute for real private-source and release validation.
