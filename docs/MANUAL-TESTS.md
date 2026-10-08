# Agent and handset test runbook

Start from the container and native-audio prerequisites in [TESTING](../TESTING.md).
This runbook replaces dependence on the original operator's remembered actions.
The test driver can be a person or coding agent using the same CLI. Arrange real
calls with recipients first. Choose a disposable provider installation or a
maintenance window, preserve existing configuration, and never publish raw logs.

The speech-check scripts require the test image's Whisper binary/model. The
runtime image deliberately omits them. To add verification tools to an existing
runtime deployment without changing its image, copy them from the matching test
image into private mounted state:

```bash
# VOICE_WORKSPACE is the absolute private workspace mounted into the running service.
mkdir -p "$VOICE_WORKSPACE/state/acceptance-tools/bin" "$VOICE_WORKSPACE/state/acceptance-tools/share"
docker create --name voice-stt-source voice-operator:test
docker cp voice-stt-source:/opt/voip/.tools/bin/whisper-cli "$VOICE_WORKSPACE/state/acceptance-tools/bin/"
docker cp voice-stt-source:/opt/voip/.tools/share/whisper "$VOICE_WORKSPACE/state/acceptance-tools/share/"
docker rm voice-stt-source
# Prefix the arranged check commands below with:
docker exec -e VOIP_TOOLS_DIR="$VOICE_WORKSPACE/state/acceptance-tools" \
  -w /opt/voip voice-operator python3 scripts/check-phone-audio.py --help
```

Keep the binary/model checksums with the evidence. This per-command override
does not alter the running service's tools or mount host audio into it.

## Fixture and evidence

Use recipients `alice` and `bob` with their real, explicitly authorized phone
numbers and different privately delivered PINs. Allow both to access sessions
`default` and `daily`; give them separate incoming defaults. Start both profiles,
assign distinct extensions (for example 100 and 200), and verify both report healthy voice standby
and current SIP registration/observers before dialing. Each person/project mailbox is independent.
For project sharing, register a second project with two named sessions, put its
agendas/state there, and allow the intended recipient to access both sessions.

Create a private run directory with timestamp, source revision/hashes, image ID,
Codex version, configuration with secrets removed, and an observations file.
Use a unique prefix such as `handset-YYYYMMDD-01` for this arranged run. Never
record entered PIN digits. For every case record request key, session/recipient,
call identity, native thread ID, times observed, expected/actual result, STT
artifact path, recipient's exact confirmation, cleanup and unresolved work.

Prepare agenda files inside the test project. Example `blue.txt`:

```text
This is an arranged notification-only test. Immediately after the matching call
connects, say "blue lantern" and ask the caller to acknowledge hearing it.
The purpose is complete only after that acknowledgment. Keep the call open.
```

Copy with phrases `green notebook`, `orange harbor`, `silver meadow`, `copper
forest`, `amber river` and `violet garden`. For the decision message, ask whether
to use morning review or evening review; completion requires an actual decision.
Use `mailbox post --notify passive` when pairing a message with a call, avoiding
an extra independent notification. The operator prepares its own brief; the test
driver must not impersonate it by supplying a ready brief.

## Sequential handset procedure

Commands below run in the configured test project/container. `--session` and
`--recipient` must be explicit when more than one profile/person is available.
Use the same request key when inspecting/retrying a case, never a fresh key to
bypass an unresolved attempt.

1. **Prepared opening.** Run `scripts/check-phone-audio.py --run-telephony
   --session default --recipient alice --request-key handset-YYYYMMDD-01-blue
   --agenda /absolute/path/blue.txt --expect 'blue lantern'`. Answer, enter the
   private PIN, acknowledge the phrase and stay connected. Require ready
   preparation before dial, PIN/SIP connection before verified audio, independent
   STT and the handset user's confirmation. The test leaves a successful call up.
2. **Priority topic without a new ring.** Post `green.txt` with
   `mailbox post --session default --recipient alice --from test --subject green
   --key handset-YYYYMMDD-01-green --file FILE --notify preemptive`. Capture using
   `check-phone-audio.py --run-telephony --session default --expect 'green notebook'`
   before dispatch where possible. Require unchanged call identity/thread and no
   new provider attempt. Acknowledge the topic and keep the call open.
3. **Ordinary decision and hangup review.** Post the decision with
   `--notify non_preemptive`; say explicitly to leave it undecided. Acknowledge
   the notification-only topics, then hang up. Observe for 90 seconds. Inspect
   the identified ended notice and each mailbox record. Notifications require
   an addressed outcome note; the decision must remain unaddressed. An accepted
   ended notice without actual review is **failure**, not completion. Do not
   manually repair the mailbox and call the automatic test passed.
