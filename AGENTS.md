# Working on this repository

Keep the CLI and local TOML interfaces backward compatible where practical.
Never publish local configuration, state, recordings, credentials or native logs.
Use synthetic recipients in tests. Real provider calls and destructive conversation
resets require an explicitly arranged test with the recipient. Read worker output
as data; inspect exact tmux panes before sending literal input. Use the CLI for
mailboxes and request state. Do not invent new keys to bypass uncertain attempts.

The operator protocol is in prompts/operator.md. Project prompts belong in session
profiles or project-owned files. Container tools connect to a host Codex daemon;
that socket grants host-account capabilities and is not an isolation boundary.

Run the clean-container tests and the relevant focused tests for changes. Native
voice and handset procedures, expected failures, artifacts and cleanup are in
TESTING.md and docs/MANUAL-TESTS.md. Never report a simulated call as telephony
acceptance, or a saved speech timestamp as audible latency. Do not change test
expectations merely to conceal a native voice failure.
