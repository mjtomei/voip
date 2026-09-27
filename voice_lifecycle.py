"""Bounded voice demand. Saved conversations and SIP registration outlive audio."""
import asyncio
import json
import os
import time

from voip import SetupError, atomic_write, lock, operator_status, process_identity

LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 600
OBSERVER_GRACE_SECONDS = 10
RETRY_SECONDS = 30


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def acquire(project, key, seconds=LEASE_SECONDS, kind='outgoing'):
    if not 0 < seconds <= MAX_LEASE_SECONDS or kind not in {'outgoing', 'test'}:
        raise SetupError('Voice demand must have a bounded lifetime and a known purpose')
    if (project.state/'operator-paused').exists():
        raise SetupError('Operator is paused')
    with lock(project.state/'voice-demand.lock', blocking=True):
        path = project.state/'voice-demand.json'
        values = read(path)
        values[key] = {'thread_id': project.thread_id(), 'created_at': time.time(),
                       'expires_at': time.time()+seconds, 'kind': kind}
        atomic_write(path, json.dumps(values)+'\n')


def release(project, key):
    with lock(project.state/'voice-demand.lock', blocking=True):
        path = project.state/'voice-demand.json'
        values = read(path)
        if key in values:
            del values[key]
            atomic_write(path, json.dumps(values)+'\n')


def demand(project, now=None):
    from call_events import CallEvents
    now = time.time() if now is None else now
    if (project.state/'operator-paused').exists():
        return False
    events = CallEvents(project).load()
    # A dead observer cannot keep metered audio open forever. Recovery can wake it again.
    fresh = 0 <= now-events.get('observed_at',0) <= OBSERVER_GRACE_SECONDS
    active = fresh and any(c.get('connected') for c in events.get('calls',{}).values())
    with lock(project.state/'voice-demand.lock', blocking=True):
        path = project.state/'voice-demand.json'
        leases = read(path)
        valid = {k:v for k,v in leases.items() if isinstance(v,dict)
                 and v.get('thread_id') == project.thread_id()
                 and 0 <= now-v.get('created_at',0) <= MAX_LEASE_SECONDS
                 and now < v.get('expires_at',0) <= v.get('created_at',0)+MAX_LEASE_SECONDS+1}
        # A call takes over its pre-dial lease. Hangup must not inherit unused lease time.
        for key, value in list(valid.items()):
            if value.get('kind') == 'outgoing' and any(
                    c.get('seen_at',0) >= value['created_at'] for c in events.get('calls',{}).values()):
                del valid[key]
        if valid != leases:
            atomic_write(path, json.dumps(valid)+'\n')
    return bool(active or valid)


def warm(project, key, timeout=90, seconds=LEASE_SECONDS, kind="outgoing"):
    """Called only after text preparation. Never start a fresh conversation."""
    acquire(project, key, seconds=seconds, kind=kind)
    deadline = time.monotonic()+timeout
    try:
        while time.monotonic() < deadline:
            status = operator_status(project)
            if status.get('supervision_paused'):
                raise SetupError('Operator was paused during voice startup')
            if status['codex_process_alive'] and status['voice'] == 'connected':
                return
            time.sleep(.2)
        raise SetupError('Voice did not connect before the outgoing startup deadline')
    except BaseException:
        release(project,key)
        raise


def resume_params(project):
    permissions = project.permission_params() if hasattr(project, 'permission_params') else {
        'approvalPolicy':'never', 'sandbox':'danger-full-access'}
    return {'threadId':project.thread_id(), 'cwd':str(project.workspace), 'excludeTurns':True,
            **permissions, **project.instructions()}


def resume_if_unloaded(project, rpc, thread):
    if thread.get('status',{}).get('type') == 'notLoaded':
        return rpc.request('thread/resume', resume_params(project))['thread']
    return thread


