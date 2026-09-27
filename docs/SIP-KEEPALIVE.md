# Twilio TLS connection recovery

On 2026-09-14, the first two phone attempts passed the caller/PIN check but
their SIP child legs failed with Twilio error 32011. Twilio's diagnostic named
the private LAN Contact address. Baresip never received the INVITEs. Entering
the complete PIN submits it automatically; `#` is unnecessary.

Registration success does not prove that the TLS connection stays open.
Stock libre 4.11.0 starts its SIP outbound keepalive only when the registrar's
response contains `Require: outbound`. Twilio's omission of this header is
documented in this [Baresip discussion](https://github.com/baresip/baresip/discussions/2614).
When the flow closes, a registration can still exist while calls fail to reach
its private Contact address.

The local builder applies `patches/re-twilio-tls-keepalive.patch` to the verified
libre source archive. For TLS registrations with SIP outbound enabled and an
AOR host ending in `.sip.twilio.com`, the patch starts CRLF keepalive without
requiring that header. It requests a 25-second interval; libre randomizes it to
20–25 seconds and expects a response within 10 seconds. Other providers retain
upstream behavior. Standard failure handling re-registers when the flow dies.
TLS certificate verification, mandatory SRTP, caller/PIN checks, and origin
filtering remain enabled.

Twilio documents CRLF ping/pong as a way to preserve signaling connections
behind NAT in its [SIP infrastructure guide](https://www.twilio.com/docs/sip-trunking).
That guide describes Elastic SIP Trunking; the live test here must establish
the behavior for this Programmable Voice registered endpoint. No public HTTP
listener or router port forwarding is configured.

Rebuild and verify locally:

```bash
python3 scripts/build-baresip.py
python3 scripts/check-sip-keepalive.py
```

The test uses three temporary Baresip clients and local TLS registrars. It
checks two keepalive exchanges on one connection when the header is absent,
and verifies the fallback does not apply to unrelated or lookalike domains.
It uses no production credentials or audio devices. Because Baresip excludes
loopback interfaces, its test connections use this host's own interface IP;
traffic remains local. Temporary listeners accept only registration traffic.

After verifying there are no active calls, restart `voip-baresip.service` to
load the new library. The Codex operator does not need a restart.

With the provisioning credentials loaded, `python3 scripts/check-twilio-sip.py`
makes one short SIP-only call to the configured local operator endpoint. It
plays silence for three seconds, ends the call, and saves sanitized metadata
in `state/twilio-sip-probe.json`. It incurs ordinary Twilio SIP charges, never
dials a PSTN destination, and refuses to start while another call is active.
It tests signaling and termination; telephone audio still needs a human call.
Do not retry blindly if call creation times out: inspect Twilio's calls first.

## Live results

The three local TLS cases passed. The deployed registrar exercised the fallback
and returned `200 OK`. A direct Twilio SIP probe then completed after three
seconds with SRTP enabled and no Twilio notifications. The subsequent handset
call passed the PIN and connected for 95 seconds, with about 64 kbps of RTP in
each direction. Its parent and child legs ended together at 00:27:34 UTC on
2026-09-15 (September 14 locally). The TLS connection retained the same local
and remote endpoints throughout a 100-second observation. No router forwarding
was added. the primary user confirmed hearing Codex respond, completing the first end-to-end
phone test. Multi-hour idle and network-change recovery remain untested.

Private sanitized evidence: `state/twilio-acceptance.json`,
`state/twilio-sip-probe.json`, and `state/twilio-keepalive-acceptance.json`.

## IPv6 address changes — September 15 recovery

After reboot, repeated network changes caused registration failures and missed
incoming calls. The logs contained `Network is unreachable` and connection
timeouts; the host had frequently changing IPv6 addresses. Baresip's `netroam`
module resets SIP transports when its monitored local addresses change, even
when a call uses another address family. This installation's Twilio signaling
uses IPv4, so the generated and live Baresip configurations now set `net_af ipv4`.
This limits Baresip's address monitoring and transports to IPv4; it does not
alter host networking or the Codex daemon.

After restarting Baresip, registration returned `200 OK`, and a three-second
Twilio-to-local-SIP test completed at 20:45:09 UTC with no Twilio notifications.
The original voice operator remained connected. Six configuration/provisioning
tests passed. This removes unrelated IPv6 changes as a source of SIP resets;
upstream network outages can still interrupt calls. The previous live config is
saved privately at `state/baresip-config-before-ipv4`.
