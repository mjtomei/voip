# Operator mailbox and call notices

Workers leave text messages in a **recipient + project** mailbox. A project is
the workspace directory selected by a session's registration. All sessions in
that directory share each person's mailbox; their conversations/audio stay
separate. The same person has different mailboxes in different project roots.
Add `--session NAME` to select the session/project and `mailbox --recipient NAME`
to select the person. `--to +E164` also works. Recipient options can go before or
after the mailbox action. Omitted recipient selection uses an identified live
caller, otherwise the configured project default (or sole eligible recipient).
Workers should always select the recipient explicitly.

Posting now queues a **nonpreemptive context notification** through the shared
[request dispatcher](REQUESTS-AND-SCHEDULES.md). `--notify preemptive` submits
context immediately; `--notify passive` only stores it. Posting does not request
a phone call. For a call agenda, use passive posting followed by
`call start --message ID`, or use `request call --file FILE` to post and enqueue
with a stable key. Preparation still happens before dialing.

```bash
cd /path/to/voip
python3 voip.py mailbox --recipient alice post --from claude:planner \
  --subject 'Deployment decision' --key deployment-42 --file /tmp/agenda.txt --notify passive
python3 voip.py mailbox list
python3 voip.py mailbox read deployment-42
python3 voip.py mailbox address deployment-42 --note 'Decision made; worker notified'
```

Use `--body 'Text'` for short messages or `--file -` to read multiline UTF-8 text
from stdin. File input avoids quoting code or shell syntax into a command.
`--key` is optional; when provided, identical retries return the existing message
without duplicating it or reopening it. Different content under that key is an
error. Sender names are descriptive, self-reported labels.

Reading and listing leave a message **unaddressed**. Only `mailbox address ID`
marks it addressed. Keep it open while a decision or required follow-up remains
pending. `mailbox reopen ID` restores that status; `mailbox list --all` includes
addressed messages and `mailbox count` prints the unaddressed count. Text,
metadata, and resolution notes persist in private `state/mailboxes/` files using
locked, atomic writes. Each file is bound to the recipient ID and phone number;
changing a person's number starts an empty mailbox while preserving the old
file. Message keys are unique within that person's project mailbox. Resolving a
message through any session in the project resolves that same message for all
its sessions. This does not cancel other calls or imply backend task completion.

Call preparation, scheduled agendas, notifications, connection counts, and
hangup commands all select the intended recipient. Even preemptive notifications
wait while a different person is connected to the target session. Caller/session
permissions still apply. These are routing boundaries, **not OS or conversation
privacy guarantees**: operators retain host access, and a shared conversation
can remember earlier calls. Use separate sessions when conversations should differ.

Legacy session mailbox files are not silently exposed or assigned. Inspect their
audience, then run `python3 voip.py --session NAME mailbox --recipient PERSON
migrate`. This explicitly assigns the whole old mailbox, retains message IDs,
timestamps/status/outcome notes, and archives the original as
`mailbox.json.migrated`. If old messages belong to several people, split and
review them before using this whole-mailbox migration. Conflicting message IDs
stop migration without overwriting an existing message.

## Connection messages

After the local SIP leg is established, the frontend supplies a text notice to
Codex's existing realtime conversation:

- Empty mailbox: “The caller is connected.”
- One pending message: “The caller is connected. There is 1 unaddressed mailbox message.”
- Several: “The caller is connected. There are 3 unaddressed mailbox messages.”

These counts are private context, not speech to read aloud. For incoming calls,
the nonempty notice also gives the command with the caller's `--recipient` for
checking the mailbox. A delayed hangup includes its original recipient too;
never substitute a newer caller or the default person. An empty
notice never mentions the mailbox. The count is taken when the notice is
submitted; later messages can trigger their own queued context notification.
For prepared outgoing calls, the operator has already read the selected bodies
in its ordinary backend turn. The matching connection notice includes its
prepared opening and notes, telling voice to begin with the actual topic and
question without another mailbox lookup. Raw worker bodies remain data for the
operator to interpret, not service instructions. Preparation never marks them
addressed.

