# Security and private data

This is a personal, trusted-host phone operator, not tenant isolation. A person
allowed to call a session can direct that session's configured tools. Sessions
can remember earlier callers. Phone allowlists, individual PINs and session
permissions control admission; they do not create separate OS accounts.

New configurations default to read-only tools. `workspace-write` and
`danger-full-access` are explicit profile/server choices. Headless sessions use
approval policy `never`; an action requiring permission must fail rather than
wait for an invisible approval screen. Test the selected policy with your Codex
build. The container does not sandbox tools executed by the host Codex daemon.

The host daemon socket carries authority to use that account. Bind only its
control directory, keep matching host UID permissions, and never expose it on
an unauthenticated network listener. Do not mount the Docker socket into voice.
Full reset support needs additional private Codex metadata/history mounts; see
[containers](docs/CONTAINERS.md). Do not place credentials in an image or build arg.

Runtime JSON, native rollouts, recordings, audio-test evidence, phone numbers,
SIP accounts, PIN verifiers, encrypted PIN delivery jobs and encryption keys are
private. Twilio recordings are disabled by this application; local recordings
and Codex conversation history still exist. `state/` and local config are ignored
by Git, but review the exact staged files before publishing. Never attach raw
support archives or a PIN print command's output to an issue.

Credential files use literal KEY=value entries, mode 0600, with no shell
evaluation. Process environment overrides file values. A PIN-delivery opt-out
cancels that delivery only; it does not disable other scheduled calls.

Report vulnerabilities through the repository's private vulnerability reporting
feature when enabled. If unavailable, ask the maintainer for a private channel
without including the exploit, credentials, or personal data in a public issue.
