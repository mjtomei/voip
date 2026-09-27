# PIN generation and delivery

PIN authentication now uses a salted, peppered scrypt verifier. The service does
not retain a recoverable PIN after terminal delivery. A pending telephone
delivery keeps an AES-256-GCM encrypted copy so the phone prompt can repeat it.
The voice operator, mailbox, and Codex transcript never receive that PIN.

The existing recipient PIN is preserved by migration. `state/call-pin` is now a
JSON verifier, so `cat` no longer retrieves the code. A forgotten PIN requires
rotation. Keep `state/pin-master-key` in a secure backup: losing it prevents both
verification and decryption, and the service refuses to silently replace it.

## Generate and print once

Run in your own terminal, outside agent tooling or command logging:

```bash
python3 voip.py pin generate alice --delivery print
# Load credentials from the documented private file or process environment.
python3 voip.py config apply
```

The first command prints the generated PIN to stdout once. You choose how to
deliver it. Default length is eight digits; `--digits` accepts 6–12. Only the
verifier goes into the configured `pin_file`. There is no command to retrieve
the plaintext afterward. The application cannot erase terminal scrollback,
redirected stdout, screenshots, or logs made by the CLI caller.

Generation changes the local verifier. `config apply` activates it on the phone
service. Until deployment finishes, the previously deployed PIN remains active.
If deployment fails, rerun `config apply`; don't unnecessarily generate again.
For a new recipient, first configure its number, `pin_file` path, and allowed
sessions; the generation command creates a missing PIN file. Existing inline
PINs must first be migrated with `python3 voip.py pin migrate`.

## Deliver by telephone

```bash
python3 voip.py pin generate alice --delivery call \
  --request-key alice-pin-delivery-001 \
  --retry-delays 1m,5m,8m,60m
# Load credentials from the documented private file or process environment.
python3 voip.py config apply
python3 voip.py pin status alice-pin-delivery-001
```

This queues a dedicated access-code delivery from the server's Twilio number to
the configured recipient. It waits for deployment of the matching verifier and
handler. The existing `voip-requests.service` drives it; enable that service for
startup recovery. It does not need Codex, SIP, a microphone, or a free operator
session. It waits while that destination has an active Twilio call on this
account, rather than adding another call to someone already talking.

The phone flow is:

1. Offer **press 9, then pound (`9#`), to stop this PIN delivery**, before reading
   the PIN. This works without knowing or confirming the PIN, including when
   the configured destination was entered incorrectly.
2. Read the PIN digit by digit and ask the recipient to enter the **entire PIN**.
   The stop-retries instruction is announced only before the PIN, not afterward.
   All digits submit automatically; pressing `1` alone does not confirm.
   A PIN starting with 9 works normally; `9#` is only a one-digit submission.
3. Correct entry confirms delivery and ends the call. `9#` cancels delivery and
   ends the call without confirming. Wrong/empty entries repeat the prompt up
   to three times on that call, then hang up and remain eligible for retry.

The four example intervals mean 1 minute **after the first failed attempt**,
then 5 minutes after the second, 8 after the third, and 60 after the fourth.
Afterward, 60 minutes repeats indefinitely until confirmed or cancelled.
Unanswered, busy, failed, and answered-but-unconfirmed calls all retry. There
is no normal request TTL or attempt limit for PIN delivery. A restart resumes
the saved job; it does not create a catch-up burst. A known failure is timed
from when the dispatcher observes it, so an outage can delay retries.

`9#` saves a durable opt-out in Twilio Sync, even when this computer is offline.
It deletes the encrypted hosted prompt immediately. Local cleanup happens when
the dispatcher observes the receipt. Queued retries and stale callbacks cannot
resurrect that delivery. Cancellation covers **this PIN-delivery request**, not
unrelated agent calls or a deliberately newly generated delivery. To correct a
mistyped destination, cancel the old request before editing and generating a
new one. Calls already being submitted may briefly ring while cancellation
settles; the disabled hosted prompt prevents them from reading the PIN.

## Status, cancellation, and uncertain submissions

```bash
python3 voip.py pin status                    # local saved state; no secrets
python3 voip.py pin cancel alice-pin-delivery-001
```

Cancellation is durable locally before network work. `cancelling` means the
hosted receipt/phone cleanup is still pending. A terminal `confirmed` or
`cancelled` state has no local encrypted prompt; `settled: true` also establishes
that the phone attempt has ended. Calling `cancel` after confirmation preserves
confirmation. Neither answering nor a completed carrier call is confirmation.

A network failure during call creation blocks further dialing to avoid duplicate
calls. A later authenticated hosted webhook can recover the exact Call SID.
Otherwise inspect the Twilio call log and URL, then explicitly associate the
matching attempt:

