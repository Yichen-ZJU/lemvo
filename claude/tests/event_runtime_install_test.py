"""Project hook installation tests; real temporary files/shell, no model or GPU."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT/'skills/research-orchestrator/templates/configure-event-runtime.py'
spec = importlib.util.spec_from_file_location('configure_event_runtime', str(INSTALLER))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

STUB = '''import argparse,json,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('command');p.add_argument('--root',required=True)
a=p.parse_args();incoming=json.load(sys.stdin)
with (Path(a.root)/'observed-events.jsonl').open('a') as f:
 f.write(json.dumps({'event':incoming.get('hook_event_name'),'root':a.root})+'\\n')
print(json.dumps({'continue':True}))
'''


@unittest.skipIf(os.name == 'nt', 'Claude command hooks use the POSIX server shell')
class EventInstall(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="lemvo hooks & 'quote' ")
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.root=self.base/'project with spaces & apostrophe\'';self.root.mkdir()
        self.templates=self.base/'source templates';self.templates.mkdir()
        self.source=self.templates/'event-wait.py';self.source.write_text(STUB)
        self.settings=self.root/'.claude/settings.local.json'

    def install(self, **options):
        return module.install_runtime(self.root,options.pop('engine','claude'),self.templates,**options)

    def save_settings(self, value):
        self.settings.parent.mkdir(exist_ok=True)
        self.settings.write_text(json.dumps(value,indent=3))

    def commands(self):
        value=json.loads(self.settings.read_text())
        return [h['command'] for event in module.EVENTS for group in value['hooks'].get(event,[])
                for h in group['hooks'] if module.owned(h)]

    def test_preserves_permissions_plugins_and_unrelated_hooks(self):
        old={'permissions':{'allow':['Read'],'deny':['Bash(rm:*)']},
             'enabledPlugins':{'my-plugin@example':True},'env':{'TEST':'keep'},
             'hooks':{'PreToolUse':[{'matcher':'Read','hooks':[{'type':'command','command':'echo existing'}]}],
                      'PostToolUse':[{'matcher':'Write','hooks':[{'type':'command','command':'echo other'}]}]}}
        self.save_settings(old)
        result=self.install();self.assertTrue(result['active'],result)
        new=json.loads(self.settings.read_text())
        for key in ['permissions','enabledPlugins','env']:self.assertEqual(new[key],old[key])
        self.assertEqual(new['hooks']['PostToolUse'],old['hooks']['PostToolUse'])
        self.assertEqual(new['hooks']['PreToolUse'][0],old['hooks']['PreToolUse'][0])
        self.assertEqual(len(self.commands()),3)
        self.assertFalse(result['permissions_changed'])

    def test_repeated_install_no_duplicate_and_settings_mtime_stable(self):
        first=self.install();stamp=self.settings.stat().st_mtime_ns
        second=self.install()
        self.assertEqual(len(self.commands()),3)
        self.assertEqual(first['runtime_script'],second['runtime_script'])
        self.assertEqual(self.settings.stat().st_mtime_ns,stamp)

    def test_space_and_shell_meta_paths_execute_real_hook_interface(self):
        result=self.install();self.assertTrue(result['active'])
        for event,command in zip(module.EVENTS,self.commands()):
            run=subprocess.run(command,shell=True,cwd=str(self.base),
                input=json.dumps({'hook_event_name':event}),universal_newlines=True,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertTrue(json.loads(run.stdout)['continue'])
        lines=[json.loads(line) for line in (self.root/'observed-events.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in lines],list(module.EVENTS))
        self.assertTrue(all(r['root']==str(self.root.resolve()) for r in lines))

    def test_new_version_keeps_old_immutable_and_refreshes_only_managed_commands(self):
        first=self.install();old=Path(first['runtime_script']);payload=old.read_bytes()
        self.source.write_text(STUB+'\n# new source version\n')
        second=self.install();self.assertTrue(second['active'])
        self.assertNotEqual(first['runtime_version'],second['runtime_version'])
        self.assertEqual(old.read_bytes(),payload)
        self.assertEqual(len(self.commands()),3)
        self.assertTrue(all(shlex.split(c,comments=True)[1]==second['runtime_script']
                            for c in self.commands()))

    def test_bad_settings_json_preserved_and_inactive(self):
        self.settings.parent.mkdir();self.settings.write_text('{"hooks":')
        before=self.settings.read_bytes();result=self.install()
        self.assertFalse(result['active']);self.assertEqual(result['status'],'invalid_settings')
        self.assertEqual(self.settings.read_bytes(),before)

    def test_wrong_hook_schema_preserved(self):
        self.save_settings({'hooks':{'PreToolUse':{'invalid':'not list'}},'permissions':{'allow':['Read']}})
        before=self.settings.read_bytes();result=self.install()
        self.assertFalse(result['active']);self.assertEqual(result['status'],'invalid_hook_schema')
        self.assertEqual(self.settings.read_bytes(),before)

    def test_absent_helper_does_not_write_settings(self):
        self.source.unlink();result=self.install()
        self.assertFalse(result['active']);self.assertEqual(result['status'],'helper_missing')
        self.assertFalse(self.settings.exists())

    def test_codex_does_not_claim_claude_native_hooks_or_change_settings(self):
        self.save_settings({'hooks':{},'permissions':{'allow':['Read']}})
        before=self.settings.read_bytes();result=self.install(engine='codex')
        self.assertFalse(result['active']);self.assertEqual(result['status'],'codex_controller_only')
        self.assertEqual(self.settings.read_bytes(),before)

    def test_existing_disable_all_hooks_respected(self):
        self.save_settings({'disableAllHooks':True,'hooks':{}})
        before=self.settings.read_bytes();result=self.install()
        self.assertFalse(result['active']);self.assertEqual(result['status'],'disabled_by_project')
        self.assertEqual(self.settings.read_bytes(),before)

    def test_explicit_disable_persists_and_enable_restores(self):
        self.assertTrue(self.install()['active'])
        result=self.install(disabled=True);self.assertFalse(result['active'])
        self.assertEqual(self.commands(),[])
        result=self.install();self.assertFalse(result['active']);self.assertTrue(result['disabled'])
        self.assertEqual(self.commands(),[])
        result=self.install(enable=True);self.assertTrue(result['active']);self.assertEqual(len(self.commands()),3)

    def test_bad_installation_record_is_not_overwritten(self):
        path=self.root/'.runtime-events/installation.json';path.parent.mkdir();path.write_text('partial{')
        result=self.install();self.assertFalse(result['active'])
        self.assertEqual(result['status'],'invalid_installation_record')
        self.assertEqual(path.read_text(),'partial{')
        self.assertFalse(self.settings.exists())

    def test_project_symlink_escape_not_written(self):
        outside=self.base/'outside';outside.mkdir();external=outside/'settings.local.json'
        external.write_text('{"permissions":{"allow":["Read"]}}')
        (self.root/'.claude').symlink_to(outside,target_is_directory=True)
        before=external.read_bytes();result=self.install()
        self.assertFalse(result['active']);self.assertEqual(result['status'],'project_path_escape')
        self.assertEqual(external.read_bytes(),before)

    def test_foreign_hook_in_managed_group_is_preserved(self):
        self.install();data=json.loads(self.settings.read_text())
        group=data['hooks']['Stop'][0];group['hooks'].append({'type':'command','command':'echo foreign'})
        self.settings.write_text(json.dumps(data));self.install()
        new=json.loads(self.settings.read_text())
        self.assertEqual(new['hooks']['Stop'][0]['hooks'],[{'type':'command','command':'echo foreign'}])
        self.assertEqual(len(self.commands()),3)

    def test_cli_missing_helper_is_machine_notice_not_research_stop(self):
        self.source.unlink()
        run=subprocess.run([sys.executable,str(INSTALLER),'install','--root',str(self.root),
            '--engine','claude','--templates',str(self.templates)],stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,universal_newlines=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertFalse(json.loads(run.stdout)['active'])


if __name__=='__main__':
    unittest.main(verbosity=2)
