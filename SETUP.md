# Setup: the public builder

Plain steps, in order. Nothing here asks you to paste a secret into chat. Where a step says "paste", paste into the GitHub page only. Values marked **secret** go in the secret box. Values marked **variable** go in the variable box and are not secret.

Estimated time: about 30 minutes.

## 0. Create the public repository (only if the automation cannot)

The automation's token may not be allowed to create an organisation repository. If so, do this yourself:

1. Open https://github.com/organizations/AIOSCompanion/repositories/new
2. Repository name: `companion-builder`
3. Choose **Public**.
4. Tick **Add a README file**.
5. Click **Create repository**.

The automation will push the build scripts into it once review is done. It holds build scripts and workflows only. The Companion code stays in the private repositories.

## 1. Configure the dedicated GitHub App

The Apple signing/notary secrets already exist in `aios-desktop-app` and the previous
v0.18.0 build used them. Reuse their maintained original values; do not create new
Apple credentials just because the public builder has not been configured.
Saved GitHub secret values cannot be read or copied back through the API.
For automatic commit/main/tag routing, also follow [bridge/README.md](bridge/README.md):
the App needs the builder installation and the additional scoped dispatch permissions
described there, and its webhook must be enabled after deploying the receiver.

1. Open https://github.com/organizations/AIOSCompanion/settings/apps (the organisation's **Developer settings**, then **GitHub Apps**).
2. Click **New GitHub App**.
3. **GitHub App name**: `companion-builder-reader`
4. **Homepage URL**: `https://github.com/AIOSCompanion/companion-builder`
5. Leave **Webhook** off during initial setup. After the event receiver is deployed,
   enable it using the URL, secret and event subscriptions in `bridge/README.md`.
6. Under **Repository permissions**, set:
   - **Contents**: **Read-only**
   - **Commit statuses**: **Read and write**
   - **Pull requests**: **Read-only** (same-repo PR event subscriptions).
   - **Actions**: **Read and write** (dispatches into the builder only).
   Checkout and status jobs request narrower tokens. The event receiver requests an
   Actions-write token scoped only to `companion-builder`; private Actions stay disabled.
7. Under **Where can this GitHub App be installed?**, choose **Only on this account**.
8. Click **Create GitHub App**.
9. On the app's page, note the **App ID** (a number). Put it in the public repo as a **variable**: go to `companion-builder` → **Settings** → **Secrets and variables** → **Actions** → **Variables** tab → **New repository variable**. Name: `BUILDER_APP_ID`. Value: the number.
10. On the app's page, scroll to **Private keys** and click **Generate a private key**. A `.pem` file downloads.
11. Back in `companion-builder` → **Settings** → **Secrets and variables** → **Actions** → **Secrets** tab → **New repository secret**. Name: `BUILDER_APP_PRIVATE_KEY`. In the value box, paste the whole contents of the `.pem` file (open it in Notepad, select all, copy, paste). Then delete the `.pem` file from your Downloads folder.
12. On the app's page, click **Install App** (left side), then **Install** next to AIOSCompanion.
13. Choose **Only select repositories** and pick exactly these five: `aios-desktop-app`,
    `aiosxaegra`, `companion-memory`, `aios-companion-releases`, `companion-builder`.
    The releases repo permits runtime/manifest reads; the builder installation permits
    workflow dispatch. Click **Install**.

Check: the app should not be installed on any other repository.

## 2. The test-log key (the public log is encrypted to this key)

The full test output is encrypted to a key only the team holds. Only the public half goes in the repo; the private half stays with you.

1. Create a key on your computer with `gpg --full-generate-key` (RSA 4096, no expiry, name `companion-builder test logs`, email `companion-builder@invalid`). Use a strong passphrase and keep it in your password manager.
2. Export the public key: `gpg --armor --export "companion-builder test logs" > test-log-public.asc`.
3. In `companion-builder`, add two **variables** (not secrets):
   - `TEST_LOG_RECIPIENT`: the text `companion-builder test logs`.
   - `TEST_LOG_PUBLIC_KEY`: open `test-log-public.asc` in Notepad and paste its whole contents.
4. Keep the private key and passphrase to yourself. Anyone with it can read the test logs.

## 3. The release-signing environment (macOS signing)

