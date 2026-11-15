"""Real synthetic Windows Job tests, bounded to this private test tree."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid

import native_windows_process as process

BASE = Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.name == 'nt', 'Windows process-tree tests')
class WindowsProcessTests(unittest.TestCase):
    def setUp(self):
        self.root = BASE / 'w26/process-tests' / uuid.uuid4().hex[:12]
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.env = dict(os.environ, PYTHONUTF8='1', TZ='CST-8')

    def cleanup(self):
        assert self.root.resolve().parent == (BASE / 'w26/process-tests').resolve()
        shutil.rmtree(self.root)

    def test_normal_hidden_child_preserves_explicit_argv_unicode_and_exitcode(self):
        source = self.root / 'args 中文.py'
        source.write_text('import sys,json\nprint(json.dumps(sys.argv[1:],ensure_ascii=False))\n', encoding='utf-8')
        result = process.run_windows_tree([sys.executable, '-X', 'utf8', str(source), 'with space', '中文'],
                                          env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), ['with space', '中文'])
        self.assertEqual(result.stderr, '')

    def test_timeout_stops_grandchild_without_stopping_unrelated_process(self):
        source = self.root / 'tree.py'
        marker = self.root / 'heartbeat'
        source.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
            'if "--child" in sys.argv:\n'
            ' while True:\n  Path(sys.argv[-1]).write_text(str(time.time()))\n  time.sleep(.05)\n'
            'else:\n subprocess.Popen([sys.executable,__file__,"--child",sys.argv[-1]])\n time.sleep(60)\n', encoding='utf-8')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.run_windows_tree([sys.executable, str(source), str(marker)], env=self.env, timeout=2)
            self.assertTrue(marker.is_file(), 'grandchild must really have run before timeout')
            value = marker.read_bytes()
            time.sleep(.3)
            self.assertEqual(marker.read_bytes(), value, 'grandchild continued after Job timeout')
            self.assertIsNone(unrelated.poll(), 'timeout affected a process outside this Job')
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)

    def test_assignment_failure_never_releases_analysis(self):
        marker = self.root / 'must-not-exist'
        with patch.object(process._Job, 'assign', side_effect=process.WindowsProcessError('synthetic assign failure')):
            with self.assertRaises(process.WindowsProcessError):
                process.run_windows_tree([sys.executable, '-c',
                    'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("started")'],
                    env=self.env, timeout=5)
        self.assertFalse(marker.exists())

    def test_invalid_or_shell_style_argv_is_not_started(self):
        for command in ('python -c anything', [], ['python', '\x00']):
            with self.subTest(command=command), self.assertRaises(process.WindowsProcessError):
                process.run_windows_tree(command, env=self.env, timeout=1)

    def nested_script(self, body):
        source = self.root / 'nested outer 中文.py'
        source.write_text('import json,os,subprocess,sys,time\nfrom pathlib import Path\n'
            'sys.path.insert(0,' + repr(str(Path(process.__file__).resolve().parent)) + ')\n'
            'from native_windows_process import run_windows_tree\n' + body,
            encoding='utf-8')
        return source

    def heartbeat_tree(self):
        source = self.root / 'nested heartbeat.py'
        source.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
            'if "--child" in sys.argv:\n'
            ' while True:\n  Path(sys.argv[-1]).write_text(str(time.time()))\n  time.sleep(.05)\n'
            'else:\n subprocess.Popen([sys.executable,__file__,"--child",sys.argv[-1]])\n time.sleep(60)\n',
            encoding='utf-8')
        return source

    def test_actual_nested_jobs_normal_return_preserves_unicode_stdout(self):
        leaf = "import json;print(json.dumps(['inner with space','中文'],ensure_ascii=False))"
        outer = self.nested_script(
            'r=run_windows_tree([sys.executable,"-X","utf8","-c",' + repr(leaf) + '],'
            'env=dict(os.environ),timeout=10)\n'
            'print(json.dumps({"inner_returncode":r.returncode,"inner_stdout":json.loads(r.stdout)},ensure_ascii=False))\n')
        result = process.run_windows_tree([sys.executable, '-X', 'utf8', str(outer)],
                                          env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
            {'inner_returncode': 0, 'inner_stdout': ['inner with space', '中文']})

    def test_actual_nested_inner_timeout_stops_inner_tree_but_outer_continues(self):
        tree = self.heartbeat_tree()
        marker = self.root / 'nested-inner-heartbeat'
        outer = self.nested_script(
            'try:\n'
            ' run_windows_tree([sys.executable,' + repr(str(tree)) + ',' + repr(str(marker)) + '],'
            'env=dict(os.environ),timeout=2)\n'
            ' raise AssertionError("inner did not time out")\n'
            'except subprocess.TimeoutExpired:\n'
            ' first=Path(' + repr(str(marker)) + ').read_bytes()\n'
            ' time.sleep(.3)\n'
            ' stable=Path(' + repr(str(marker)) + ').read_bytes()==first\n'
            ' print(json.dumps({"outer_continued":True,"inner_tree_stopped_before_outer_exit":stable}))\n')
        result = process.run_windows_tree([sys.executable, '-X', 'utf8', str(outer)],
                                          env=self.env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
            {'outer_continued': True, 'inner_tree_stopped_before_outer_exit': True})

    def test_actual_nested_outer_timeout_stops_inner_descendants_only(self):
        tree = self.heartbeat_tree()
        marker = self.root / 'nested-outer-heartbeat'
        outer = self.nested_script(
            'run_windows_tree([sys.executable,' + repr(str(tree)) + ',' + repr(str(marker)) + '],'
            'env=dict(os.environ),timeout=60)\n')
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.run_windows_tree([sys.executable, '-X', 'utf8', str(outer)],
                                          env=self.env, timeout=3)
            self.assertTrue(marker.is_file(), 'the nested grandchild must actually run')
            before = marker.read_bytes()
            time.sleep(.3)
            self.assertEqual(marker.read_bytes(), before)
            self.assertIsNone(unrelated.poll(), 'nested Job cleanup affected an unrelated process')
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
