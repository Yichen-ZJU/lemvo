"""Exercise the released adapter entry, not just hook installation in isolation."""
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_dispatch_test as base


class EventAdapter(unittest.TestCase):
    setUp = base.Dispatch.setUp
    adapter = base.Dispatch.adapter

    def test_actual_claude_launch_sees_project_hooks_and_wait_environment(self):
        script = self.bin/'claude'
        script.write_text(base.FAKE.replace(
            "args=sys.argv[1:]", """args=sys.argv[1:]
install=json.loads(Path('.runtime-events/installation.json').read_text())
settings=json.loads(Path('.claude/settings.local.json').read_text())
Path('launch-configuration.json').write_text(json.dumps({
 'active':install['active'], 'hooks':list(settings['hooks']),
 'ceiling':os.environ.get('CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS')}))"""))
        r=self.adapter()
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        seen=json.loads((self.p/'launch-configuration.json').read_text())
        self.assertTrue(seen['active'])
        self.assertEqual(seen['ceiling'],'0')
        self.assertIn('PreToolUse',seen['hooks'])
        self.assertTrue(list((self.p/'turns').glob('*/event-runtime-install.json')))

    def test_bad_existing_settings_are_preserved_without_scientific_stop(self):
        settings=self.p/'.claude/settings.local.json'
        settings.parent.mkdir()
        settings.write_text('{uncompleted user settings')
        r=self.adapter()
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertEqual(settings.read_text(),'{uncompleted user settings')
        self.assertFalse(json.loads((self.p/'.runtime-events/installation.json').read_text())['active'])
        self.assertTrue((self.p/'src/method.py').exists())

    def test_codex_path_does_not_install_claude_hooks(self):
        r=self.adapter(engine='codex')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.p/'.claude/settings.local.json').exists())
        self.assertTrue((self.p/'src/method.py').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
