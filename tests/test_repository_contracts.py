"""Portable export regressions. No real identities, network, jobs or scheduled-task mutations."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from check_repository import scan


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = load('export_router', ROOT / 'workspace/.agents/skills/chassis-business-routing/scripts/intent_router.py')
        sys.path.insert(0, str(ROOT / 'workspace/deployment'))
        cls.auth = load('export_auth', ROOT / 'workspace/deployment/feishu_auth_maintenance.py')

    def test_repository_requests_never_create_vehicle_tasks(self):
        for text in ('帮我把走查测试这一功能上传到Gitlab上，请整理相关文件、资料，写好README',
                     '把走查测试上传到 GitLab，历史 VIN DEM0VEH1CLE000001 2026-11-03 09:53-10:10',
                     '整理走查测试源码并写 README', '走查测试打包部署包'):
            with self.subTest(text=text), patch.object(self.router.subprocess, 'run') as process:
                result = self.router.route(text)
                self.assertEqual(result['route'], 'none')
                self.assertEqual(result['missing'], [])
                process.assert_not_called()

    def test_explicit_execution_is_not_hidden_by_repository_work(self):
        text = '执行走查测试 DEM0VEH1CLE000001，同时将代码上传到 GitLab'
        with patch.object(self.router.subprocess, 'run', return_value=type('Result', (), {
            'stdout': json.dumps({'route':'walkthrough-test','status':'needs_input','message':'missing'})})()) as process:
            result = self.router.route(text)
        self.assertEqual(result['route'], 'walkthrough-test')
        process.assert_called_once()

    def test_auth_adapter_rejects_missing_or_foreign_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(self.auth.MaintenanceError):
                self.auth.load_worker(root)
            (root / 'deployment').mkdir()
            (root / 'deployment/walkthrough-test.installation.json').write_text(json.dumps({
                'workspace_root':str(root), 'skill_root':str(root / 'foreign'),
                'execution_release':'walkthrough-target16-hardening'}))
            with self.assertRaises(self.auth.MaintenanceError):
                self.auth.load_worker(root)

    def test_auth_adapter_uses_walkthrough_policy_without_ad_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / 'runtime/wtr096-hardening/skill/scripts'
            scripts.mkdir(parents=True)
            (root / 'deployment').mkdir()
            (scripts / 'report_policy.py').write_text('MARKER = "walkthrough-policy"\n')
            (root / 'deployment/walkthrough-test.installation.json').write_text(json.dumps({
                'workspace_root':str(root), 'skill_root':str(scripts.parent),
                'execution_release':'walkthrough-target16-hardening'}))
            before = list(sys.path)
            try:
                worker = self.auth.load_worker(root)
                self.assertEqual(worker.MARKER, 'walkthrough-policy')
                self.assertEqual(worker.WORKSPACE_ROOT, root.resolve())
            finally:
                sys.path[:] = before

    def test_private_paths_and_credentials_are_rejected(self):
        cases = [('private/config.json', b'{}'), ('.env', b'A=1'), ('tool.exe', b'MZ'),
                 ('example.py', ('TOKEN="' + 'gl' + 'pat-' + 'a'*24 + '"').encode()),
                 ('example.json', ('{"id":"ou_' + 'a'*32 + '"}').encode())]
        for name, raw in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                scan(name, raw)

    def test_synthetic_fixture_and_example_config_are_allowed(self):
        scan('tests/fixture.py', b'VIN="DEM0VEH1CLE000001"\n')
        scan('examples/settings.windows.example.json', (ROOT / 'examples/settings.windows.example.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
