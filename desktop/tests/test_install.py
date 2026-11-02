import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if os.environ.get('LEMVO_TEST_TMPDIR'):
    tempfile.tempdir=os.environ['LEMVO_TEST_TMPDIR']
INSTALL=Path(__file__).resolve().parents[1]/'install.py'

class InstallTests(unittest.TestCase):
    def test_managed_update_preserves_local_edits_and_unmanaged_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'source';dest=root/'skills';home=root/'home'
            for name in ('fixture-managed','fixture-unmanaged'):
                (src/name).mkdir(parents=True)
                (src/name/'SKILL.md').write_text('original')
            (src/'fixture-managed'/'说明🙂.md').write_text('UTF-8 manifest path',encoding='utf-8')
            (dest/'fixture-unmanaged').mkdir(parents=True)
            (dest/'fixture-unmanaged/SKILL.md').write_text('user-file')
            command=[sys.executable,str(INSTALL),'--skills-source',str(src),'--skills-dest',str(dest),'--home',str(home)]
            def run():
                p=subprocess.run(command,capture_output=True,text=True)
                self.assertEqual(p.returncode,0,p.stderr)
                return json.loads(p.stdout)
            first=run()
            self.assertEqual(first['install_directories'],1)
            self.assertEqual((dest/'fixture-unmanaged/SKILL.md').read_text(),'user-file')
            (src/'fixture-managed/SKILL.md').write_text('new-version')
            run()
            self.assertEqual((dest/'fixture-managed/SKILL.md').read_text(),'new-version')
            (dest/'fixture-managed/SKILL.md').write_text('local-customization')
            last=run()
            self.assertEqual(last['install_directories'],0)
            self.assertEqual((dest/'fixture-managed/SKILL.md').read_text(),'local-customization')
            self.assertTrue((home/'lemvo.ps1').exists())

if __name__=='__main__':unittest.main()
