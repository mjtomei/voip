# Callbacks, missed calls, and explicit transfers

Plain PIN entry returns a caller to the latest session that placed a call to
that recipient within the last three hours, until one callback successfully
connects to that operator. That success consumes the latest attempt's callback
preference; later ordinary calls return to the usual incoming route (the home
operator for directory-enabled recipients). Older outgoing calls are not revived.
Every actual outgoing retry is a new phone call and starts a fresh window.
Status updates, preparing an agenda,
reusing an existing call, and PIN-delivery calls do not refresh it. At three
hours the usual incoming route applies. Entering `extension#` before the PIN
always takes precedence. Callback routing never bypasses the personal PIN or
current session permissions. Generated routes are checked against the current
live directory map rather than historical extension numbers.

PIN entry alone, an unanswered SIP dial, or an explicit-extension call does not
consume the callback preference. Successful SIP connection is recorded by the
local dispatcher (normally within 15 seconds), or the protected Dial completion
callback. Consumption is durable and tied to the original outgoing attempt, so
late callbacks cannot consume a newer retry. Cloud journal outages can delay
this change until synchronization recovers.

PIN verification belongs to the telephone keypad service. The voice operator
must never request a spoken PIN or invent a separate session PIN. Connection
notices explicitly distinguish the current verified call from earlier attempts
that did not pass the PIN gate; transfers retain the existing authentication.

The callback choice is bound when the PIN prompt is issued. If another session
calls during PIN entry, that old selection is rejected instead of silently
connecting a different session. An unavailable journal or removed/unauthorized
recent target falls back to normal entry routing. This is a routing convenience,
not an additional authentication method.

On a new incoming connection the operator is instructed to greet immediately:
"You're with the [directory] operator. How can I help?" It identifies the
directory before mailbox lookups, rather than reading opaque session IDs or
authentication metadata. A same-call voice reconnect does not repeat that
greeting. Native speech remains model-generated, so handset verification is
needed in addition to prompt and notice tests.

## Call history and missed-call context

The first authenticated callback that reaches the original session also carries
that outgoing attempt's prepared agenda, provided it is still pending. The
protected PIN handler sends the original and current call IDs in the SIP-only
caller identity; agenda text and PINs remain local. This avoids waiting for the
periodic cloud-history poll before greeting the caller. An explicit extension
does not carry a callback agenda. The usual three-hour callback window applies,
including a new window for each retry.

The connection notice gives the operator the existing opening and context before
it speaks. It labels them as an earlier snapshot with its preparation time,
rather than claiming a fresh review. The brief is withheld if the mailbox
message changed or was addressed, the configured recipient/number changed, the
saved conversation was reset, or the attempt no longer matches the session.
The ordinary greeting and pending-mailbox flow still work in these cases.
Persisted SIP connection events limit the opening to the first matching callback;
concurrent or later callbacks cannot reuse it. Reconnecting audio on that same
call restores context without asking for a repeated opening. No callback marks
the agenda addressed merely by connecting or reading it aloud.

Reproduce the deterministic cross-language regression with
`node --test twilio/call-routing.test.js` and
`python3 -m unittest discover -s tests -p test_callback_agenda.py -v`.
The Node test runs the actual PIN handler and feeds its SIP caller identity into
the Python connection-notice code using synthetic recipients. Native voice
acceptance adds `scripts/test-features.py --run --case callback`: real preparation,
simulated failed outgoing attempt and authenticated callback, STT verification
of the prepared spoken topic without another backend lookup, then a TTS request
to repeat the earlier reference with STT verifying that same reference in the
answer. The capture waits for output silence before connecting, so residual
preparation speech cannot satisfy the callback assertion. See [TESTING](../TESTING.md) for the
isolated container setup. This does not replace real handset acceptance.

Protected Functions and the local dispatcher share a private Twilio Sync journal
per recipient. It contains call/session IDs, direction, attempt times, provider
status and PIN/connection flags, never PINs, agendas, transcripts or phone numbers.
It retains at most 100 calls and seven days per recipient. This cloud journal
allows callback selection while the local dispatcher is restarting. Journal
updates use conditional revisions to avoid overwriting concurrent calls.

