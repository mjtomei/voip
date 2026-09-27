import json
from pathlib import Path
from unittest.mock import patch, Mock

from service_fixture import ServiceFixture
from directory_service import Runtime, ensure_project, home_target, transfer_target, policy, preview, public_map, snapshot, tick
from directory_routes import directory_id
from service_config import access_bytes
from voice_config import resolve_recipient
from voip import Project, SetupError, atomic_write
import time


class DirectoryServiceTests(ServiceFixture):
    def enable(self):
        self.work = self.root/'work'; self.work.mkdir()
        config = self.root/'server.toml'
        config.write_text(config.read_text()+'\n[directory_extensions]\nenabled=true\nroot='+json.dumps(str(self.work))+'\nrecipients=["alice"]\n')
        return Project(self.root)

    def observed(self, *relative):
        return {'root':str(self.work), 'mode':'smart', 'observed_at':100,
                'directories':[{'directory':str(self.work/p)} for p in relative]}

    def test_generated_working_directory_is_separate_from_private_state_and_resumes_identity(self):
        provider = self.enable()
        workspace = self.work/'project'; workspace.mkdir()
        project = ensure_project(provider, str(workspace))
        self.assertEqual(project.workspace, workspace)
        self.assertTrue(project.root.is_relative_to(provider.state))
        self.assertEqual(list(workspace.iterdir()), [])
        atomic_write(project.state/'operator-thread-id', self.project.thread_id())
        same = ensure_project(provider, str(workspace))
        self.assertEqual(same.thread_id(), self.project.thread_id())
        self.assertEqual(Project(self.root, project.session_id).workspace, workspace)
        self.assertLessEqual(len(project.sip['username']), 32)
        other = ensure_project(provider, str(self.work/'other'))
        self.assertNotEqual(project.sip['username'], other.sip['username'])
        self.assertEqual(resolve_recipient(project, 'alice')['id'], 'alice')
        with self.assertRaises(SetupError): resolve_recipient(project, 'bob')

    def test_home_target_resolves_published_directory_and_never_guesses_default(self):
        provider=self.enable();home=ensure_project(provider,str(self.work))
        data={'observed_at':time.time(),'plan':{'default':str(self.work)},
              'targets':{home.session_id:{'session':home.session_id,'username':home.sip['username']}}}
        path=provider.state/'directory-status.json';atomic_write(path,json.dumps(data))
        self.assertEqual(home_target(provider),home.session_id)
        self.assertNotEqual(home_target(provider),'default')
        from voice_config import operator_identity
        self.assertEqual(operator_identity(home)['label'],'home directory operator')
        self.assertIn(str(self.work),home.instructions()['developerInstructions'])
        data['targets'][home.session_id]={'session':'default','username':provider.sip['username']}
        atomic_write(path,json.dumps(data))
        with self.assertRaisesRegex(SetupError,'does not match'):home_target(provider)
        data['observed_at']=0;atomic_write(path,json.dumps(data))
        with self.assertRaisesRegex(SetupError,'not current'):home_target(provider)
        path.unlink()
        with self.assertRaises(SetupError):home_target(provider)

    def test_generated_operators_use_explicit_provider_tmux_namespace(self):
        provider = self.enable()
        provider.operator['tmux_socket'] = '/tmp/container-operators.sock'
        project = ensure_project(provider, str(self.work / 'inside'))
        self.assertEqual(project.operator['tmux_socket'], '/tmp/container-operators.sock')
        provider.operator['tmux_socket'] = ''
        other = ensure_project(provider, str(self.work / 'outside'))
        self.assertEqual(other.operator['tmux_socket'], str(provider.provider_state / 'directory-tmux.sock'))

    def test_cli_home_transfer_resolves_before_using_existing_call_control(self):
        import contextlib,io
        from voip import main
        provider=self.enable();home=ensure_project(provider,str(self.work))
        atomic_write(provider.state/'directory-status.json',json.dumps({'observed_at':time.time(),
            'plan':{'default':str(self.work)},'targets':{home.session_id:{'session':home.session_id,'username':home.sip['username']}}}))
        args=['voip.py','--root',str(self.root),'call','transfer','--to-home','--call-id','local',
              '--request-key','home-test','--note','Caller requested home']
        with patch('sys.argv',args),patch('call_control.Control') as control,contextlib.redirect_stdout(io.StringIO()):
            control.return_value.transfer.return_value={'state':'submitted'}
            main()
        control.return_value.transfer.assert_called_once_with('local',home.session_id,'home-test','Caller requested home')

    def test_default_transfer_alias_is_home_and_provider_directory_name_is_explicit(self):
        provider=self.enable();home=ensure_project(provider,str(self.work))
        path=provider.state/'directory-status.json'
        atomic_write(path,json.dumps({'observed_at':time.time(),'plan':{'default':str(self.work)},
            'targets':{home.session_id:{'session':home.session_id,'username':home.sip['username']}}}))
        self.assertEqual(transfer_target(provider,'default'),home.session_id)
        self.assertEqual(transfer_target(home,'default'),home.session_id)
        self.assertEqual(transfer_target(home,provider.workspace.name),'default')
        self.assertEqual(transfer_target(home,'team'),'team')
        from voice_config import operator_identity
        self.assertEqual(operator_identity(provider)['transfer_destination'],provider.workspace.name)
        path.unlink()
        with self.assertRaisesRegex(SetupError,'not current'):transfer_target(provider,'default')
        # Unavailable home does not hide the explicitly requested provider project.
        self.assertEqual(transfer_target(home,provider.workspace.name),'default')

    def test_provider_directory_alias_does_not_shadow_a_registered_session(self):
        provider=self.enable()
        identity=provider.workspace.name
        atomic_write(self.root/'sessions'/(identity+'.toml'),'control_port=23456\n')
        self.assertEqual(transfer_target(provider,identity),identity)

    def test_default_transfer_without_directory_routing_is_unchanged(self):
        self.assertEqual(transfer_target(self.project,'default'),'default')
        self.assertEqual(transfer_target(self.project,'team'),'team')

    def test_automatic_routes_do_not_change_static_asset_hash_on_every_tmux_change(self):
        provider = self.enable()
        before = access_bytes(provider)
        ensure_project(provider, str(self.work/'alpha'))
        ensure_project(provider, str(self.work/'beta'))
        self.assertEqual(access_bytes(Project(self.root)), before)
        cfg = json.loads(before)
        self.assertEqual(cfg['directories']['recipients'], ['alice'])
        self.assertNotIn(str(self.work), before.decode())

    def test_legacy_provider_can_omit_default_sip_control_port(self):
        provider = self.enable()
        path = provider.server_path
        lines = path.read_text().splitlines()
        path.write_text('\n'.join(line for line in lines if not line.strip().startswith('control_port')) + '\n')
        provider = Project(self.root)
        self.assertNotIn('control_port', provider.sip)
        project = ensure_project(provider, str(self.work/'legacy'))
        self.assertNotEqual(project.sip['control_port'], 4444)

    def test_preview_is_read_only_and_manual_routes_reserve_their_numbers(self):
        provider = self.enable()
        (self.root/'sessions/team.toml').write_text('extension="1"\ncontrol_port=12345\n')
        result = preview(provider, self.observed('alpha', 'beta'))
        self.assertEqual(set(result['extensions']), {'2','3'})
        self.assertFalse((provider.state/'directory-index.json').exists())

    def test_public_map_has_no_paths_and_generation_binds_target_identity(self):
        provider = self.enable()
        plan = preview(provider, self.observed('alpha'))
        identity = directory_id(self.work/'alpha')
        targets = {identity:{'session':identity,'username':'operator-'+identity}}
        first = public_map(provider, plan, targets, now=100)
        second = public_map(provider, plan, targets, now=110)
        self.assertEqual(first['generation'], second['generation'])
        self.assertNotEqual(first['expires_at'], second['expires_at'])
        self.assertNotIn(str(self.work), json.dumps(first))
        changed = public_map(provider, preview(provider, self.observed('alpha','beta')), targets, now=100)
        self.assertNotEqual(first['generation'], changed['generation'])

    def test_stale_or_mismatched_host_snapshot_is_rejected(self):
        provider = self.enable()
        provider.config['directory_extensions']['snapshot_file'] = 'state/snapshot.json'
        atomic_write(provider.state/'snapshot.json', json.dumps(self.observed('alpha')))
        with patch('directory_service.time.time', return_value=200):
            with self.assertRaises(SetupError): snapshot(provider)
        with patch('directory_service.time.time', return_value=110):
            self.assertEqual(snapshot(provider)['root'], str(self.work))

    def test_disappearing_directory_removes_admission_without_stopping_its_operator(self):
        provider = self.enable()
        class Runtime:
            def __init__(self): self.started=[]; self.maps=[]; self.owned={}
            def start(self, p): self.started.append(p.workspace); self.owned[p.session_id]=p
            def ready(self, p): return True
            def publish(self, m): self.maps.append(m)
        runtime = Runtime()
        with patch('directory_service.snapshot', return_value=self.observed('alpha')):
            tick(provider, runtime)
        with patch('directory_service.snapshot', return_value=self.observed()):
            tick(provider, runtime)
        identity = directory_id(self.work/'alpha')
        self.assertIn(identity, runtime.maps[0]['targets'])
        self.assertNotIn(identity, runtime.maps[1]['targets'])
        self.assertTrue((provider.state/'directory-projects'/identity).exists())
        self.assertEqual(runtime.maps[1]['routes'], {})
        self.assertEqual(runtime.started.count(self.work/'alpha'), 2)

    def test_outgoing_readiness_uses_current_observer_without_systemd(self):
        from outgoing_calls import Calls
        from twilio_source import function_source
        from voip import ROOT
        import hashlib
        provider = self.enable()
        calls = object.__new__(Calls)
        calls.project = provider
        calls.deployment = {'build_sid':'same', 'deployed_build_sid':'same',
            'functions':{'outgoing':{'sha256':hashlib.sha256(function_source(ROOT,'outgoing')).hexdigest()}},
            'access_asset':{'sha256':hashlib.sha256(access_bytes(provider)).hexdigest()}}
        with patch('outgoing_calls.operator_status', return_value={'voice':'connected','codex_process_alive':True}), \
             patch('outgoing_calls.run', side_effect=AssertionError('systemd must not be required')), \
             patch('endpoint_health.voice_health', return_value={'ok': True}), \
             patch('endpoint_health.sip_health', return_value={'ok': True}), \
             patch('call_events.CallEvents.load', return_value={'observer_online':True,'observed_at':time.time()}):
            calls.check_ready()
        with patch('outgoing_calls.operator_status', return_value={'voice':'connected','codex_process_alive':True}), \
             patch('call_events.CallEvents.load', return_value={'observer_online':True,'observed_at':0}):
            with self.assertRaisesRegex(SetupError, 'observer'): calls.check_ready()

    def test_route_publication_uses_a_stable_document_and_conditional_updates(self):
        runtime = object.__new__(Runtime)
        runtime.deployment = {'pin_sync_service_sid':'IS'+'a'*32}
        runtime.api = Mock()
        runtime.api.request.side_effect = [SetupError('HTTP 404, missing'), {}, {'revision':'7'}, {}]
        runtime.publish({'v':1})
        create = runtime.api.request.call_args_list[1]
        self.assertTrue(create.args[0].endswith('/Documents'))
        self.assertEqual(create.args[1]['UniqueName'], 'directory-routes')
        runtime.publish({'v':1, 'generation':'next'})
        update = runtime.api.request.call_args_list[3]
        self.assertTrue(update.args[0].endswith('/Documents/directory-routes'))
        self.assertEqual(update.kwargs, {'revision':'7'})

    def test_endpoint_recovery_does_not_recreate_credentials_threads_or_running_children(self):
        provider = self.enable()
        project = ensure_project(provider, str(self.work/'alpha'))
        runtime = object.__new__(Runtime)
        runtime.owned, runtime.children = {}, {}
        runtime.deployment = {'account_sid':'AC'+'a'*32, 'credential_list_sid':'CL'+'b'*32}
        runtime.api = Mock()
        runtime.api.items.return_value = []
        child = Mock(); child.poll.return_value = None
        with patch('voip.audio_up'), patch('voip.sip_configure'), \
             patch('voip.operator_init', side_effect=lambda p: atomic_write(p.state/'operator-thread-id', self.project.thread_id())) as initialize, \
             patch('voip.operator_ensure') as ensure, patch('directory_service.subprocess.Popen', return_value=child) as spawn:
            runtime.start(project)
            runtime.start(project)
            initialize.assert_called_once()
            self.assertEqual(ensure.call_count, 2)
            self.assertEqual(spawn.call_count, 3)
            runtime.api.request.assert_called_once()
        self.assertEqual((project.state/'twilio-sip-secret').stat().st_mode & 0o777, 0o600)