```bash
python3 voip.py pin reconcile KEY --call-sid CA...
```

Never choose a new key to bypass an ambiguous attempt. If no call can be
identified, cancel the job and investigate the provider state; the system
intentionally offers no blind redial override. Missing previously published
receipts also block delivery rather than recreating a deleted prompt.

## Storage and provider boundary

- `pin_file`: scrypt verifier, salt, length, and a keyed duplicate-check tag.
  Parameters: N=16384, r=8, p=5, 32-byte output; HMAC-SHA256 pepper first.
- `state/pin-master-key`: random 256-bit master key, mode 0600, separate from
  recipient configuration. Purpose-specific keys are derived using HMAC-SHA256.
- `state/pin-deliveries.json`: durable job, immutable recipient/number, attempts,
  retry timing, and pending AES-GCM ciphertext. Each envelope has a random
  96-bit nonce and binds its delivery ID as authenticated associated data.
- Private Twilio access Asset: verifiers and routing, never plaintext PINs.
  `PIN_MASTER_KEY` is a protected Function environment secret. `CALL_PIN` is
  removed during deployment.
- Dedicated Twilio Sync Service: authoritative encrypted pending prompt and
  terminal receipts. Conditional revision updates prevent confirmation,
  cancellation, and old webhooks from overwriting each other. Terminal receipts
  retain no ciphertext. No Sync client tokens are issued; client ACLs are enabled.

The installer uses the existing Twilio API key and stores it as
`PIN_SYNC_KEY_SID` / `PIN_SYNC_KEY_SECRET` in the private Function environment
for receipt access. These credentials and the master key are not written into
public deployment/status output. This adds Functions/Sync API usage on the same
Twilio account; it needs no new account or OpenAI key. `cryptography` is required
for call delivery and is already installed on this machine.

This protects stored credentials; it is not end-to-end encrypted telephone
speech. Twilio must process the spoken PIN and keypad entry. No recordings or
application event logging are enabled, but provider diagnostics, historical
assets/backups, and callers' recordings are outside the application's erasure
guarantee. Migration replaces active plaintext sources without changing the
code; it does not retroactively erase historical copies. Rotate a PIN when
previous copies need to become unusable.

The verifier follows [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Hosted receipts use [Twilio Sync Documents](https://www.twilio.com/docs/sync/api/document-resource)
and conditional updates. The keypad flow uses [TwiML Gather](https://www.twilio.com/docs/voice/twiml/gather).

## Verification

Unit/integration tests cover Python/Node cryptographic interoperability,
tampered ciphertext, distinct PIN routing, full-PIN confirmation, `9#` opt-out,
wrong digits, replay, cloud revision conflicts, host restart, offline
cancellation, and ambiguous call creation. General requests additionally test
saved arbitrary retry intervals and a finite attempt limit.

`python3 scripts/test-pin-audio.py` runs the actual bundled IVR against an
in-memory provider, synthesizes its synthetic PIN prompt and terminal replies,
plays them through an isolated Linux audio device, captures the output, and
verifies speech with local Whisper without text hints. This is **not a carrier
or Twilio TTS test**. September 19 evidence:
`state/e2e/pin-ivr-20260919-181155/results.json` — prompt (including digits),
confirmation, and cancellation all passed. The earlier test launcher failed
before playback because it didn't resolve the local `paplay` binary; fixed by
using the project's executable resolver.

The September 21 prompt update removes the repeated stop-retries instruction
after the PIN. All 55 hosted-handler tests passed, as did the prompt,
confirmation and cancellation TTS/STT cases in
`state/e2e/pin-ivr-20260921-074938/results.json`. Inspection of the captured
prompt's STT found exactly one stop-retries announcement, before the PIN.

A real handset test remains: recipient opt-out before PIN entry; correct full
PIN confirmation; an unconfirmed hangup followed by retry; and checking that
neither confirmed nor cancelled deliveries ring again after daemon restart.
Use a temporary recipient or arrange the intended PIN rotation explicitly.

The September 19 deployment preserved the existing PIN and operator thread,
verified all four protected Functions and the verifier Asset, removed `CALL_PIN`,
and restarted the dispatcher. No PIN delivery is queued and no real call was
placed. Live Sync create/read/conditional-update/delete checks passed, including
rejection of a stale revision and removal of ciphertext on cancellation:
`state/e2e/pin-provider-20260919-181836.json`. The synthetic cloud document was
deleted afterward. Full Python checks: 185 passed, one optional check skipped;
Node hosted-handler checks: 47 passed. A subsequent focused run of 23 scheduler/provider
checks passed, including the added explicit retry-list schedule test.