The provider dispatcher polls roughly every 15 seconds and stores a local cache.
Ended calls with no recorded operator connection produce durable, deduplicated
missed-call notices after a 30-second callback-settling interval. Caller ID alone
is explicitly unverified. Late provider events can correct incomplete history;
provider status is not proof that audio was heard. PIN-delivery calls are excluded.

```sh
python3 voip.py --session default call history --recipient alice
```

History is scoped to the selected session and optional recipient. Missed-call
notices use ordinary text delivery while idle, without opening realtime voice.
They wait during active calls; connection context includes recent unsuccessful
attempts for the actual caller. A notice does not authorize calling back, marking
mail addressed, or cancelling tasks. Network/Sync failures leave updates pending;
local attempt refresh retries publishing without placing another call.

## Caller-requested transfers and hangup

The operator uses the call identity from its current connection notice:

```sh
python3 voip.py --session default call current
python3 voip.py --session default call transfer --call-id CALL_ID \
  --to-session project --request-key caller-request-42 \
  --note 'Caller wants to discuss the project plan'
python3 voip.py --session default call transfer-status caller-request-42
python3 voip.py --session project call hangup --call-id NEW_CALL_ID
python3 voip.py --session project call transfer --call-id CALL_ID \
  --to-home --request-key return-home-42 --note 'Caller requested home'
```

The destination is a registered session ID, including an active generated
`dir_...` identity, or a transfer alias described below. Resolve a directory with
`directories status`. Access checks
use the connected recipient, not a supplied recipient override. The service
checks the exact parent phone call, current source identity, destination health,
permissions and occupancy, then opens the destination's voice connection before
redirecting the existing phone leg. No new telephone call or PIN entry is needed.
For generated destinations, warming voice can overlap a directory refresh.
The CLI therefore waits for a fresh hosted directory entry naming the exact
destination endpoint before redirecting. It polls for up to two refresh intervals
plus five seconds, bounded to 20–60 seconds, subject to an in-flight provider
request's timeout. If admission remains unavailable, it rejects the transfer,
releases its warmup demand, and leaves the source call connected. It rechecks
the source call and destination occupancy, observer, and health after waiting.
The transfer record's `route_check` records the latest sanitized publication
result and directory generation.
Only caller-authorized context should be included in the brief handoff note.
`--to-home` resolves the current, published home-directory route and keeps all
normal recipient access, health and occupancy checks. With directory routing
enabled, `--to-session default` also resolves to that home route. A missing or
stale home route rejects the transfer and leaves the caller connected; it never
falls back to the provider project's operator.

The provider project's operator remains reachable by its directory name (for
example `--to-session voip`), unless that name already identifies another
registered session. No directory-confirmation option is required. These are
transfer destination aliases: saved session IDs, conversation history, service
processes, source `--session` selectors, queues and mailbox ownership are unchanged.
With directory routing disabled, `default` retains its existing behavior.
New transfer records save both the requested alias and canonical destination.
Reusing a request key checks the saved destination and does not resolve the alias
again or repeat the redirect, including for older records predating aliases.
Operator startup context includes its directory, spoken label and transfer name.
On arrival, the destination's connection notice supplies the source session,
caller identity and note. Its prepared spoken opening confirms the transfer and
names the destination directory, for example "Transfer complete. You're now
with the home directory operator." This confirmation follows the destination's
SIP connection; merely submitting the redirect does not produce it. A voice
reconnect on that same call does not intentionally repeat the confirmation.

Spoken transfer requests must invoke Codex's backend, which executes the CLI;
a voice acknowledgment alone performs no transfer. The connection notice repeats
this requirement and supplies the current call's home-transfer command. The
instruction to speak the prepared opening before using tools applies only to that
opening, not the caller's next request. Native voice can still fail to delegate:
check the transfer ledger and backend tool records before blaming SIP or treating
“I'll transfer you” as a submitted attempt.

