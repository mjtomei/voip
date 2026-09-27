# Containers and a host Codex daemon

The phone frontend, virtual audio, Baresip, dispatcher, cron and mailbox live in
one unprivileged container. Codex remains on the host and executes tools there.
This preserves access to host tmux/SSH/workspaces under the host account.
Codex's [App Server Unix transport](https://developers.openai.com/codex/app-server)
uses WebSockets over a Unix socket. Native realtime availability remains
experimental and must be tested with the installed account/build.

## Requirements and boundaries

- Linux Docker Engine; this recipe does not claim Docker Desktop Unix-socket
  interoperability on macOS/Windows. Docker and Podman builds may differ; Docker
  is the validated recipe.
- A host Codex installation already authenticated with voice access and a running
  `codex app-server daemon start`. The container does not start or stop that daemon.
- Container UID/GID match the host user, preserving socket and workspace access.
- The project workspace must be mounted **at the same absolute path**. Codex sees
  host paths while the frontend sees container paths. Mount registered external
  projects at their same paths too; matching paths are verified on resume.
- Mount the daemon's socket **directory**, not just its socket inode, so daemon
  socket replacement remains visible. No host audio, privileged container, Docker
  socket, host process namespace or publicly exposed daemon port is needed.
- The daemon socket grants host-account capabilities. Read-only bind mounts do
  not make RPC requests read-only. Configure operator tool permissions explicitly.

## Setup

Copyable settings are in [the container server example](../server.container.toml.example),
[local Compose overlay](../compose.local.yaml.example), and
[configuration index](../examples/README.md). The overlay is optional; copy it to
`compose.local.yaml`, set `HOST_CODEX_HOME` in `.env`, and include both files with
`docker compose -f compose.yaml -f compose.local.yaml ...` when using it.
Uncomment its project mount and set `VOICE_PROJECT_DIR` if an external project is
registered. All TOML paths must be replaced explicitly; they do not expand `.env`.

1. Build `docker build --target runtime -t voice-operator:runtime .`.
2. Copy `.env.example` to `.env`. Set `VOICE_WORKSPACE` to a new private host
   directory, `CODEX_SOCKET_DIR` to `$CODEX_HOME/app-server-control` (normally under
   `~/.codex`), and UID/GID from `id -u` and `id -g`. Create the workspace yourself.
3. Initialize the mounted workspace, replacing the fictional numbers:

```bash
docker compose run --rm voice voip init --number +12025550100 \
  --recipient alice --destination +12025550101 \
  --daemon-socket /codex-control/app-server-control.sock
```

4. In workspace `server.toml`, set the host-side CLI command under `[operator]`:

```toml
manage_daemon = false
daemon_socket = "/codex-control/app-server-control.sock"
cli_command = ["/absolute/path/to/checkout/bin/voip-container", "--container", "voice-operator"]
permissions = "read-only"
# Use danger-full-access for host Docker CLI access and unrestricted host tools.
tmux_socket = "/tmp/voip-runtime/operator-tmux.sock"
```

The command executes on the host when Codex uses mailbox/call tools. That wrapper
runs `docker exec` against this container; no Docker control socket is shared.
The configured container name must match `.env`. Direct shell/files/tmux tasks
run on the host; phone-service CLI commands run in the container namespace.
For a convenient host command, add a `voip` symlink in your PATH pointing to this
checkout's `bin/voip-container`. Preserve any existing executable instead of
overwriting it. `VOIP_CONTAINER_NAME` selects a non-default container name.
The public default is read-only. `workspace-write` permits project file operations,
but the Codex sandbox blocks access to the host Docker socket, so neither mode
can use this Docker-based mailbox/call wrapper. Select `danger-full-access` when
the operator should manage the container through host Docker and use unrestricted
host tools. The service does not silently expand permissions. The private tmux
socket above keeps container operator processes separate from host tmux servers,
including when the workspace contains an old host socket. Generated directory
operators inherit this explicit socket; existing private profiles need the same
setting when migrating a host installation.

