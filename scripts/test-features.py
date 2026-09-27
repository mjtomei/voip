#!/usr/bin/env python3
"""Opt-in real Codex voice E2E tests; simulated phone signaling, no handset calls.

Every caller request is synthesized into an isolated Pulse device. Spoken
responses are verified by local STT. Real native sessions, tools, mailbox,
preparation, prompts, dispatcher, schedule and reset paths are exercised.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from call_events import CallEvents, call_key
from call_schedule import Scheduler
from operator_mailbox import Mailbox, save_json
from operator_reset import reset_operator
from requests_queue import Requests
from voice_acceptance import Capture, ask, synthesize, play, stt_paths
from voip import Project, SetupError, atomic_write
from voice_e2e_fixture import Fixture, until

CASES = ['smoke', 'profiles', 'mailbox', 'prepared', 'hangup', 'requests', 'backend_requests', 'schedules',
         'missed', 'redial', 'concurrency', 'different_recipients', 'frontend_reset', 'session_reset', 'outgoing_reset', 'directory_workspace']


def require(value, reason):
    if not value:
        raise SetupError(reason)


class Suite:
    def __init__(self, fixture, timeout):
        self.f = fixture
        self.timeout = timeout
        self.step = 0
        self.case = ''

    def directory(self, label):
        self.step += 1
        return self.f.directory / self.case / (str(self.step).zfill(2) + '-' + label)

    def say(self, project, text, patterns, **kwargs):
        return ask(project, self.directory(project.session_id), text, patterns,
                   timeout=self.timeout, **kwargs)

    def fresh_phrase(self):
        return secrets.choice(['purple', 'yellow', 'silver', 'orange']) + ' ' + secrets.choice([
            'mountain', 'castle', 'harbor', 'lantern', 'meadow', 'garden'])

    def post(self, p, body, key=None, recipient=None):
        key = key or 'e2e-' + secrets.token_hex(5)
        Mailbox(p, recipient).post('acceptance-test', 'Audio acceptance agenda', body, key)
        return key

    def enqueue(self, p, kind='call', phrase=None, **kwargs):
        key = 'e2e-' + secrets.token_hex(5)
        phrase = phrase or self.fresh_phrase()
        message = self.post(p, 'Notification only: the review reference is ' + phrase +
            '. Explain this reference to the caller. No actions or decisions are needed. '
            'Do not mark it addressed during preparation or before acknowledgment.', recipient=kwargs.get('recipient'))
        Requests(p).enqueue(key, kind, 'Discuss the reference ' + phrase, message_ids=[message], **kwargs)
        return key, message, phrase

    def dispatch(self, key, expected='active'):
        def tick():
            record = self.f.dispatcher.process(key)
            if record['state'] == 'blocked':
                raise SetupError('Request blocked: ' + record.get('waiting_for', 'unknown'))
            return record if record['state'] == expected else None
        return until(tick, self.timeout + 180, key + ' becoming ' + expected)

    def end(self, p, identity, key=None, review=False):
        if identity in self.f.phone.parents:
            self.f.phone.finish(identity)
        else:
            self.f.disconnect(p, identity)
        def delivered():
            return any(e['id'] == call_key(identity) + ':ended' and
                       e['deliveries'].get(p.thread_id(), {}).get('state') == 'submitted'
                       for e in CallEvents(p).load()['events'])
        until(delivered, 15, 'hangup notice delivery')
        if review:
            audit = self.f.root / ('state/disconnected-' + p.session_id + '.txt')
            until(lambda: audit.exists() and call_key(identity) in audit.read_text(),
                  self.timeout, 'custom hangup prompt and mailbox review')
        if key:
            self.f.dispatcher.process(key)
        self.f.settle(p)

    def smoke(self):
        p = self.f.projects['default']
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/test-voice.py'), 'run',
            '--root', str(p.root), '--marker-file', 'result.txt', '--response-seconds', str(self.timeout)],
            capture_output=True, text=True, timeout=self.timeout + 240)
        atomic_write(self.directory('legacy-smoke') / 'runner.log', result.stdout + result.stderr)
        require(result.returncode == 0, 'Legacy TTS + shell + STT smoke failed; inspect runner.log')

    def directory_workspace(self):
        p = self.f.projects['default']
        marker = p.workspace / 'proof.txt'
        require(p.workspace != p.root, 'Fixture did not separate voice metadata and work directory')
        self.say(p, 'Use your shell tool to write the absolute path of your current working directory '
                 'into the file proof dot txt in that same directory. Do not change directories. '
                 'Then say directory workspace verified.', ['directory workspace verified'],
                 predicate=lambda: marker.exists() and marker.read_text().strip() == str(p.workspace))

    def profiles(self):
        projects = self.f.projects
        require(projects['default'].thread_id() != projects['blue'].thread_id(), 'Profiles share a native thread')
        # Simultaneous real voice input/output on separate audio devices and threads.
        def check(name, expected, forbidden):
            p = projects[name]
            return self.say(p, 'What is your profile identification phrase? Say only that phrase.',
                            [expected], forbidden=[forbidden])
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(check, 'default', 'silver meadow', 'copper forest')
            b = pool.submit(check, 'blue', 'copper forest', 'silver meadow')
            a.result(); b.result()

    def mailbox(self):
        p = self.f.projects['default']
        with Capture(p, self.directory('custom-connection')) as capture:
            identity = self.f.connect(p)
            capture.verify(['hello from the garden'], timeout=self.timeout)
        box = Mailbox(p)
        nonce = str(100000 + secrets.randbelow(900000))
        marker = self.f.root / 'state/mailbox-marker.txt'
        done = self.post(p, 'The caller may authorize this action: write ' + nonce +
            ' into state/mailbox-marker.txt. After doing so say "mailbox action verified". '
            'Address this message only after completing the action.')
        pending = self.post(p, 'Ask whether the caller approves the green deployment. '
            'An explicit yes or no decision is required. Keep pending until answered.')
        self.say(p, 'I authorize you to read my test mailbox and perform its marker file task and mark only that task '
                 'addressed after completion while leaving the deployment question pending because I have not decided '
                 'then say mailbox action verified.',
                 ['mail ?box action verified'], predicate=lambda: marker.exists() and marker.read_text().strip() == nonce
                    and box.read(done)['status'] == 'addressed')
        require(box.read(pending)['status'] == 'unaddressed', 'Unanswered question incorrectly addressed')
        self.end(p, identity)
        require(box.read(pending)['status'] == 'unaddressed', 'Hangup incorrectly addressed unanswered question')
        self.say(p, 'Inspect the mailbox status of the deployment question. Say deployment still pending if it remains unanswered.',
                 ['(?:still|is|s|remains) pending|remains unanswered'],
                 forbidden=['not (?:still )?pending', '(?:is|s) (?:already )?addressed'],
                 predicate=lambda: box.read(pending)['status'] == 'unaddressed')
        # Cleanup only after all assertions; this does not count as operator completion.
        box.address(pending, 'Harness cleanup after verifying the unanswered question stayed pending')

    def prepared(self, review=False):
        p = self.f.projects['default']
        self.f.settle(p)
        key, message, phrase = self.enqueue(p)
        old_thread = p.thread_id()
        record = self.dispatch(key)
        saved = self.f.calls(p).load()[key]
        require(saved['preparation']['source'] == 'operator-turn', 'Preparation did not run on the real backend')
        require(self.f.phone.posts[-1]['from'] == p.config['twilio']['phone_number'], 'Server Twilio number was not used')
        require(saved['preparation']['prepared_at'] <= self.f.phone.posts[-1]['at'], 'Dial preceded preparation')
        require(Mailbox(p).read(message)['status'] == 'unaddressed', 'Preparation addressed the agenda')
        prepared_turns = self.f.turn_ids(p)
        with Capture(p, self.directory('opening')) as capture:
            sid = self.f.phone.answer(p, key)
            capture.after('call_established')
            capture.verify([phrase.replace(' ', ' ?')], timeout=self.timeout, forbidden=['connected', 'mailbox'])
        require(self.f.turn_ids(p) == prepared_turns, 'Opening caused an additional backend delegation')
        require(p.thread_id() == old_thread, 'Persistent profile unexpectedly reset')
        self.say(p, 'Thank you, I acknowledge the review reference. No follow up is needed. Say acknowledgment received.',
                 ['acknowledgment received|acknowledgement received'])
        self.end(p, sid, key, review=review)
        if review:
            require(Mailbox(p).read(message)['status'] == 'addressed', 'Acknowledged notification was not addressed on hangup')
            require(Mailbox(p).read(message)['resolution'].strip(), 'Addressed notification lacks an outcome note')
        require(Requests(p).read(key)['state'] == 'completed', 'Ended connected request not completed')

    def hangup(self):
        self.prepared(review=True)

    def requests(self):
        p = self.f.projects['default']
        identity = self.f.connect(p)
        self.f.settle(p)
        self.say(p, 'Please say ready for request tests.', ['ready'])
        # Actual input activity makes normal requests wait; no mocked idle/quiet state.
        key, message, phrase = self.enqueue(p, mode='non_preemptive')
        audio = self.directory('busy-input') / 'request.wav'
        synthesize(p, 'Please listen until I finish. I am explaining a long issue before we change the topic. '
                      'There are several details to discuss. Please wait until I finish this sentence. ' * 2, audio)
        player = subprocess.Popen([p.executable('paplay'), '--device=' + p.audio['incoming_sink'], str(audio)],
                                  env=p.pulse_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(2)
            record = self.f.dispatcher.process(key)
            require(record['state'] == 'queued' and not record.get('prompt_state'), 'Nonpreemptive request interrupted audio')
            before = len(self.f.phone.posts)
            urgent, urgent_message, urgent_phrase = self.enqueue(p, kind='message', mode='preemptive', phrase='priority cedar')
            record = self.dispatch(urgent, 'prompt_submitted')
            require(record['prompt_result']['method'] == 'realtime', 'Priority request did not enter connected voice')
        finally:
            player.terminate(); player.wait(timeout=5)
        self.say(p, 'Please summarize the priority reference you were just given, then say the request code.',
                 ['priority (?:reference is )?cedar', 'violet ?garden'])
        with Capture(p, self.directory('reuse')) as capture:
            self.dispatch(key, 'prompt_submitted')
            capture.verify([phrase.replace(' ', ' ?')], timeout=self.timeout)
        require(len(self.f.phone.posts) == before, 'Same-recipient reuse dialed another call')
        require(Requests(p).read(key)['outcome'] == 'existing_call_kept_connected', 'Call was not reused')
        # A different recipient must wait even if priority is preemptive.
        other, other_message, _ = self.enqueue(p, recipient='bob', mode='preemptive')
        record = self.f.dispatcher.process(other)
        require(record['state'] == 'queued' and not record['attempts'], 'Other recipient displaced an active call')
        Requests(p).cancel(other)
        self.say(p, 'I acknowledge both reference notifications. Please say references acknowledged.', ['references acknowledged'])
        self.end(p, identity)
        Mailbox(p, 'bob').address(other_message, 'Harness cleanup of cancelled test agenda')

    def backend_requests(self):
        p = self.f.projects['default']
        gate = self.f.root / 'gate.py'
        atomic_write(gate, "from pathlib import Path\nimport time\nPath('state/gate-started').write_text('started')\n"
            "end=time.monotonic()+75\nwhile not Path('state/gate-release').exists() and time.monotonic()<end: time.sleep(.2)\n"
            "Path('state/gate-finished').write_text('finished')\n")
        audio = self.directory('start-gate') / 'request.wav'
        synthesize(p, 'Run python three gate dot pie in this project with your shell. '
            'The test script waits for a release file. Wait for its completion; do not create the release file yourself. '
            'When it finishes say gate work finished.', audio)
        with Capture(p, self.directory('priority-during-tool')) as capture:
            play(p, audio)
            until(lambda: (self.f.root / 'state/gate-started').exists(), self.timeout, 'audio-triggered backend tool')
            normal, normal_message, normal_phrase = self.enqueue(p, kind='message', phrase='patient willow')
            record = self.f.dispatcher.process(normal)
            require(record['state'] == 'queued' and not record.get('prompt_state'), 'Normal request interrupted running backend')
            urgent, urgent_message, urgent_phrase = self.enqueue(p, kind='message', mode='preemptive', phrase='urgent maple')
            record = self.dispatch(urgent, 'prompt_submitted')
            require(record['prompt_result']['method'] == 'steer', 'Busy off-call request did not steer the existing native turn')
            require(not (self.f.root / 'state/gate-finished').exists(), 'Priority request did not arrive during tool execution')
            atomic_write(self.f.root / 'state/gate-release', 'release\n')
            capture.verify(['gate ?work finished'], timeout=self.timeout,
                predicate=lambda: (self.f.root / 'state/gate-finished').exists())
        self.dispatch(normal, 'prompt_submitted')
        self.f.settle(p)
        self.say(p, 'Read my test mailbox. What were the urgent and patient reference phrases? '
                 'Please say both phrases and confirm the gate work finished.',
                 ['urgent maple', 'patient willow', 'gate ?work finished'])
        # No caller acknowledgment of these notices occurred: harness cleanup is explicit.
        for message in (normal_message, urgent_message):
            Mailbox(p).address(message, 'Harness cleanup after request admission and STT assertions')

    def write_schedule(self, phrase, mode='non_preemptive'):
        atomic_write(self.f.root / 'agenda.txt', 'Notification only: scheduled reference ' + phrase + '. No decision is required.')
        atomic_write(self.f.root / 'schedules.toml', '''[[schedules]]
id="review"
cron="* * * * *"
timezone="America/Los_Angeles"
session="default"
recipient="alice"
reason="Scheduled reference"
agenda_file="agenda.txt"
mode="''' + mode + '"\n')

    def schedules(self):
        p = self.f.projects['default']
        phrase = 'scheduled orange harbor'
        self.write_schedule(phrase)
        scheduler = Scheduler(p)
        scheduler.tick()
        require(scheduler.tick() == [], 'Scheduled occurrence duplicated')
        key = next(k for k, r in Requests(p).load().items() if r['spec']['source'] == 'schedule:review')
        self.dispatch(key)
        with Capture(p, self.directory('scheduled-opening')) as capture:
            sid = self.f.phone.answer(p, key)
            capture.after('call_established')
            capture.verify([phrase.replace(' ', ' ?')], timeout=self.timeout)
        self.say(p, 'I acknowledge the scheduled reference and no follow up is needed so please confirm with the word thanks.', ['thanks|thank you|acknowledged'])
        # A subsequent occurrence while connected uses the very same queue/reuse path.
        self.write_schedule('scheduled blue mountain', mode='preemptive')
        future = (int(time.time()) // 60 + 1) * 60
        Scheduler(p, clock=lambda: future).tick()
        second = next(k for k, r in Requests(p).load().items() if r['spec']['source'] == 'schedule:review' and k != key)
        before = len(self.f.phone.posts)
        with Capture(p, self.directory('scheduled-reuse')) as capture:
            self.dispatch(second, 'prompt_submitted')
            capture.verify(['scheduled blue mountain'], timeout=self.timeout)
        require(len(self.f.phone.posts) == before, 'Scheduled preemption redialed a connected recipient')
        self.say(p, 'I acknowledge both scheduled references and no follow up is needed so please confirm with the word thanks.', ['thanks|thank you|acknowledged'])
        self.end(p, sid, key)
        atomic_write(self.f.root / 'schedules.toml', 'schedules=[]\n')
        # Reset only this isolated scheduler's history for the independent missed scenario.
        (p.shared_state / 'schedule-state.json').unlink(missing_ok=True)

    def missed(self):
        p = self.f.projects['default']
        self.write_schedule('offline counting fixture')
        now = int(time.time()) // 60 * 60
        Scheduler(p, clock=lambda: now - 300).tick()
        queued_before = len(Requests(p).load())
        # Recover four skipped minutes before the next observer connection, no catch-up.
        skipped = Scheduler(p, clock=lambda: now).missed('alice')
        require(len(skipped) == 4 and len(Requests(p).load()) == queued_before, 'Offline recovery dialed or miscounted')
        atomic_write(self.f.root / 'schedules.toml', 'schedules=[]\n')
        Scheduler(p, clock=lambda: now).tick()
        identity = self.f.connect(p)
        self.f.settle(p)
        self.say(p, 'This is a diagnostic question. How many scheduled calls does this connection notice say were skipped '
                 'while the scheduler was offline? State the count and say skipped calls. Do not call anyone.',
                 ['(?:four|4) (?:scheduled )?(?:calls (?:were )?)?skipped|(?:four|4) scheduled calls were skipped'])
        event = next(e for e in CallEvents(p).load()['events'] if e['id'] == call_key(identity) + ':connected')
        require(len(event['missed_schedule_keys']) == 4 and event.get('missed_schedule_reported_at'), 'Skipped count not receipted')
        before = len(self.f.phone.posts)
        self.end(p, identity)
        identity = self.f.connect(p)
        self.f.settle(p)
        self.say(p, 'For this latest connection only, is there a new skipped call count in the notice? '
                 'If there is none, say no new skipped calls.', ['no new (?:skipped calls|count|skipped call count)'])
        event = next(e for e in CallEvents(p).load()['events'] if e['id'] == call_key(identity) + ':connected')
        require(not event['missed_schedule_keys'] and len(self.f.phone.posts) == before, 'Skipped calls repeated or dialed')
        self.end(p, identity)
        atomic_write(self.f.root / 'schedules.toml', 'schedules=[]\n')
        for key, r in Requests(p).load().items():
            if r['state'] == 'queued' and r['spec']['source'] == 'schedule:review':
                Requests(p).cancel(key)
                for m in r['spec']['message_ids']:
                    Mailbox(p).address(m, 'Harness cleanup of undialed scheduler baseline occurrence')

    def redial(self):
        p = self.f.projects['default']
        self.f.settle(p)
        profile = p.root / 'sessions/default.toml'
        atomic_write(profile, profile.read_text() + '\n[redial]\nmax_attempts=2\ndelay_seconds=5\n')
        p = self.f.projects['default'] = Project(self.f.root)
        key, message, phrase = self.enqueue(p)
        self.f.phone.next_status = 'busy'
        self.dispatch(key)
        record = self.f.dispatcher.process(key)
        require(record['state'] == 'retry_wait' and len(record['attempts']) == 1, 'Busy did not schedule configured retry')
        before = len(self.f.phone.posts)
        record = self.f.dispatcher.process(key)
        require(len(self.f.phone.posts) == before and record['state'] == 'retry_wait', 'Redial ignored configured delay')
        self.dispatch(key)
        record = Requests(p).read(key)
        require(len(record['attempts']) == 2, 'Expected precisely two attempts')
        attempt = record['attempts'][-1]['key']
        with Capture(p, self.directory('retry-opening')) as capture:
            sid = self.f.phone.answer(p, attempt)
            capture.after('call_established')
            capture.verify([phrase.replace(' ', ' ?')], timeout=self.timeout)
        self.say(p, 'The reference is acknowledged. Please say retry acknowledged.', ['retry acknowledged'])
        self.end(p, sid, key)
        require(Requests(p).read(key)['state'] == 'completed', 'Retry call not completed')
        # Queue cancellation and TTL are tested through audio status queries and state.
        cancelled, msg, _ = self.enqueue(p)
        Requests(p).cancel(cancelled)
        self.f.dispatcher.process(cancelled)
        path = self.f.root / 'state/request-status-key.txt'
        atomic_write(path, cancelled)
        self.say(p, 'Read state slash request status key dot T X T. Inspect that request through the CLI. '
                 'If it is cancelled, say request cancelled without claiming any backend tasks were killed.', ['request cancelled|request canceled'])
        Mailbox(p).address(msg, 'Harness cleanup of cancelled agenda')
        expired, expired_message, _ = self.enqueue(p, ttl_seconds=1)
        time.sleep(1.1)
        record = self.f.dispatcher.process(expired)
        require(record['state'] == 'expired' and not record['attempts'], 'Expired request was dialed')
        atomic_write(path, expired)
        self.say(p, 'Read the request key file again and inspect its current status. If it expired without a call, '
                 'say request expired without dialing.', ['request expired without dialing'])
        Mailbox(p).address(expired_message, 'Harness cleanup of expired agenda')
        require(len(self.f.phone.posts) == before + 1, 'Cancel or expiry created another provider attempt')


    def different_recipients(self):
        self.concurrency('bob')

    def concurrency(self, other_recipient='alice'):
        p, blue = self.f.projects['default'], self.f.projects['blue']
        for target in (p, blue):
            self.f.settle(target)
        a, ma, pa = self.enqueue(p, phrase='silver compass')
        b, mb, pb = self.enqueue(blue, phrase='copper lantern', recipient=other_recipient)
        if other_recipient != 'alice':
            require(ma not in Mailbox(blue, other_recipient).load(), 'First recipient message leaked to second mailbox')
            require(mb not in Mailbox(p, 'alice').load(), 'Second recipient message leaked to first mailbox')
        else:
            require(Mailbox(p, 'alice').path == Mailbox(blue, 'alice').path,
                    'Same person and project must share a mailbox across sessions')
        self.dispatch(a)
        # Calls.prepare and the native models are real. The same number in another
        # profile must not reserve this profile's endpoint.
        self.dispatch(b)
        posts = self.f.phone.posts[-2:]
        require((posts[0]['to'] == posts[1]['to']) == (other_recipient == 'alice'), 'Concurrency recipient selection was incorrect')
        require(posts[0]['sid'] != posts[1]['sid'], 'Separate profiles shared a phone call')
        with Capture(p, self.directory('default-concurrent')) as ca, Capture(blue, self.directory('blue-concurrent')) as cb:
            sa = self.f.phone.answer(p, a)
            ca.after('call_established')
            sb = self.f.phone.answer(blue, b)
            cb.after('call_established')
            ca.verify([pa.replace(' ', ' ?')], forbidden=[pb.replace(' ', ' ?')], timeout=self.timeout)
            cb.verify([pb.replace(' ', ' ?')], forbidden=[pa.replace(' ', ' ?')], timeout=self.timeout)
        with ThreadPoolExecutor(2) as pool:
            results = [pool.submit(self.say, target, 'I acknowledge the reference. Say reference acknowledged.', ['reference acknowledged'])
                       for target in (p, blue)]
            for r in results: r.result()
        self.end(p, sa, a); self.end(blue, sb, b)

    def remember(self, p):
        self.f.settle(p)
        self.say(p, 'Please remember the conversation password golden umbrella without writing it to a file and repeat that password back to me now.', ['golden ?umbrella'])

    def recovery(self, scope):
        p = self.f.projects['blue']
        self.remember(p)
        original = p.thread_id()
        reset_operator(p, scope)
        require(p.thread_id() == original, scope + ' changed the conversation')
        self.say(p, 'What conversation password did I ask you to remember? Say only that phrase.', ['golden ?umbrella'])

    def frontend_reset(self):
        self.recovery('frontend')

    def session_reset(self):
        self.recovery('session')

    def outgoing_reset(self):
        p = self.f.projects['blue']
        self.remember(p)
        original = p.thread_id()
        self.f.profile('blue', reset=True)
        p = self.f.projects['blue']
        key, message, phrase = self.enqueue(p, phrase='fresh turquoise river')
        self.dispatch(key)
        require(p.thread_id() != original, 'reset_on_outgoing_call did not create a new conversation')
        new = p.thread_id()
        self.f.runtime.reset_once(p, key)
        require(p.thread_id() == new, 'Same outgoing attempt reset twice')
        with Capture(p, self.directory('fresh-opening')) as capture:
            sid = self.f.phone.answer(p, key)
            capture.after('call_established')
            capture.verify([phrase.replace(' ', ' ?')], timeout=self.timeout)
        self.say(p, 'If no conversation password was supplied in this new conversation please say no password supplied without inspecting archived conversations.',
                 ['no pass ?word supplied'], forbidden=['golden ?umbrella'])
        other, other_message, other_phrase = self.enqueue(p, phrase='reused turquoise river', mode='preemptive')
        with Capture(p, self.directory('fresh-reuse')) as capture:
            self.dispatch(other, 'prompt_submitted')
            capture.verify([other_phrase.replace(' ', ' ?')], timeout=self.timeout)
        require(p.thread_id() == new, 'Reused active call incorrectly reset the session')
        self.say(p, 'I acknowledge both references. Please say references acknowledged.', ['references acknowledged'])
        self.end(p, sid, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='create isolated real native sessions and run acceptance tests')
    parser.add_argument('--case', action='append', choices=CASES)
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--permissions', choices=['workspace-write', 'danger-full-access'], default='workspace-write',
                        help='Host tool permissions; container CLI via Docker requires explicit danger-full-access')
    args = parser.parse_args()
    if not args.run:
        print('Cases: ' + ', '.join(CASES) + '\nUse --run to consume native Codex voice usage. No telephony calls are placed.')
        return 0
    if not all(path.is_file() for path in stt_paths()):
        parser.error('Install local STT first: python3 scripts/install-stt.py')
    if not 30 <= args.timeout <= 300:
        parser.error('timeout must be 30–300 seconds')
    directory = args.output or ROOT / 'state/e2e' / (time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
    directory.mkdir(parents=True, mode=0o700)
    selected = args.case or CASES
    report = {'started_at': time.time(), 'boundary': 'Real native Codex, tools and Pulse audio; simulated Twilio and SIP observer',
              'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                  ('scripts/test-features.py', 'scripts/voice_e2e_fixture.py', 'voice_acceptance.py',
                   'request_runtime.py', 'voice_frontend.py', 'call_events.py',
                   'prompts/operator.md', 'operator_reset.py')},
              'permissions': args.permissions, 'phone_calls_placed': 0, 'cases': {name: {'status': 'not_run'} for name in selected},
              'telephony_status': 'pending_joint_test', 'artifacts': str(directory)}
    try:
        for name in selected:
            profiles = ('default', 'blue') if name in {'profiles', 'concurrency', 'different_recipients'} else (('blue',) if name.endswith('_reset') else ('default',))
            fixture = Fixture(directory / name, profiles,
                              workspace=directory / name / 'worker-workspace' if name == 'directory_workspace' else None,
                              permissions=args.permissions, keep_voice=name != 'hangup')
            suite = Suite(fixture, args.timeout)
            suite.case = name
            report['cases'][name] = {'status': 'running', 'started_at': time.time()}
            save_json(directory / 'report.json', report)
            print('RUN ' + name + ' — artifacts ' + str(fixture.directory), flush=True)
            try:
                fixture.setup()
                getattr(suite, name)()
                require(not fixture.heartbeat_errors, 'Fixture observer heartbeat encountered errors')
                report['cases'][name].update(status='passed', finished_at=time.time())
                print('PASS ' + name, flush=True)
            except Exception as e:
                report['cases'][name].update(status='failed', error=str(e), finished_at=time.time())
                atomic_write(fixture.directory / 'failure.log', traceback.format_exc())
                print('FAIL ' + name + ': ' + str(e), flush=True)
            finally:
                try:
                    fixture.diagnostics()
                except Exception as error:
                    report['cases'][name]['diagnostic_error'] = type(error).__name__
                errors = fixture.cleanup()
                report['cases'][name]['cleanup_errors'] = errors
                if errors:
                    report['cases'][name]['status'] = 'failed'
                save_json(directory / 'report.json', report)
    finally:
        for value in report['cases'].values():
            if value['status'] == 'running':
                value.update(status='interrupted', finished_at=time.time())
        report['finished_at'] = time.time()
        report['passed'] = all(r['status'] == 'passed' for r in report['cases'].values())
        save_json(directory / 'report.json', report)
        print('Report: ' + str(directory / 'report.json'), flush=True)
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
