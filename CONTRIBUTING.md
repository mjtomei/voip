# Development and release checks

Use a clean source checkout and the commands in [TESTING.md](TESTING.md). Ordinary
tests do not require credentials. Tests that use the host Codex daemon consume
model usage; carrier tests require arranged recipients. Keep the distinctions in
test reports and retain original failures when fixing a harness.

The project currently has no license file, by the owner's choice. Dependencies
and third-party patches retain their own upstream terms; adding or distributing
bundled binaries requires checking those terms. CI builds dependencies from their
pinned inputs; do not commit `.tools`, `.build`, downloaded models or node_modules.

Before a public push:

```bash
python3 scripts/check-public-tree.py
git status --short
git diff --cached --stat
git diff --cached
```

Review the exact staged tree, including Markdown, HTML and historical evidence.
The local checker is a guard for known private paths, artifact classes and obvious
credential patterns; it is not a comprehensive secret scanner. Enable GitHub
secret scanning/push protection and private vulnerability reporting on the actual
repository. This setup does not create or publish a GitHub repository.

Use `voip ... --json` for machine consumers. The envelope is
`{"schema_version":1,"ok":true,"data":...}`; failures contain
`error.code="operation_failed"` and a safe message, with exit 1. Argument parsing
uses exit 2. Existing command payloads retain their domain-specific fields. Keep
stable request/message IDs, preserve unknown outcome blocking and distinguish
accepted prompts from work completion. Never silently change a person's mailbox
scope or route to a default recipient after an identity error.

Documentation-only/low-impact changes need appropriate focused verification.
Protocol, persistence, scope, permissions and lifecycle changes need meaningful
regression coverage. Preserve current CLI names as compatible aliases; build on
the local CLI/TOML model instead of reviving the abandoned distributed API plan.
