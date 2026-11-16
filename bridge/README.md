# Private events to the public builder

This Worker receives signed GitHub App webhooks. Builds and tests execute only in
`AIOSCompanion/companion-builder` on hosted GitHub runners. The Worker does not build
anything or check out product source.

## Routing

- A branch push, including `main`, dispatches `test.yml` for **each** commit in the
  push and its head. It does not collapse a multiple-commit push to its last commit.
- Opening, reopening or updating a PR in the same private repository dispatches its
  head. Fork PRs are excluded.
- A newly created desktop `vX.Y.Z` tag dispatches `release.yml`. Its original tag
  object SHA accompanies the request. The workflow validates that identity, peels
  annotated tags and checks out the resulting immutable commit. Moved version tags
  are refused.
- Release signing retains the `release-signing` owner approval. The release
  workflow produces and verifies a draft; it does not automatically publish it.

The receiver authenticates the exact raw body using WebCrypto HMAC verification,
checks the organization/repository allowlist and drops private metadata before
storing a delivery. Durable Objects retain the minimal delivery and retry GitHub
failures. Push payloads at GitHub's 2048-commit limit trigger paginated commit
expansion, checkpointed between requests. A new branch at that limit can run tests
on older reachable history too, because it has no previous branch SHA.

The same delivery ID is deduplicated. GitHub dispatch has no idempotency key: a
connection loss after GitHub accepts a dispatch but before its response reaches the
Worker can produce a duplicate run on retry. Repeated events (such as a branch push
and its PR update) can also test the same SHA. Release workflow concurrency and the
single-draft release logic still apply. No exactly-once execution claim is made.

## Configuration

Existing Apple signing/notary credentials remain the originals already held in the
private desktop repository; creating this bridge does not replace them. GitHub
cannot copy or reveal saved secret values across repositories. They still need to
be populated in the builder environment from the maintained originals.

The dedicated builder GitHub App must be installed on the three source repositories,
the releases repository **and the builder repository**. Set these repository
permissions: Contents read, Commit statuses write, Pull requests read, Actions write.
The bridge mints an Actions-write token scoped **only to the builder**; checkout and
status jobs continue requesting their smaller permissions separately. Private repo
Actions remain disabled.

In the App settings, enable a webhook at the deployed Worker's `/webhook` URL,
set a strong webhook secret and subscribe to **Push** and **Pull request** events.
Do not create an unsigned repository-dispatch endpoint or put a broad personal
token into the Worker.

Create a protected `bridge-deploy` GitHub environment with Alamin as reviewer and
the builder `main` branch allowed. Configuration used by `bridge-deploy.yml`:

- Repository variable `BUILDER_APP_ID` and secret `BUILDER_APP_PRIVATE_KEY`, also
  used by the build workflows.
- Environment variable `BRIDGE_CLOUDFLARE_ACCOUNT_ID`.
- Environment secret `BRIDGE_CLOUDFLARE_API_TOKEN`, scoped to deploy this Worker and
  its Durable Object namespace. Do not assume the existing embeddings token has
  deployment permissions.
- Environment secrets `BRIDGE_WEBHOOK_SECRET` and `BRIDGE_STATUS_TOKEN` (independent
  random values). The former matches the GitHub App webhook secret; the latter
  protects the delivery-status endpoint.

Deploy through the **public builder's** `bridge-deploy.yml` workflow. It defaults
to disabled. After owner setup and the agreed review, dispatch with `enable=true`.
Deployment first disables acceptance, transfers Worker secrets through stdin and
then enables it. The script suppresses provider error bodies and never prints keys.
Disabling acceptance does not erase accepted deliveries; their retry alarms retain
them until the bridge is enabled again.

SQLite-backed Durable Objects are supported on Cloudflare's Free plan. Deployment
must use the agreed account and plan; this implementation does not change billing
plans or establish a spending cap. Deployment and hosted event acceptance are
**unverified until actually performed**.

## Acceptance gates

1. `npm ci --ignore-scripts && npm test` in `bridge/` and the builder self-check pass.
   The runtime test uses real workerd, SQLite storage and a real durable alarm;
   its GitHub API is synthetic and cannot access private repositories.
2. Deploy disabled, configure the App webhook and verify its signed ping after
   enabling acceptance.
3. Push a two-commit throwaway branch to a private source repo: both SHAs get real
   builder test runs and private commit statuses; inspect decrypted logs locally.
4. Open/update its same-repo PR and merge an approved change to main: verify the
   respective heads get test runs, with no private Actions run.
5. Create a reviewed test version tag matching the package version; approve the
   signing environment. Verify macOS signing/notarisation, Windows installer checks,
   encrypted-only logs and the final draft manifest/assets.
6. Query `/delivery/<GitHub-delivery-UUID>` with the status bearer token to confirm
   `complete=true`; a nonzero retry count means investigate the delivery before
   treating automation as healthy. Keep monitoring delivery failures in GitHub.

Unit/mocked integration checks cannot substitute for gates 2–6. A green builder
self-check does not establish a working private-source or release integration.