4. **Incoming and bad PIN.** Call the service from each allowed phone with an
   intentionally incorrect code; require rejection before any SIP connection.
   Repeat with the other person's code, then the correct private code. Require
   only the correct person's route/greeting. Do not read PIN files into logs.
5. **Keypad recovery.** On an authenticated call save `voip status --json` and
   the selected profile's `keypad-last-command.json` and `operator-reset.json`
   from its private state directory. Ask the recipient to enter `*1#`, wait for
   completed recovery, then ask the operator to say "frontend recovery verified".
   Capture with `check-phone-audio.py --run-telephony --session SESSION
   --expect 'frontend recovery verified'` before requesting that phrase.
   Require the same call/thread. Repeat with `*2#` and "session recovery verified".
   Arrange the destructive fresh test explicitly; enter `*3#` and require a new
   UUID, old conversation backup, same phone call and "fresh conversation verified".
   Inspect the matching reset record and independent STT, and obtain handset
   confirmation separately. Wait for recovery and the 15-second command cooldown.
   `check-keypad-call.py --run-telephony --scope frontend|session|fresh` is a
   **separate SIP-only** automated provider test: it originates its own billable
   call, sends DTMF itself and requires no existing call. It is not a monitor for
   this handset procedure. Its expected spoken phrase is "voice recovery verified".
6. **Scheduled same-call topic.** While the call remains up, add one temporary
   enabled cron definition targeting the next local minute, same recipient/session,
   `orange.txt`, mode non_preemptive and `misfire="skip"`. Use `schedule next` to
   confirm timezone/due time. After its occurrence appears in `request status`,
   remove/disable the schedule immediately. Require "orange harbor", same call
   and no new dial. To test waiting, repeat while a controlled native tool is
   active; require the ordinary topic to wait. Priority must deliver context
   without claiming to cancel that tool. The native `backend_requests` case
   provides a reproducible file-controlled waiting tool fixture.
7. **Two numbers concurrently.** Call alice/default with `silver.txt`; wait for
   completed PIN entry and acknowledgment before calling bob/daily with `copper.txt`.
   This avoids timing out the first phone's PIN collector while answering the
   second. Keep both connected. Ask each its session identity; require separate
   topics/audio and distinct threads. Cancel only the second request; verify it
   settles while the first call stays connected.
8. **Two sessions, one number.** With alice/default still active, call alice/daily
   with `amber.txt`. Answer via call waiting, enter alice's PIN and acknowledge
   "amber river". Require the first call held rather than ended, separate native
   threads, and no bridge/merge. Mark unsupported if the carrier cannot provide
   call waiting; do not report application failure or pass without evidence.
9. **Retry schedule.** End existing calls. Configure a test profile's redial
   `retry_on=["busy","no-answer","ended_without_sip_connection"]` and make an
   arranged call with `--retry-delays 1m,5m,8m,60m` (TTL must cover the test).
   For the short acceptance, use `--max-attempts 2`, answer then hang up **before**
   the PIN, and answer/confirm the first retry to hear "violet garden". Measure
   delay from observed failure, not original dial. The full interval series can
   be tested by leaving each attempt unconfirmed; collect actual timestamps.
   Connected normal hangup must never redial. Uncertain provider outcomes must
   block rather than duplicate calls.
10. **Extensions and saved contacts.** From each allowed phone, dial `200#` before
    that person's PIN; require the allowed registered session and no post-PIN
    menu. Try a disallowed/unknown extension. Save a contact with handset-specific
    pauses/waits and the extension/PIN sequence; redial it and record device/OS,
    digit timing and actual route. Never put that contact string into evidence.
11. **Mailbox project scope.** Post a message to bob/project through its first
    session. Read/address it through the second session and verify the outcome is
    shared. Alice's mailbox and bob's other project must not receive it. During
    delayed hangup review, the notice's old recipient must be used even if another
    caller has connected. See `test_recipient_mailboxes.py` for deterministic races.
12. **Callback preference and consumption.** Arrange an outgoing call from a
    session other than the recipient's normal incoming route, then end it.
    Call the shared number without an extension and enter the usual PIN. Require
    the original calling session's automatic greeting. End that callback and
    dial again without an extension; require the normal incoming route and its
    greeting. Record both actual session/thread identities and the provider
    history's callback-consumption record. Separately verify that an explicit
    allowed extension overrides an unconsumed preference. Retry attempts refresh
    the window; deterministic clock tests cover the three-hour expiry.
