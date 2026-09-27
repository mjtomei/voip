import json
from pathlib import Path
import shutil
import tempfile
import unittest

from voip import ROOT, Project, atomic_write
from service_config import check_config
from voice_config import resolve_recipient
from pin_security import master_key, verifier


class ReferenceSetupTest(unittest.TestCase):
    def test_provider_and_external_project_templates_form_a_valid_private_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);provider=root/'provider';project=root/'project'
            shutil.copytree(ROOT/'examples/reference',provider)
            shutil.copytree(ROOT/'examples/project',project)
            for directory in (provider,project):
                for path in directory.rglob('*.toml'):
                    text=path.read_text().replace('/home/operator/mobile-project',str(project))
                    text=text.replace('/home/operator/voip',str(provider)).replace('/home/operator',str(root))
                    path.write_text(text)
            p=Project(provider);key=master_key(p,create=True)
            for path,pin in [(provider/'state/pins/alice.json','123456'),
                             (project/'voice/private/bob-pin.json','654321')]:
                atomic_write(path,json.dumps(verifier(pin,key)))
            result=check_config(p)
            self.assertEqual(set(result['sessions']),{'default','daily'})
            self.assertEqual(result['enabled_schedules'],[])
            daily=Project(provider,'daily')
            self.assertEqual(daily.root,project)
            self.assertEqual(p.permissions(),'read-only')
            self.assertEqual(daily.permissions(),'read-only')
            self.assertEqual(resolve_recipient(daily,'alice')['id'],'alice')
            self.assertEqual(resolve_recipient(daily,'bob')['id'],'bob')
            self.assertEqual(daily.profile['redial']['delays_seconds'],[300,600])
            self.assertIn('America/Chicago',daily.instructions()['developerInstructions'])

    def test_container_host_helpers_only_ensure_daemon_and_publish_snapshots(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('host_helpers',ROOT/'scripts/install-host-helpers.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        project=SimpleNamespace(root=Path('/home/operator/voice workspace'))
        with patch.object(module,'settings',return_value={'enabled':True,'snapshot_file':'state/host-directories.json'}):
            units,enabled=module.render(project,'/usr/local/bin/codex',Path('/home/operator/.codex'))
        self.assertIn('voip-host-codex.timer',enabled)
        self.assertEqual(len(enabled),2)
        text='\n'.join(units.values())
        self.assertIn('"app-server" "daemon" "start"',text)
        self.assertIn('KillMode=process', units['voip-host-codex.service'])
        self.assertIn('directory-snapshot.py',text)
        self.assertIn('"/home/operator/voice workspace/state/host-directories.json"',text)
        self.assertNotIn('daemon" "stop',text)
        self.assertNotIn('docker',text)
        with patch.object(module,'settings',return_value={'enabled':True,'snapshot_file':''}):
            with self.assertRaisesRegex(module.SetupError,'snapshot_file'):
                module.render(project,'codex',Path('/home/operator/.codex'))
