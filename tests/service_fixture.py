import tempfile
from pathlib import Path
import unittest
from voip import Project, ROOT, atomic_write

def single_recipient(root):
    (root / 'recipients.toml').write_text('''default_recipient="alice"
[recipients.alice]
number="+12025550101"
pin="123456"
sessions=["default"]
incoming_session="default"
''')

class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'server.toml').write_text((ROOT / 'config.example.toml').read_text() + '\n[twilio]\nphone_number = "+12025550100"\n')
        (self.root / 'sessions').mkdir()
        (self.root / 'sessions/team.toml').write_text('reset_on_outgoing_call = false\ncontrol_port = 12345\n')
        self.contacts = '''default_recipient = "alice"
[recipients.alice]
number = "+12025550101"
pin = "123456"
sessions = ["default", "team"]
incoming_session = "default"
[recipients.bob]
number = "+12025550102"
pin = "65432100"
sessions = ["team"]
incoming_session = "team"
'''
        (self.root / 'recipients.toml').write_text(self.contacts)
        self.project = Project(self.root)
        self.team = Project(self.root, 'team')
        for n, p in enumerate((self.project, self.team), 1):
            atomic_write(p.state / 'operator-thread-id', str(n) * 8 + '-1111-1111-1111-111111111111')