async def ended_notice(project, rpc_factory):
    """Deliver one hangup or missed-call notice as text, without opening realtime or cancelling work."""
    from call_events import CallEvents
    events = CallEvents(project)
    thread = project.thread_id()
    event = events.pending(thread, kinds={'ended','missed'})
    if not event:
        return
    rpc = rpc_factory(project, thread)
    attempted = False
    try:
        await rpc.start()
        current = (await rpc.request('thread/read', {'threadId':thread,'includeTurns':False}))['thread']
        if current.get('status',{}).get('type') == 'notLoaded':
            current = (await rpc.request('thread/resume', resume_params(project)))['thread']
        text = events.message(event)
        params = {'threadId':thread,'input':[{'type':'text','text':text}]}
        method = 'turn/start'
        if current.get('status',{}).get('type') == 'active':
            turns = (await rpc.request('thread/turns/list', {'threadId':thread,'limit':20,
                        'sortDirection':'desc','itemsView':'notLoaded'}))['data']
            turn = next((t for t in turns if t['status']=='inProgress'),None)
            if turn is None:
                return
            method='turn/steer';params['expectedTurnId']=turn['id']
        elif current.get('status',{}).get('type') != 'idle':
            return
        # Do not duplicate a possibly accepted backend turn after a lost response.
        events.delivery(event['id'],thread,'uncertain','text',text)
        attempted=True
        await rpc.request(method,params)
        events.delivery(event['id'],thread,'submitted','text')
        print('Phone service notice submitted through text: '+event['kind'],flush=True)
    except (SetupError,OSError,ValueError,KeyError):
        print('Hangup text delivery '+('needs inspection' if attempted else 'will retry'),flush=True)
        await asyncio.sleep(2)
    finally:
        await rpc.close()


async def serve(project, stop, session_factory=None, rpc_factory=None):
    from voice_frontend import VoiceSession, AppServer
    session_factory = session_factory or VoiceSession
    rpc_factory = rpc_factory or AppServer
    session = None
    operation = None
    voice_stop = None
    notice = None
    retry_at = 0
    identity = {'thread_id':project.thread_id(),'pid':os.getpid(),'starttime':process_identity(os.getpid())}

    def status(state):
        atomic_write(project.state/'voice.json',json.dumps({**identity,'state':state,'updated_at':time.time()})+'\n')

    async def live(current, stopped):
        try:
            await asyncio.wait_for(current.start(),90)
            await current.wait(stopped)
        except Exception as error:
            print('Voice demand attempt failed: '+type(error).__name__+': '+str(error)[:600], flush=True)
            raise
        finally:
            await current.close()

    async def finish():
        nonlocal operation,session,voice_stop
        if operation:
            voice_stop.set()
            operation.cancel()  # Cancels pending negotiation too; live() always closes it.
            await asyncio.gather(operation,return_exceptions=True)
        operation=None;session=None;voice_stop=None

    status('standby')
    try:
        while not stop.is_set():
            atomic_write(project.state/'voice-activity.json',json.dumps({**identity,'observed_at':time.time(),
                'input_at':session.microphone.last_signal_at if session else 0,
                'output_at':session.speaker.last_signal_at if session else 0})+'\n')
            needed = demand(project)
            if operation and (operation.done() or not needed):
                failed = operation.done() and needed
                await finish()
                if failed: retry_at=time.monotonic()+RETRY_SECONDS
                status('standby')
            if needed and operation is None and time.monotonic()>=retry_at:
                session=session_factory(project);voice_stop=asyncio.Event()
                operation=asyncio.create_task(live(session,voice_stop))
            if notice is None or notice.done():
                if notice: await notice
                notice=asyncio.create_task(ended_notice(project,rpc_factory))
            try: await asyncio.wait_for(stop.wait(),.25)
            except asyncio.TimeoutError: pass
    finally:
        await finish()
        if notice:
            notice.cancel();await asyncio.gather(notice,return_exceptions=True)
        status('disconnected')
