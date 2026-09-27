# Twilio call entry

**Current configuration:** use `server.toml`, `recipients.toml`, and session
profiles with `python3 voip.py config apply`. See
[configuration](../docs/CONFIGURATION.md) and
[requests/scheduling](../docs/REQUESTS-AND-SCHEDULES.md). The API provisioning
script now bundles the shared access helper into four protected Functions and
deploys recipient PIN verifiers/session routes in a private Asset. The manual Console
steps below describe the original single-recipient deployment; do not upload
unbundled current handlers or treat those legacy environment fields as the
current access map. No new Twilio account or OpenAI API key is required.

These are Twilio-hosted Functions, deployable through the API or Console. They authenticate incoming callers/outgoing recipients and dial the registered Baresip
endpoint; the separate `/pin-delivery` IVR delivers codes and saves confirmations or opt-outs. They have no local
operator lifecycle hook and do not start or restart Codex.

## API setup on this machine

`scripts/twilio-setup.py` provisions incoming and immediate outgoing routing. It
accepts `TWILIO_SID` / `TWILIO_SECRET` for an API key, or the explicit aliases
`TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET`. The account SID is obtained from
the dedicated Functions Service, so a Standard key does not need Accounts API
permissions. Credentials are read from the environment and never printed or
written into deployment state.

```bash
source ~/.profile
python3 scripts/twilio-setup.py prepare
python3 scripts/twilio-setup.py verify
# After checking Baresip registration:
python3 scripts/twilio-setup.py route
```

`prepare` creates/reuses this installation's named Service, production
environment, secure SIP Domain, and Credential List. It attaches the credentials
to registration and call authentication, deploys all four Protected Functions, and
writes local Baresip configuration. It leaves number routing unchanged. `verify`
checks the deployed code hashes, visibility, environment values, domain, and
authentication mappings. `route` attaches the selected owned number to the
verified entry point and refuses to overwrite another webhook/application/trunk.
No command buys a number or originates a call.

Private inputs and recovery state are under `state/`, mode 0600:

- `twilio-call-settings.json`: `phone_number` and a nonempty `allowed_callers` list,
  all in E.164 format.
- `call-pin`: migrated PIN verifier, not recoverable plaintext. See
  [PIN generation and delivery](../docs/PIN-DELIVERY.md) for rotation and confirmed delivery.
- `pin-master-key`: verifier pepper and delivery encryption master key; back it up securely.
- `pin-deliveries.json`: saved encrypted pending deliveries and terminal status.
- `twilio-sip-secret`: generated dedicated SIP password, also written to
  `baresip/accounts`. Losing it requires credential recovery, not silent rotation.
- `twilio-provisioning.json`: resource IDs, code hashes, deployment identity,
  and prior number routing, for inspection/reuse after interruption.
- `twilio-call-request-secret`: outgoing admission-ticket signing key.
- `outgoing-calls.json`: durable outgoing attempts and private admission tickets.

Outgoing usage and recovery are in [OUTGOING-CALLS.md](../docs/OUTGOING-CALLS.md).
The setup commands do not originate calls; `voip.py call start` does.