A submitted redirect is not confirmed arrival. `transfer-status` distinguishes
an observed target connection, ended phone call, and pending/returned state.
Ambiguous API outcomes retain the original key and block replacement attempts;
inspect status before retrying. The protected handoff URL has a short-lived
signature bound to this exact phone call, recipient, source and destination.
A busy/no-answer/failed target SIP dial returns to the original session when it
is still reachable. A failed target lookup before dialing now takes that same
return path and announces what happened instead of silently hanging up. For a
generated source, the short-lived signed transfer ticket includes the exact
source endpoint just verified against the live call. Recovery can use that
endpoint during a directory outage only while the caller still has access under
the same directory policy. This recovery snapshot never authorizes a destination,
extends ticket validity, bypasses identity checks, or creates a retry loop. Older
tickets without a snapshot require the source to remain resolvable. If the source
cannot be authorized/resolved either, the handler explains the failure before
ending the call. A SIP failure while dialing the source can still end the call.

Hosted handoff logs record a transfer ID, stage, internal reason and, when
available, directory generation and target presence. They omit phone numbers,
PINs, submitted context, credentials, and signed URLs. `target_resolved` means a
Dial was constructed; it does not confirm that the destination answered.

There is a race between preflight and another incoming call;
SIP's one-call-per-session limit prevents mixing callers. Legacy incoming calls
without a bound provider identity must reconnect once before using call control.

Transfers end the source session's local call and trigger its normal mailbox
review. They do not cancel its tasks. The destination keeps its own conversation.
`call hangup` ends only the identified telephone call; a response merely accepting
the request is not proof of audible termination.

## Validation

`tests/test_call_routing.py` covers journal conflicts, durable and scoped missed
notices, exact phone-call identity, unknown transfer outcomes and actual bundled
Function syntax. `twilio/call-routing.test.js` exercises the real handlers with
an in-memory Sync service: callback windows, retries, extension override, wrong
PIN, revoked access, selection changes during PIN entry, delayed status callbacks,
signed transfers, replay onto another call, and return to the source after failure.
Transfer regressions also cover warmup/publication ordering, admission timeout,
stale policy or wrong endpoint, source/target changes while waiting, lookup
failure before Dial, generated-source recovery during a cloud outage, revoked
recovery permission, bounded fallback, chained handler invocations, and log
redaction. The chained test verifies handler isolation; real provider callback
ordering still requires handset acceptance.
Run these through the clean-container suite documented in `TESTING.md`.

For handset acceptance, arrange two accessible sessions for one recipient:

1. Request an outgoing call from the non-default session, decline it, then call
   the shared number and enter the usual PIN. Verify that session answers and
   does not ask for a spoken PIN or another authentication step.
2. Repeat using an explicit extension for the other session. Verify override.
   After a successful callback, an ordinary call should instead use the normal
   incoming route. A new outgoing retry should create a fresh callback preference.
3. Ask the operator about the missed attempt; verify direction and context,
   without assuming that a voicemail or unverified caller heard anything.
4. Ask to switch sessions with a harmless reference phrase. Verify the phone
   remains connected, the destination knows the phrase and caller identity,
   and the original operator releases voice without cancelling unrelated work.
   Also ask specifically for home and verify its working directory and spoken
   arrival announcement; do not treat the profile name `default` as home.
5. Try an occupied or unauthorized destination; verify no callers are mixed.
   Exercise a controlled failed target and verify return to the source.
6. Ask to hang up; verify the phone ends and both operators return to standby.

The three-hour expiry and retry refresh use deterministic clock tests; carrier
behavior requires the arranged handset checks. A simulated provider does not
establish telephone acceptance. Native speech timing must be measured from audio,
not transcript-save timestamps.

Provider mechanisms: [updating a live phone call](https://www.twilio.com/docs/voice/tutorials/how-to-modify-calls-in-progress),
[SIP Dial results](https://www.twilio.com/docs/voice/twiml/sip), and
[conditional Sync document updates](https://www.twilio.com/docs/sync/api/document-resource).
