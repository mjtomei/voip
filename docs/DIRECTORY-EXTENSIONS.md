# Directory extensions

Manual session extensions keep their configured numbers. Optional generated
extensions expose directories with live tmux sessions, with the configured home
directory as the no-extension default for explicitly enabled recipients.
Detached tmux sessions count. A voice operator's own tmux session does not.

## Selection and numbering

The default `smart` discovery rule is:

1. Use a session's base directory when it differs from the configured home root.
2. Otherwise compare the session name with the working directories of its live
   panes and their ancestors below home (so `project/src` can select `project`).
   Exact/normalized names win; a sufficiently close, unambiguous name can
   match a descriptive suffix such as `render` and `renderer-demo`.
3. If no candidate reaches the confidence threshold or the best two are too
   close, use the deepest common ancestor of the live pane directories, ignoring
   panes sitting at home. A single distinct project directory selects itself;
   sibling project subdirectories select their shared parent.
4. Keep home if there are no eligible project panes, or their only shared ancestor
   is home. `directories preview` reports the selection and its reason.

For example, a session named `starlight` with panes in `~/demo_workspace` and a
spare shell in `~` selects `~/demo_workspace` through step 3. This selects the
operator's workspace without changing any existing tmux session or process cwd.

Only directories within the configured root are included. Symlink aliases are
canonicalized, linked panes are deduplicated, and dead panes do not count. The
selector does not scan unrelated files or move any shell/agent. To change a
session's deliberate base when attaching, use `tmux attach-session -t '=NAME'
-c /absolute/project/path`. Existing processes keep their current directories.
`mode="sessions"` uses bases literally; `mode="panes"` includes every live pane
directory instead of selecting one directory per session.

Build a tree from the selected directories, sort siblings lexically, and number
the choices. Omit intermediate directories with one child and no operator of
their own. For example:

```text
~                         default, no extension
├── notes/journal         1
└── work/apps
    ├── alpha             21
    └── beta              22
```

If `work/apps` also has its own active session it gets `2`; the child extensions
remain `21` and `22`. A target with a child is a choice between stopping here and
continuing. Home is always a target, even with no workers, and a single project
under it gets `1`. Wider forks use equal-width labels (`10`, `11`, …), preventing
ambiguous concatenations. Exact manual numbers are reserved; generated labels
skip conflicts. The current phone collector supports at most five extension
digits. An unrepresentable tree fails explicitly instead of truncating numbers
or silently omitting directories.

Numbers are recomputed as the active tree changes. **Saved generated numbers can
therefore point somewhere different on a later call.** Use manual extensions for
contacts that must stay fixed. Once a caller selects a generated extension, the
PIN callback is bound to that route-map generation: renumbering during PIN entry
rejects the attempt instead of silently changing its destination. Already
connected calls are unaffected by directory disappearance or renumbering.

## Persistent operators and existing projects

An opaque identity derived from the canonical directory selects its saved
conversation. Renumbering does not replace that identity. Generated configuration,
PIN-independent SIP secrets, mailbox/queue state and logs live under the ignored
provider `state/directory-projects/` tree; no setup files are written into worker
directories. `operator.working_directory` sets the actual native tool workspace
separately from service metadata. Thread creation, resume and resets validate
that workspace.

When a directory has exactly one existing configured operator and all opted-in
recipients are already allowed to use it, that operator is reused. Its prompts,
project mailbox and schedule remain intact. Otherwise a dedicated operator is
created. Home has a separate conversation unless a compatible home operator
already exists. Existing explicit session requests/schedules retain their targets;
enabling a home incoming default does not migrate old conversations or mailboxes.

The supervisor warms eligible endpoints and publishes only those whose voice
frontend heartbeat, local SIP observer and registrar acknowledgement are current. The real provider/media boundary
still requires a handset check. When a worker directory disappears, new incoming
admission is removed; its saved state and running operator are retained, so active
calls and ongoing work are not terminated. Up to 32 directory operators can be
owned per supervisor run. Retired endpoints are released on a controlled service
restart, preserving their conversations. The ordinary worker sessions are never
stopped by the directory supervisor.

## Enable and operate

In the provider's private `server.toml`:

```toml
[directory_extensions]
enabled = true
root = "/home/operator"
recipients = ["alice"]
mode = "smart"
interval_seconds = 15
tmux_socket = ""
```

The root defaults to the current user's home. Set it explicitly in a container.
Listed recipients opt into all generated directories under that root and the
home incoming default; other recipients retain their existing routes and access
rules. A manual extension continues to require that recipient's existing session
permission. These are shared-account operators, not separate OS identities.

```bash
voip --root /path/to/provider directories preview --json
voip --root /path/to/provider config apply --hosted-only
voip --root /path/to/provider directories run
```

`preview` reads tmux and configuration without creating threads, registering SIP
credentials or publishing routes. `run` provisions missing directory SIP users,
initializes each conversation once, supervises its phone processes and refreshes
the route map. It never originates a test call. `directories status --json` shows
the last observation, selection reasons, extensions, ready/pending identities,
resolved operator sessions and failures. Check the observation time.

The foreground `service run`/Compose stack starts the directory supervisor when
enabled. With the legacy per-component systemd installation, full `config apply`
also enables `voip-directories.service`; after hosted-only deployment, render and
install that unit before enabling it. Run only one directory supervisor for a
provider. Changes to opt-in recipients/root need a hosted config update and a
supervisor restart; changes in the tmux tree do not need redeployment.

Routing uses a private [Twilio Sync Document](https://www.twilio.com/docs/sync/api/document-resource),
conditional revision updates and application-enforced expiration. Only opaque
route/session identities and SIP usernames enter the document, not local paths,
PINs or transcripts. Expired/unavailable maps reject generated/default-home
admission; manual extension routing remains available. Background refreshes and
hosted lookups add Sync API usage. Provider credentials already need Sync access
for PIN delivery. The directory supervisor checks deployed source/configuration
hashes before provisioning anything.

## Container discovery

The container's tmux server cannot enumerate host workers. Run this read-only
host helper against the shared provider directory:

```bash
python3 scripts/directory-snapshot.py --root /path/to/provider \
  --output /path/to/provider/state/host-tmux.json --watch
