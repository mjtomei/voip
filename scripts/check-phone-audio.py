#!/usr/bin/env python3
"""Record/STT an arranged real call, optionally enqueue its prepared agenda.

This is deliberately separate from automated local tests. --run-telephony is
required; --request-key places a real billable call through the request daemon.
A passing result proves words at the local speaker monitor, not handset playback.
"""
import argparse
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import CallEvents
from operator_mailbox import Mailbox, save_json
from requests_queue import Requests
from voice_acceptance import Capture, synthesize, play
from voip import Project, SetupError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-telephony', action='store_true')
    parser.add_argument('--session', default='default')
    parser.add_argument('--recipient')
    parser.add_argument('--request-key', help='stable key for a new or retried authorized outgoing request')
    parser.add_argument('--agenda', type=Path)
    parser.add_argument('--expect', action='append', required=True, help='required regex in normalized independent STT')
    parser.add_argument('--forbid', action='append', default=[])
    parser.add_argument('--tts', help='optional caller input spoken through the local microphone after connection')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args()
    if not args.run_telephony:
        parser.error('Arrange the real call with the recipient, then pass --run-telephony')
    if bool(args.request_key) != bool(args.agenda):
        parser.error('--request-key and --agenda are required together')
    if not 30 <= args.timeout <= 600:
        parser.error('timeout must be 30–600 seconds')
    p = Project(session_id=args.session)
    directory = p.state / 'voice/telephony-tests' / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
    started = time.time()
    existing = {k for k,c in CallEvents(p).load()['calls'].items() if c['connected']}
    result = None
    queue = Requests(p)
    queued = False
    try:
        with Capture(p, directory) as capture:
            if args.request_key:
                message = Mailbox(p, args.recipient).post('joint-phone-test', 'Arranged phone acceptance test', args.agenda.read_text(),
                                         args.request_key)
                queue.enqueue(args.request_key, 'call', 'Arranged phone audio acceptance test',
                              [message['id']], recipient=args.recipient)
                queued = True
            gate_observed = False
            def connected():
                nonlocal gate_observed
                state = CallEvents(p).load()
                active = {k:c for k,c in state['calls'].items() if c['connected']}
                if args.request_key:
                    r = queue.read(args.request_key)
                    if r['state'] in {'blocked', 'failed', 'cancelled', 'expired'}:
                        raise SetupError('Call request ' + r['state'] + '; inspect its status')
                    # Match the provider identity for this attempt, never a concurrent incoming call.
                    from outgoing_calls import Calls
                    saved = Calls(p).load()
                    ids = {saved[a['key']]['id'] for a in r['attempts'] if a['key'] in saved}
                    found = any(c.get('outgoing_request_id') in ids for c in active.values())
                else:
                    found = bool(active)
                if found and not gate_observed:
                    capture.after('call_established')
                    gate_observed = True
                return found
            if args.tts:
                deadline = time.monotonic() + args.timeout
                while not connected():
                    if time.monotonic() >= deadline:
                        raise SetupError('No matching established call')
                    time.sleep(.5)
                audio = directory / 'request.wav'
                synthesize(p, args.tts, audio)
                play(p, audio)
            result = capture.verify(args.expect, forbidden=args.forbid, predicate=connected, timeout=args.timeout)
            result.update(real_phone_signaling=True, handset_playback_verified=False, started_at=started,
                          request_key=args.request_key)
            save_json(directory / 'telephony-result.json', result)
            print('PASS: STT verified the local output during the matching real call. Confirm handset sound with the recipient.')
            print('Evidence: ' + str(directory / 'telephony-result.json'))
    finally:
        # A failed test cannot leave its own queued/redial request running unattended.
        # Never cancel a pre-existing call simply because a monitor assertion failed.
        if result is None and queued:
            queue.cancel(args.request_key)
            print('Test did not pass; cancellation requested for its own call. Check request status for settlement.')

if __name__ == '__main__':
    main()