The hosted build uses Node.js 22, Twilio SDK 6.1.1, and Runtime Handler 2.0.1.
Twilio's hosted Runtime Handler versions differ from npm's local-development
versions; select from the [hosted version list](https://www.twilio.com/docs/serverless/functions-assets/handler).
API transport failures require checking the saved/remote state before retrying;
the script does not blindly retry failed POST requests. It never logs raw API
error messages, request bodies, or Function variables.

## Legacy Console setup (historical; use API setup for current deployment)

1. Obtain a voice-capable number in a billing-enabled Twilio account. Keep the
   number, SIP Domain, Credential List, and Functions in that same account.
2. Create a **Programmable Voice SIP Domain**, enable SIP registration, create
   a Credential List containing the operator SIP username/password, and associate
   that list with the Domain's registration authentication and Voice authentication.
   Enable Secure Media and keep the initial setup in the US1 Region.
   Use the general domain, for example `your-name.sip.twilio.com`, consistently
   in Baresip and the Functions. Twilio recommends omitting region/edge suffixes
   when dialing a registered endpoint; our initial registration uses its default
   Ashburn edge. This uses Programmable Voice registered SIP endpoints.
   [Twilio SIP registration](https://www.twilio.com/docs/voice/api/sip-registration),
   [Domain settings](https://www.twilio.com/docs/voice/sip/api/sip-domain-resource).
3. Create one Functions Service with two **Protected** Functions: `/incoming`
   containing `incoming.js`, and `/authorize` containing `authorize.js`.
   Protected visibility makes Twilio validate the webhook signature. It is
   required: these handlers rely on that validation and must not be deployed
   as public endpoints. [Function visibility](https://www.twilio.com/docs/serverless/functions-assets/visibility).
4. Set the Function environment variables below and deploy both Functions in
   the same Service/environment. The Twilio runtime provides the `Twilio` helper.
5. Point the phone number's incoming Voice webhook to the deployed `/incoming`
   URL using POST. Point the SIP Domain's incoming Voice webhook to `/incoming`
   too: SIP-originated requests have a SIP address as `To`, and our handler
   rejects them. There is no outbound dialing flow.
6. Enter the SIP domain and username in the project's `config.toml`. Run
   `python3 voip.py sip configure` locally to enter the SIP password, then start
   Baresip and verify successful registration before attempting a call.

| Variable | Value |
| --- | --- |
| `TWILIO_NUMBER` | Purchased phone number, E.164, e.g. `+12025550100` |
| `ALLOWED_CALLERS` | Your mobile number in E.164; comma-separated if more than one |
| `CALL_PIN` | Separate 6–12 digit access PIN, entered using the phone keypad |
| `SIP_DOMAIN` | General SIP Domain matching Baresip, e.g. `your-name.sip.twilio.com` |
| `SIP_USERNAME` | Dedicated registered endpoint username |

On this machine, the SIP password belongs in the private Baresip account file. Neither Function
needs that password or makes a REST API call. No Twilio Account Auth Token or
OpenAI API key needs to be copied into this project for this Console setup.

## Call behavior

The entry handler checks the caller and called number before prompting for a
PIN. Missing input hangs up. The authorization handler repeats those checks,
compares the PIN, and dials the configured SIP address with TLS and SRTP. The
Twilio-to-Baresip SIP URI explicitly includes `secure=true`; `transport=tls`
alone does not enable media encryption. The Domain's Secure setting controls
the opposite direction. [Twilio Secure Media](https://www.twilio.com/docs/voice/api/secure-media).
Incorrect input
hangs up with no retry loop. Caller ID is one check; the PIN supplies another.
The PIN is not played into the Codex audio stream. Do not log `event.Digits`.

Dialing has a 20-second answer timeout and a four-hour connected-call limit.
Recording is explicitly disabled. If the SIP endpoint ends the call, the next
TwiML instruction hangs up the caller leg. Telephone hangup leaves SIP registration
and the Codex operator running.

One active call is enforced in Baresip. Both Baresip's registrar-origin filtering
and the Domain's call-entry behavior need validation so direct SIP traffic cannot
bypass the caller authentication. Do not expose another call-routing endpoint
that directly dials this operator.

## Deployment checks

The JavaScript tests verify generated TwiML and reject unknown/missing callers,
wrong destinations, invalid PINs, and invalid SIP configuration. They cannot
verify Twilio's hosted signature checks, registration, media, or billing.

After deployment, verify the Function visibility settings, an unauthorized call,
a wrong PIN, a valid call, a second simultaneous call, and hangup from both ends.
Inspect Twilio call records to confirm both legs end and idle registration does
not accrue call minutes. The local spoken shell test and the first end-to-end
phone test have passed. the primary user confirmed hearing Codex respond, the operator used
tmux during the call, and both call legs ended together on handset hangup.
The client needs the local TLS keepalive patch described in
[SIP-KEEPALIVE.md](../docs/SIP-KEEPALIVE.md). Long idle, network recovery,
simultaneous-call handling, and the remaining negative live cases above still
need validation. See [VOICE-STATUS.md](../docs/VOICE-STATUS.md).