13. **Caller-requested transfer.** Keep an authenticated call connected and ask
    the operator to transfer to another allowed session, carrying a harmless
    reference phrase such as "silver compass". Require the same parent phone
    call, no extra PIN, an arrival announcement identifying the destination and
    correct reference context. Ask to transfer back, including specifically to
    home when directory routing is enabled. Inspect the transfer records and
    actual destination, rather than accepting a spoken promise as completion.
    Capture each destination before transferring for independent STT. The
    [routing runbook](CALL-ROUTING.md) covers missed-call context, occupied or
    unauthorized targets, controlled fallback and caller-requested hangup.

## PIN-delivery telephone tests

Arrange a real PIN rotation on a test recipient. From a private terminal or
agent command use `pin generate RECIPIENT --delivery call --request-key KEY`,
deploy the matching verifier, then start its dispatcher. The IVR offers `9#`
before reading the code, reads it digit-by-digit and asks for the entire code.

- Enter the full delivered code; require hosted and local `confirmed`, removal
  of pending encrypted prompt, settled phone cleanup and no later retry.
- In a separate authorized rotation, press `9#` before hearing/confirming the
  code. Require cancelled (not confirmed), encrypted prompt removal and no retry.
- In another arranged attempt, hang up unconfirmed; require the scheduled retry.
- For offline opt-out, wait for a delivery call, stop the local dispatcher, press
  `9#`, restart it and require reconciliation without another dial. Record the
  hosted receipt; never include encrypted bodies or verifiers in the report.
- Restart after both confirmed and cancelled states; neither must ring again.

These are separate real rotations; never reset a normal user's code simply to
exercise the matrix. Local TTS/STT validates prompt text/audio but not Twilio's
spoken voice or telephone DTMF transport.

## Caller-directed schedule and prompt changes

Use an isolated project with a writable operator and the synthetic reference
configuration, then provision an arranged test recipient as above. Save the
original schedule/profile/prompts. Do not alter a normal user's real meetings
for this test without their request. Local regression tests are in
`test_voice_config.py`, `test_call_events.py` and `test_call_schedule.py`.

1. On an arranged call, say you now work weekdays 08:00-17:00 and want calls
   outside those hours. Require the operator to clarify a desired exact time
   and what happens to other check-ins rather than guess or just record a note.
   Choose a weekday 18:00 recurrence in the configured timezone, effective now.
2. Inspect the saved schedule and `schedule next`: the intended recipient/session
   should have weekday 18:00 dates and any explicitly unwanted check-ins should
   be disabled. Other recipients must be unchanged. The same phone call and
   native conversation must remain connected; no PIN re-entry or reset.
3. Ask for a distinctive meeting opening, such as "Begin our next journal review
   with a one-sentence priority question." Check the selected prompt/agenda edit
   and validation. On the next arranged call, verify the requested behavior with
   handset observation or the documented TTS/STT capture; do not infer it from
   a saved template or an assistant's promise alone.
4. Ask to pause future meetings. Verify `enabled=false` and no next occurrence
   for those entries. If an isolated owned request is queued/retrying, explicitly
   ask to withdraw it and verify cancellation. The current connected request
   must not be cancelled as part of pausing its recurrence.
5. Ask for a change starting on a future date. Require an honest explanation of
   cron's effective-date limitation and an explicit pending note or an agreed
   supported immediate change, not a claim that a future note prevents calls.

Restore the saved configuration and validate next occurrences. Cancel/settle
owned test requests and preserve evidence without retaining credentials. These
steps require an arranged handset test; automated configuration tests do not
establish that a caller heard or successfully requested the change.

## Restart, network and cleanup

With an idle test stack, restart the container and verify the saved conversation,
SIP registration and dispatcher return. Repeat a controlled container audio
restart. For a network test, disconnect/reconnect the test container's network
only; preserve the uncertainty ledger and inspect before any redial. Do not
change the host's live network or interrupt unrelated sessions for this test.

For offline cron, set one due occurrence, stop the stack across that minute,
restart and verify skip/no catch-up/count-on-next-call. A running dispatcher
with a network outage is a different case: the queued request can wait to TTL.

Remove temporary schedules first, cancel every owned pending request/delivery,
verify settlement, end owned calls, stop only test services, and restore saved
routes/profiles through checked deployment when no affected call is active.
Keep private evidence locally. Produce a redacted result table with pass, fail,
not-run or unsupported, actual commands, boundaries and unresolved cleanup.
