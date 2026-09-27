# Operating, upgrading and restoring an installation

Choose one supervisor: foreground stack (`service run`, Docker/Compose), managed
background stack (`service start`), user startup stack (`service enable`), or the
legacy per-component systemd units installed by full `config apply`. Do not run
two against the same project. Deployment with `--hosted-only` is independent of
supervision. Existing deployments can keep their current supervisor during an
upgrade; migration is a separate maintenance step.

## Routine controls

```bash
voip --root /path/to/workspace status --json
voip --root /path/to/workspace service status
voip --root /path/to/workspace service start
voip --root /path/to/workspace service stop
voip --root /path/to/workspace service enable
voip --root /path/to/workspace service disable
voip --root /path/to/workspace doctor --live --json
```

`service run` stays in the foreground. The stack owns SIP, monitors, per-project
dispatchers and frontend recovery, with private logs under `state/logs`. Stopping
it ends its SIP/media processes but does not interrupt the host Codex daemon or
ordinary worker tasks. Pending requests survive; restarting can dispatch them.
Startup service installation requires working user systemd; enable user lingering
through the host administrator if services must run after logout. Containers use
`docker compose up -d`, `stop`, `down` and their restart policy instead.

The legacy `operator pause` only pauses automatic frontend recovery; it is not
a stop-calling switch. To stop one scheduled job, set its `enabled=false` and
cancel any existing request separately. Inspect `cancel_settled` before saying
an owned provider call ended. Cancel PIN deliveries separately and reconcile
hosted confirmation/opt-out receipts before rotation.

Status aggregates registered projects, sessions, active locally observed calls,
pending queue records and upcoming schedules. Check observer timestamps: stale
state is not evidence that a phone is still connected. `doctor --live` checks
daemon initialization and local SIP observer health; provider registration and
native speech require their dedicated probes. `config show` exposes effective
non-credential configuration and source paths. `config plan` lists deployment
effects before `config apply`; it does not mutate hosted resources.

## Backup and restore

1. Disable future schedules, settle/cancel pending provider attempts as intended,
   and wait for active calls to end. Stop the phone stack. Coordinate with worker
   agents so they are not writing mailboxes/config while the backup is copied.
2. Back up every registered project directory plus provider configuration and
   state, preserving permissions. The provider PIN master key, encrypted delivery
   state, signing secret, SIP credentials and provider IDs must remain together.
   An archive is secret material even though PINs are not plaintext.
3. Back up the host Codex account/history through its supported host procedure.
   Saving only `operator-thread-id` does not save the conversation. Do not copy a
   running SQLite database without a consistent backup and its WAL handling.
4. Restore to the same absolute paths for container/host consistency. Restore
   UID ownership and private modes before enabling services. Install the matching
   code/dependencies, inspect `config show`, `config check` and `config plan`.
5. Keep the dispatcher stopped while checking provider call/delivery receipts.
   Unknown submissions must be reconciled under their original keys. Do not
   erase the ledger to make startup succeed. Start the service only when the
   retained pending work is intended to run; then inspect status/next schedules.

Do not put backup archives in the public repository, container image, CI artifacts
or a support issue. Use a private encrypted backup destination selected by the user.

## Upgrade and rollback

Record the image/source revision and host Codex version. Run clean-container
tests on the new revision, then the native smoke on an isolated thread. Review
release notes and configuration validation before changing a live installation.
Stop the chosen supervisor during code/environment replacement; preserve state.
Deploy changed hosted handlers deliberately and verify their hashes/access rules.
Run a consented handset test when changing SIP, routing, PIN or call recovery.

No automatic destructive schema migration is performed at startup. Explicit
mailbox/PIN migration commands keep their existing recovery rules. Rollback must
use a code version compatible with the current state; restore the pre-upgrade
backup if a deliberate migration changed it, after reconciling external calls.
Do not replay an old backup's already-settled provider attempts blindly.