Signing secrets never sit at repository level. They sit in an environment that needs your approval before any job can read them.

1. In `companion-builder`, go to **Settings** → **Environments** → **New environment**. Name: `release-signing`. Click **Configure environment**.
2. Tick **Required reviewers**, add yourself (`Al-aminI`), and save.
3. Under **Deployment branches and tags**, choose **Selected branches and tags**, and add a **Branch** rule: `main`. The workflow dispatch runs from the builder's main branch; its input names a separate tag in the private source repository. Do not use a `v*` tag rule here: GitHub checks the workflow's ref, not its input.
4. Under **Environment secrets**, click **Add secret** for each of these, pasting the value from the place it already lives (your Apple developer account or the `aios-desktop-app` repository secrets):
   - `APPLE_API_KEY`: the App Store Connect API key file contents (the `.p8` text).
   - `APPLE_API_KEY_ID`: the key ID.
   - `APPLE_API_ISSUER`: the issuer ID.
   - `CSC_LINK`: the Developer ID certificate, base64.
   - `CSC_KEY_PASSWORD`: the certificate password.
   - `DOTENV_PRODUCTION_B64`: the original build-time settings file, base64. GitHub cannot reveal an existing secret's value; obtain it from the maintained release configuration, not the GitHub Secrets page.
   - `SENTRY_AUTH_TOKEN`: lets the build upload source maps to Sentry. Copy it from the `aios-desktop-app` secret of the same name.
   - `CLOUDFLARE_AUTH_TOKEN`: the Workers AI token the app uses for memory embeddings. Copy it from the `aios-desktop-app` secret of the same name.
   - `CLOUDFLARE_ACCOUNT_ID`: the Cloudflare account the Workers AI token belongs to. Copy it from the `aios-desktop-app` secret of the same name.
5. Do not add `APPLE_ID` or `APPLE_APP_SPECIFIC_PASSWORD`. The Apple ID route is removed on purpose.

The three `APPLE_API_*` names are already set on `aios-desktop-app`. The automation cannot read their values, so they must be copied here by you.

## 4. The publish credential (writes release assets)

Writing to `aios-companion-releases` (creating the draft, uploading assets, the build's publish step) needs a write token on that repository only. The App reads; this token writes. It also reads drafts, because GitHub shows a draft only to a token with write access.

1. Open https://github.com/settings/personal-access-tokens/new (a fine-grained token on your account).
2. **Token name**: `companion-builder publish`. **Expiration**: 90 days (set a reminder to rotate).
3. **Resource owner**: AIOSCompanion.
4. **Repository access**: **Only select repositories** → `aios-companion-releases` only.
5. Under **Repository permissions**: **Contents**: **Read and write**. Nothing else.
6. Click **Generate token** and copy it once.
7. In `companion-builder` → **Settings** → **Environments** → `release-signing` → **Add secret**. Name: `PUBLISH_RELEASES_TOKEN`. Paste the token.

When the token expires, make a new one the same way and replace the secret.

## 5. Check before the first test run

GitHub cannot reveal saved repository secret values. Use the original certificate, notary key and maintained
release configuration to populate the environment; listing a secret's name does not copy its value.
Never paste any of these values into a conversation.

Run `python scripts/builder.py doctor --mode test` and `--mode release` to see missing configuration names.
Use `python scripts/builder.py self-check` to verify public builder code independently of private credentials.

The owner can populate the test configuration from original local files without copying secrets into a terminal command:

```sh
python scripts/configure.py --app-id APP_ID --app-key-file /path/to/app-key.pem --log-public-key-file /path/to/team-public.asc --log-recipient TEAM_KEY_FINGERPRINT
```

`--release-files /path/to/map.json` additionally accepts a JSON object mapping the environment-secret names above
to local text files containing their intended values (including base64 text where specified). Values are streamed
to `gh secret set` through stdin and never printed. The helper reads only paths the owner explicitly supplies.

Tell the maintainer when steps 1 to 4 are done. It will then run one test on a throwaway tag. That run is free. Please do not add any other secrets to the public repository.

## What the automation will not do

- They will not create the App, the signing environment, the test-log key or the publish token for you.
- They will not ask you to paste a secret into chat. If a message asks you to, do not paste.