```

Set `snapshot_file="state/host-tmux.json"` in `[directory_extensions]`. The host
helper writes a private snapshot every 15 seconds. The container rejects snapshots
older than 45 seconds or with a different root/mode. Run the helper under the host
user's supervisor for startup. No host tmux or Docker socket is mounted. The source
and provider paths still use the matching host/container mounts described in
[CONTAINERS.md](CONTAINERS.md). Native Codex executes tools in the host directory;
the container stores only the corresponding voice metadata.

## Verification and rollout

`test_directory_routes.py` covers inference, compression, wide forks, manual
collisions, stable identities, dead/linked panes, symlinks and error handling.
`test_directory_service.py` covers private workspace separation, durable identities,
recipient scope, stable deployment hashes, expiration and removal of admission.
`twilio/directories.test.js` exercises actual hosted handlers with a simulated
Sync response, including home defaults, PINs, manual precedence, outgoing calls,
expiration and map changes during authentication.

The native TTS/STT case is:

```bash
python3 scripts/test-features.py --run --case directory_workspace --timeout 180
```

It asks the real operator through audio to save its shell working directory, checks
the actual file against the selected workspace, and independently transcribes its
spoken confirmation. Use the isolated host/container recipe in [TESTING.md](../TESTING.md).
This does not place a phone call or prove dynamic carrier routing.

For an arranged handset test: enable one test recipient, verify home and a project
are ready, then call with PIN only and with the project's `extension#` followed by
PIN. Ask each operator to run `pwd` and name its current workspace. Add/remove a
worker in an isolated tmux server and check renumbering; leave an existing call
connected to verify continuity. Select an extension, change the map before PIN
submission, and require rejection. Recheck a manual extension and a recipient who
has not opted in. Stop refresh long enough for expiry; generated admission must
fail and manual routing must still work. Restore test configuration afterward.

See [on-demand voice lifecycle](ON-DEMAND-VOICE.md): healthy idle operators now remain in standby without a realtime connection.
