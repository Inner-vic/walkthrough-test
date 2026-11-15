"""Readiness must fail before a new job sends queries or downloads inputs."""
import argparse
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import analyzer_contract
import native_preflight as native
import walkthrough_worker as worker
from test_analyzer_contract import synthetic_contract_audit


def good_receipt():
    return {'status': 'native_preflight_passed', 'python': '3.11.synthetic',
            'analyzer_contract': synthetic_contract_audit(),
            'dependencies': {name: 'synthetic' for name in native.NATIVE_MODULES},
            'network_requests': 0, 'query_sent': False, 'source_files_present': True}


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'general_performance').mkdir()
        (self.root / 'general_performance/o.ini').write_text('[PARAMETER]\n', encoding='utf-8')
        self.cfg = {'execution': 'wsl', 'distribution': 'Ubuntu',
                    'python': '/opt/analyzer/bin/python', 'analyzer_root': '/opt/current-analyzer'}

    def test_unknown_source_fails_before_dependency_imports(self):
        with patch.object(analyzer_contract, 'validate_analyzer_contract', side_effect=ValueError('unsupported source')), \
             patch.object(native.importlib, 'import_module', side_effect=AssertionError('no imports')):
            with self.assertRaisesRegex(ValueError, 'unsupported source'):
                native.probe_native(self.root)

    def test_missing_additional_dependency_reports_name_without_import_error_contents(self):
        def imports(name):
            if name == 'requests_toolbelt':
                raise ImportError('sensitive arbitrary details')
            return types.SimpleNamespace(__version__='synthetic')
        with patch.object(analyzer_contract, 'validate_analyzer_contract', return_value=synthetic_contract_audit()), \
             patch.object(native.shutil, 'which', return_value='/bin/tool'), \
             patch.object(native.importlib, 'import_module', side_effect=imports):
            with self.assertRaisesRegex(ValueError, 'requests_toolbelt') as caught:
                native.probe_native(self.root)
        self.assertNotIn('sensitive', str(caught.exception))

    def test_dependency_probe_covers_actual_import_graph(self):
        with patch.object(analyzer_contract, 'validate_analyzer_contract', return_value=synthetic_contract_audit()), \
             patch.object(native.shutil, 'which', return_value='/bin/tool'), \
             patch.object(native.importlib, 'import_module', return_value=types.SimpleNamespace(__version__='synthetic')) as imported:
            result = native.probe_native(self.root)
        self.assertEqual({c.args[0] for c in imported.call_args_list}, set(native.NATIVE_MODULES))
        self.assertEqual(result['network_requests'], 0)
        self.assertFalse(result['query_sent'])

    def test_host_requires_full_checked_contract_and_dependency_receipt(self):
        payload = good_receipt()
        with patch.object(native.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as run:
            result = native.check_native_environment(self.cfg)
        self.assertEqual(result, payload)
        self.assertEqual(run.call_args.args[0][:4], ['wsl.exe', '-d', 'Ubuntu', '--exec'])
        self.assertEqual(run.call_args.args[0][-2:], ['--analyzer-root', '/opt/current-analyzer'])

    def test_status_ok_only_or_missing_extra_dependency_cannot_pass(self):
        missing = good_receipt()
        missing['dependencies'].pop('PIL')
        for payload in ({'status': 'ok'}, missing):
            with self.subTest(payload=payload.get('status')), \
                 patch.object(native.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(payload))):
                with self.assertRaises(native.NativePreflightError):
                    native.check_native_environment(self.cfg)

    def test_changed_source_contract_cannot_pass(self):
        payload = good_receipt()
        payload['analyzer_contract']['contract_fingerprint'] = 'f' * 64
        with patch.object(native.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(payload))):
            with self.assertRaisesRegex(native.NativePreflightError, '源码契约'):
                native.check_native_environment(self.cfg)

    def test_job_readiness_failure_precedes_identity_or_spawn(self):
        request = {'baseline_metadata': {'vin': 'SYNTHETIC'}, 'replacement_metadata': {'vin': 'SYNTHETIC'}}
        settings = {'runtime_state_dir': str(self.root), 'local_analysis': self.cfg,
                    'lark_profile': 'synthetic-user', 'lark_home': str(self.root),
                    'expected_lark_app_id': 'synthetic-app', 'expected_operator_open_id': 'synthetic-user'}
        context = {'source_chat_id': 'synthetic-chat', 'source_message_id': 'synthetic-message'}
        args = argparse.Namespace(text='走查测试 synthetic', ccconnect_context='synthetic', settings=self.root/'settings.json')
        with patch.object(worker, 'route', return_value={'status': 'ready', 'normalized': request}), \
             patch.object(worker, 'request_identity', return_value=context), \
             patch.object(worker, 'load_settings', return_value=settings), \
             patch.object(worker.product, 'validate_config', return_value={'synthetic': True}), \
             patch.object(worker, 'check_native_environment', side_effect=native.NativePreflightError('source unsupported')), \
             patch.object(worker.policy, 'activate_lark_environment', side_effect=AssertionError('no identity actions')), \
             patch.object(worker.subprocess, 'Popen', side_effect=AssertionError('no background job')):
            with self.assertRaisesRegex(native.NativePreflightError, 'source unsupported'):
                worker.start_job(args)
        self.assertEqual(list(self.root.glob('walkthrough-*.json')), [])


if __name__ == '__main__':
    unittest.main()
