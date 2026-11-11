import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CliTests(unittest.TestCase):
    def test_real_inspection(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','samples'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['files'])
    def test_invalid_path_is_failure(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','inspect','missing-directory'],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,2);self.assertEqual(result.stdout,'')
    def test_exposure_command_and_output(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'result.json'
            result=subprocess.run([sys.executable,'-m','datapack.cli','exposure','samples',
                '--start','2026-11-10T23:59:40Z','--end','2026-11-11T00:01:00Z',
                '--output',str(out)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            payload=json.loads(out.read_text(encoding='utf-8'))
            self.assertIn('result_fingerprint',payload)
            self.assertTrue(payload['vehicles'])
            # 重复运行输出完全一致（幂等指纹）。
            result2=subprocess.run([sys.executable,'-m','datapack.cli','exposure','samples',
                '--start','2026-11-10T23:59:40Z','--end','2026-11-11T00:01:00Z',
                '--output',str(out)],cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(result2.returncode,0,result2.stderr)
            self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['result_fingerprint'],
                             payload['result_fingerprint'])
    def test_exposure_rejects_naive_time(self):
        result=subprocess.run([sys.executable,'-m','datapack.cli','exposure','samples',
            '--start','2026-11-10T23:59:40','--end','2026-11-11T00:01:00Z'],
            cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
