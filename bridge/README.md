# Private events to the public builder

This portable Node receiver runs on an existing Linux host. It receives signed
webhooks and dispatches GitHub workflows; all builds and tests execute in
`AIOSCompanion/companion-builder` on hosted GitHub runners. It never checks out
product code. The receiver has no npm dependencies and requires Node >=22.13.

## Routing and persistence

- Every branch push, including main, dispatches each commit and the head to test.yml.
- Same-repository PR opened/reopened/synchronize/ready events dispatch the head;
  forks and closed PRs are excluded.
- A newly created desktop version tag dispatches release.yml with its immutable
  tag-object SHA. Moved tags are refused. Release validation peels annotated tags.
- Pushes reaching the 2048-commit payload limit delegate expansion to catch-up.yml.

WebCrypto verifies the exact request body against the webhook HMAC. Only allowed
organization/repository metadata and hashes are persisted; private commit messages
and payloads are discarded. SQLite commits acceptance and its retry alarm in one
transaction, with WAL and synchronous FULL. Successful dispatches are checkpointed
individually. Restarting the service resumes accepted work and outages retry with
bounded exponential backoff. The database survives code deployment.

Delivery UUIDs are deduplicated. GitHub dispatch has no idempotency key: losing a
response after acceptance can cause a duplicate workflow on retry. Push plus PR
events can also repeat a SHA. No exactly-once claim is made.

## Credentials and host setup

The existing reader App remains restricted to private source/release repositories;
its key lives only in approved source-access/release-signing environments. Every
private checkout and status job still requires owner approval. The bridge uses the
existing separate dispatch App installed ONLY on companion-builder, with Actions
write and no private source access. Deployment and startup both audit that scope.
Do not create replacement Apps or signing keys.

Use the existing protected bridge-deploy environment, main-only and owner-approved:

- Variable BRIDGE_APP_ID.
- Secrets BRIDGE_APP_PRIVATE_KEY, BRIDGE_WEBHOOK_SECRET, BRIDGE_STATUS_TOKEN.
- Secrets BRIDGE_SSH_HOST and BRIDGE_SSH_KNOWN_HOSTS for a verified existing host.
  Pin its host key using authenticated cloud access; never disable host checking.
- Existing GCP_WORKLOAD_IDENTITY_PROVIDER, GCP_DEPLOY_SERVICE_ACCOUNT and
  BRIDGE_GCP_PROJECT secrets. Cloud federation must restrict the numeric repository
  and owner IDs, main branch and bridge-deploy environment. The deploy identity
  needs OS Login administrative access on the existing host.

The approved job creates a temporary SSH key, registers it with OS Login for
15 minutes, derives the username from the authenticated profile and removes the
key when finished. No reusable SSH private key is saved in GitHub. The host-access
workflow verifies the same pinned host and administrative login before deployment.

The existing Linux x64 target needs Python3, tar/xz, systemd and passwordless sudo
for the trusted installer. It installs an isolated Node 22.23.4 runtime under the
bridge directory, verifies the official archive against a committed SHA-256 before
extraction, and leaves system Node unchanged. Deployment creates a dedicated
non-login service user and a hardened systemd unit. Public receiver files are
versioned under /opt/companion-builder-bridge; credentials and SQLite live in
/var/lib/companion-builder-bridge with private permissions. Prior code versions
and the existing database are retained. Keys travel via stdin, never argv or logs.

The service listens ONLY on 127.0.0.1:8123. Configure the host's existing TLS proxy
with a dedicated HTTPS route, a 25MB request limit and body timeout. No new hosting
plan is purchased by this code. The deploy workflow does not modify production
proxy routes, firewalls or certificates. The separate approved `bridge-proxy.yml`
workflow adds `/builder/webhook` and bearer-protected `/builder/delivery/` routes
to the existing TLS server using BRIDGE_PUBLIC_HOST and BRIDGE_NGINX_SITE secrets.
It retains a private backup, validates nginx before reload and restores the prior
configuration if validation or reload fails. Existing backend routing remains in
place. Keep the bridge disabled until HTTPS is
ready. Disabled mode refuses new deliveries and retains pending retry work.

Run only the public builder's bridge-deploy.yml from main. It defaults enable=false,
stops the prior receiver before installing its complete configuration, then starts
and checks local readiness. configure each private source repository webhook with
the HTTPS /builder/webhook endpoint, the shared webhook secret, and push/PR events.

## Acceptance gates

1. Builder self-check and `npm test` pass. Runtime tests use actual loopback HTTP
   and SQLite, with restart recovery, partial-failure retry and duplicate delivery;
   GitHub calls are synthetic and cannot reach private repositories.
2. Deploy disabled to the verified existing host, configure TLS, enable, and verify
   a real signed GitHub ping. No deployment or event routing is proven by unit tests.
3. A two-commit private branch push creates both real public-builder test runs;
   approve source-access, verify statuses and decrypt logs locally.
4. A same-repo PR update and approved main merge create the expected runs, with
   private Actions still disabled.
5. A reviewed version tag produces a signed/notarized macOS build, verified Windows
   assets and final draft manifest after release-signing approval. Existing Apple
   credential originals must be supplied; GitHub cannot reveal saved secret values.
6. The bearer-protected /delivery/<UUID> status reports complete=true; monitor
   retries and GitHub webhook delivery failures. No public payload logging.

Hosted deployment, automatic event routing and product release remain unverified
until those gates are actually exercised. A builder self-check is not release proof.
