# Companion public builder

Use this repository for Companion GitHub CI and release builds. Product source stays private.

- Use `python scripts/builder.py test <repository> <full-sha>` and `python scripts/builder.py release <version-tag>`.
- Run `python scripts/builder.py doctor --mode test` or `--mode release` before private-source work. Missing credentials are a configuration blocker, not permission to run private Actions instead.
- Use `self-check.yml` for changes to this public builder. It needs no private-source or signing credentials.
- Automatic source push/PR/tag routing lives in `bridge/`. Deploy it only through this repo's
  `bridge-deploy.yml`, after its own configuration and review. Keep delivery metadata private,
  require signed webhooks, and retain the signing-environment approval for releases.
- Never dispatch private-repository CI/release workflows, push a tag that starts private builds, or add self-hosted runners as a fallback.
- Never publish product source, environment files, signing keys, tokens or decrypted logs here. Private-source output is sealed; only validated hashes and summary counts may be exported.
- Releases dispatch from the builder's main branch. The source version tag is an input; the signing environment allows main and retains its owner approval.
- No Linux package is shipped by this builder. Hosted Ubuntu is used for tests and orchestration.
- A green self-check proves builder tests only; it does not prove private checkout, signing, installer validation or a product release. Report those gates separately.
- Keep the private-source App key only in owner-approved `source-access` and
  `release-signing` environments. Every job that reads it must use one of those.
  The live bridge uses a separate App installed only on the public builder;
  never deploy the private-source reader key into that service.