When adopting this setup from a host installation, start fresh operator
conversations after setting `cli_command`. Saved conversations can retain old
host-side service commands even when the current connection notice contains the
container command. After saving needed context and arranging any active caller's
participation, use the container CLI's `operator reset --scope fresh` for each
affected profile. This saves the old history and selects a new conversation;
ordinary worker sessions, mailboxes and schedules are retained. Historical CLI
commands are not automatically forwarded to the container.

5. Configure the private credentials file, PIN and provider deployment through
   `docker compose run --rm voice voip ...` as in the README. Use
   `config apply --hosted-only`. Neither the container image nor `.env` includes
   secrets. PIN telephone delivery begins only after deployment and dispatcher
   startup; choose a time the recipient expects the call.
6. Run `docker compose up -d`, then `docker compose exec -T voice voip doctor --live`
   and `docker compose exec -T voice voip status --json`.

The foreground supervisor initializes an operator only if no saved identity
exists. Unknown creation outcomes still require inspection/adoption. Restarting
the container preserves workspace state and resumes the selected conversation.

## Host discovery and startup helpers

Directory discovery must see host worker tmux sessions. In the private provider
configuration, set `[directory_extensions].snapshot_file` to
`state/host-directories.json`, and keep `tmux_socket` pointed at the host worker
socket (empty means its normal default). Install the host helpers from the source
checkout, using the same private workspace mounted in Compose:

```bash
python3 scripts/install-host-helpers.py --root /absolute/path/to/voice-workspace --enable
```

This enables a user timer that idempotently starts the shared host Codex daemon,
and a user service that publishes a private tmux discovery snapshot every fifteen
seconds. It does not stop/restart the daemon or manage workers. It does not start
the phone container. Omit `--enable` to inspect rendered units first. The command
prints the unit names; disable those exact startup units with `systemctl --user
disable --now UNIT...` when retiring this deployment. Configure `--codex` and
`--codex-home` if the host uses non-default locations.

For startup without an interactive login, enable user lingering through the host
administrator (`loginctl enable-linger USER`) and ensure Docker's service starts
at boot. Compose's `restart: unless-stopped` then recovers the phone container;
the host timer recovers the daemon independently. Stop old per-component phone
services and timers before starting this stack. Never run two dispatchers or SIP
registrations for the same profiles during migration.

## Resets and historical inspection

`*1#` uses the socket and container process records. Existing `*2#`/`*3#`
implementations additionally check the Codex SQLite metadata, descendants and
rollout backup before archiving. Without that evidence they fail closed before
interrupting the operator. This is an explicit extra capability, not implied by
a successful voice connection.

To enable these resets, add a read-only bind of the host Codex home **at its
original absolute path**, set `[operator].state_database` to its `state_5.sqlite`,
and pass `CODEX_HOME` with that same path to the container. This exposes native
history and may also expose account credential files to container processes;
the base Compose file deliberately mounts only the control directory. If that
exposure is unacceptable, keep frontend recovery only. Never copy a live SQLite
database without its WAL/SHM or assert that an incomplete snapshot proves no
child agents exist. Schema/version compatibility is checked by the reset path.

## Network and lifecycle

SIP uses outbound TLS registration and secure media. WebRTC establishes outbound
media. The default bridge network is the starting configuration; real carrier
media/NAT behavior needs the handset acceptance tests on the deployment network.
Do not infer telephony success from the container's local WebRTC echo test.

`docker compose stop` stops the owned stack. It leaves persisted requests,
mailboxes and host Codex work intact. `docker compose down` removes the container,
not the bind-mounted workspace. Disabling a schedule stops future occurrences;
cancel an already-enqueued request separately. `docker compose up -d` starts it
again, including eligible saved requests. Docker's own startup service must be
enabled for `restart: unless-stopped` to work after host boot.

Run tests with the test image and dedicated workspaces. Never mount a production
workspace into a clean test container or reuse a production native thread.