Connection notices also include a nonzero count of scheduled calls skipped while
the scheduler was offline, scoped to the connected session and recipient. This is
separate from the unaddressed mailbox count: skipped occurrences create no agenda
message or catch-up call. The count is pending until the connection notice is
accepted; reconnecting that call does not announce it as new. Prepared openings
still come first. See [offline schedule handling](REQUESTS-AND-SCHEDULES.md#cron).

These are attributed service notices, not words spoken by the caller. They
provide context; a prepared outgoing connection explicitly requests its opening.
On an ordinary
phone call, the SIP leg opens after the existing PIN check. Ringing and answering
the phone before PIN verification do not produce a connected notice. Both
incoming and outgoing calls use this same event path.

Hangup produces a call-ended notice. It says the person is no longer on that
call and explicitly asks the operator to review messages discussed or handled
on that call. The operator checks whether each message's purpose was fulfilled
and uses `mailbox address ID --note 'Concrete outcome'` for completed messages
with no required follow-up. A notification-only message can be complete once
delivered and acknowledged. Messages with pending decisions, actions, questions,
follow-up, or uncertain outcomes remain unaddressed. Preparing, reading aloud,
and hanging up do not by themselves establish completion.

A saved realtime transcript is not guaranteed to be present in the backend's
working context after voice closes. The ended notice therefore supplies the
native threads that received connection notices for that call, plus the observed
connection/end times. Before treating an acknowledgment as missing, the operator
uses ordinary host CLI tools to inspect the saved transcript for that identified
call. It checks the caller's own words and attribution; timestamps can lag and
are not proof of audible delivery. Unavailable or uncertain evidence remains
pending. The service does not infer completion from speech or close messages itself.

This is an instruction for the operator to assess outcomes and use its normal
CLI tools; the service does not automatically close messages. A delayed notice
refers to its identified call, and repeats must preserve existing resolutions.
The notice also preserves ongoing work and pending questions and does not
authorize a callback. A monitoring outage alone is not reported as a hangup.

## Services and recovery

Baresip's pinned `ctrl_tcp` module listens only on `127.0.0.1:4444` (change
`sip.control_port` and rerun SIP configuration to select another local port).
`voip-call-events.service` consumes structured call events and takes read-only
call snapshots every two seconds. No public port or new Twilio resource is
needed. The control module permits local commands; keep its listener on loopback.
The same observer also handles [keypad reset commands](KEYPAD-RESETS.md), using a
separate recovery subprocess so call-state monitoring continues during resets.

Call state and notices are stored in private `state/call-events.json`. SIP
addresses, credentials, DTMF, and raw event payloads are not copied there. The
nonsecret outgoing request UUID is retained for agenda correlation. Calls
are identified by a hash of their SIP Call-ID. Duplicate or late events cannot
reopen a closed call. A snapshot after reconnect can recover an active call or
report that a previously active call ended at an unknown time.

The frontend submits queued notices through native `thread/realtime/appendText`
to the saved UUID. Reconnecting voice reasserts a still-active call and refreshes
the count; an obsolete connected notice is skipped if the call has already
ended. Events retain their original observation times and stable IDs. Submission
is recorded after the RPC accepts it. This experimental API does not provide an
end-to-end delivery acknowledgement: a crash between submission and saving can
repeat a notice with the same ID. RPC acceptance alone is not proof that the
model acted on it. The retained notice text is a local diagnostic record, not a
promise that every service notice appears in Codex's native transcript.

The observer is installed and enabled on this machine. To restore all services:

```bash
systemctl --user enable --now voip-audio.timer voip-operator.timer \
  voip-baresip.service voip-call-events.service
python3 voip.py operator unpause
python3 voip.py operator status
```

For a new installation, run `scripts/build-baresip.py`, `voip.py sip configure`,
and `voip.py render-systemd`, then install the generated units as described in
the README. Never initialize another operator just to restart these services.

## Validation on this installation

The subsequent prepare-before-dial handset test also passed on September 18:
the same operator prepared the agenda before dialing, the matching connection
received its opening, and the primary user confirmed hearing “harbor lantern.” No backend
tool calls were recorded during the call. The test message was addressed only
after that verification. This checks receipt and the absence of a lookup during
the call; saved transcript timestamps are not an audible-latency measurement.
See [prepared opening validation](OUTGOING-CALLS.md#prepare-before-dial-validation-september-18-2026).

On September 18, 2026, the combined mailbox/outgoing-call handset test verified:

- Identical posts return one message; reading leaves it unaddressed.
- The connected notice reports the pending count, and the operator can retrieve
  the worker's text through ordinary CLI tools. the primary user heard the expected test
  phrase over the outgoing phone call.
- Reading and hangup do not resolve the message. After the primary user confirmed the
  readback in text, a local TTS request asked the operator to finish. The operator
  wrote the fresh proof value from the mailbox to a local file and marked the
  message addressed. Final acknowledgement was tested through local audio,
  rather than another handset command.
- Reposting an addressed message with the same content/key does not reopen it.
  The mailbox ended empty; the same operator stayed connected across hangup.

All 74 Python and 32 JavaScript tests passed. Private evidence and the test's
scope are recorded in `state/outgoing-mailbox-acceptance.json`; see the
[outgoing-call acceptance record](OUTGOING-CALLS.md#handset-acceptance-september-18-2026).
For task completion, rely on mailbox status and the recorded outcome rather than
a conversational acknowledgment such as “all set.”

In earlier validation, all 52 Python tests passed.
`scripts/check-mailbox-calls.py` made two three-second
Twilio calls to the local SIP endpoint, without dialing a handset. Both produced
connected and ended notices; the first omitted the mailbox and the second
reported one unaddressed message. A spoken request then caused the operator to
read the message, write its fresh challenge using a shell tool, and mark it
addressed. The mailbox returned to zero unaddressed messages and the original
voice session remained connected. Evidence is in `state/mailbox-acceptance.json`.

References: [Baresip control and event protocol](https://github.com/baresip/baresip/blob/v4.11.0/modules/ctrl_tcp/ctrl_tcp.c),
[Codex App Server](https://learn.chatgpt.com/docs/app-server).
