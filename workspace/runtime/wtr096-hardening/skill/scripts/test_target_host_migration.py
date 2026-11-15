"""Synthetic host handoff checks; no real settings, credentials or processes."""
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import walkthrough_route as routing
from request_model import InputError


CONTEXT = ('[cc-connect request_context_v=1 sender_id=ou_synthetic platform=feishu '
           'chat_id=oc_synthetic message_id=om_synthetic thread_id=]')
ANCHOR = '2026-10-23T23:59:59+08:00'
TEXT = '走查测试 DEM0VEH1CLE000001 2026-10-21 14:00-14:10，2026-10-21 15:00-15:10'
METADATA = {'baseline_metadata':{'vin':'DEM0VEH1CLE000001','start':'2026-10-21T14:00:00','end':'2026-10-21T14:10:00'},
            'replacement_metadata':{'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:00:00','end':'2026-10-21T15:10:00'},
            'normalization':{'received_at':ANCHOR}}


def load_wrapper(filename):
    workspace = next(p for p in Path(__file__).resolve().parents
                     if (p / 'AGENTS.md').is_file() and (p / '.agents/skills').is_dir())
    candidate = workspace / 'staging/wta081' / filename
    source = candidate if candidate.is_file() else workspace / 'deployment' / filename
    spec = importlib.util.spec_from_file_location('_synthetic_' + filename.replace('.', '_'), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.host = load_wrapper('walkthrough_entry.py')
        self.legacy = load_wrapper('walkthrough_legacy_entry.py')
        self.current_runtime = self.root / 'data/walkthrough-test-windows081/runtime'
        self.old_runtime = self.root / 'data/walkthrough-test-fallback/runtime'
        self.current_runtime.mkdir(parents=True)
        self.old_runtime.mkdir(parents=True)
        self.identity = routing.request_identity(CONTEXT)
        self.key = 'walkthrough-' + hashlib.sha256(self.identity['source_message_id'].encode()).hexdigest()[:24]
        self.old_record = self.old_runtime / (self.key + '.json')
        self.skill = self.root / 'candidate-skill'
        self.skill.mkdir()
        self.settings_path = self.root / 'synthetic-settings.json'
        self.settings = {'runtime_state_dir':str(self.current_runtime), 'execution_release':'synthetic-release',
            'local_analysis':{'backend':'native_full','execution':'windows','environment':'test'},
            'cloud_environment':{key:'synthetic' for key in self.host.TEST_KEYS}}
        self.settings_path.write_text(json.dumps(self.settings), encoding='utf-8')
        self.installation = {'skill_root':str(self.skill),'runtime_state_dir':str(self.current_runtime),
            'execution_release':'synthetic-release',
            'host_python':sys.executable,'settings_path':str(self.settings_path),'execution_enabled':True,
            'legacy':{'runtime_state_dir':str(self.old_runtime),'host_python':sys.executable}}
        self.manifest = self.root / 'installation.json'
        self.manifest.write_text(json.dumps(self.installation), encoding='utf-8')
        self.worker = SimpleNamespace(start_job=Mock(return_value={'status':'queued'}),
            run_worker=Mock(return_value=0), request_identity=routing.request_identity)
        self.preflight = SimpleNamespace(check=Mock(return_value={'status':'local_preflight_passed'}))

    def old(self, **changes):
        record = {'kind':'walkthrough_test_local','job_key':self.key,'status':'completed',
                  'request':METADATA,'created_at':ANCHOR, **self.identity, **changes}
        self.old_record.write_text(json.dumps(record), encoding='utf-8')
        return record

    def main(self, args, *, route_result=None, delegate=None):
        output = []
        decision = route_result or {'status':'ready','normalized':METADATA}
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.host, 'WORKSPACE', self.root))
            stack.enter_context(patch.object(self.host, 'MANIFEST', self.manifest))
            stack.enter_context(patch.object(self.host, 'emit', side_effect=output.append))
            stack.enter_context(patch.object(self.host, 'inspect_static_settings', return_value={'status':'passed'}))
            stack.enter_context(patch.object(sys, 'argv', ['synthetic-host', *args]))
            stack.enter_context(patch.object(sys, 'path', list(sys.path)))
            stack.enter_context(patch.dict('os.environ', {}, clear=False))
            stack.enter_context(patch.dict(sys.modules, {'walkthrough_worker':self.worker,'preflight':self.preflight}))
            route = stack.enter_context(patch.object(routing, 'route', return_value=decision))
            stack.enter_context(patch.object(routing, 'source_normalization_anchor',
                wraps=routing.source_normalization_anchor))
            process = stack.enter_context(patch.object(self.host.subprocess, 'run',
                side_effect=delegate or AssertionError('unexpected child process')))
            code = self.host.main()
        return code, output, route, process

    def test_exact_legacy_source_replay_returns_existing_record_before_preflight(self):
        self.old()
        code, output, route, process = self.main(['start','--text',TEXT,'--ccconnect-context',CONTEXT,
                                                '--received-at','2026-10-26T12:00:00+08:00'])
        self.assertEqual(code, 0)
        self.assertTrue(output[-1]['idempotent'])
        self.assertTrue(output[-1]['legacy_record'])
        self.assertEqual(Path(output[-1]['record_path']), self.old_record)
        self.assertEqual(route.call_args.kwargs['received_at'], ANCHOR)
        self.preflight.check.assert_not_called()
        self.worker.start_job.assert_not_called()
        process.assert_not_called()

    def test_legacy_replay_with_different_chat_or_sender_is_rejected(self):
        self.old()
        for header in (CONTEXT.replace('oc_synthetic','oc_other'), CONTEXT.replace('ou_synthetic','ou_other')):
            with self.subTest(header=header):
                code, output, route, process = self.main(['start','--text',TEXT,'--ccconnect-context',header])
                self.assertEqual(code, 1)
                self.assertIs(output[-1]['task_created'], False)
                self.assertEqual(output[-1]['status'], 'failed')
                route.assert_not_called()
                process.assert_not_called()
        self.worker.start_job.assert_not_called()
        self.preflight.check.assert_not_called()

    def test_changed_legacy_request_is_rejected(self):
        self.old()
        altered = json.loads(json.dumps(METADATA))
        altered['baseline_metadata']['end'] = '2026-10-21T14:11:00'
        code, output, route, process = self.main(['start','--text',TEXT,'--ccconnect-context',CONTEXT],
                                                route_result={'status':'ready','normalized':altered})
        self.assertEqual(code, 1)
        self.assertIs(output[-1]['task_created'], False)
        self.worker.start_job.assert_not_called()
        self.preflight.check.assert_not_called()

    def test_legacy_replay_missing_both_clock_anchors_is_rejected(self):
        request = {key:value for key,value in METADATA.items() if key != 'normalization'}
        self.old(request=request, created_at=None)
        code, output, route, process = self.main(['start','--text',TEXT,'--ccconnect-context',CONTEXT])
        self.assertEqual(code, 1)
        self.assertIs(output[-1]['task_created'], False)
        self.worker.start_job.assert_not_called()

    def test_legacy_explicit_record_delegates_only_to_old_host_with_process_blas_limit(self):
        self.old(status='failed')
        completed = SimpleNamespace(stdout='', stderr='', returncode=0)
        code, output, route, process = self.main(['resume','--record',str(self.old_record)],
                                                delegate=lambda *args,**kwargs:completed)
        self.assertEqual(code, 0)
        command = process.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[4]).name, 'walkthrough_legacy_entry.py')
        self.assertEqual(process.call_args.kwargs['env']['OPENBLAS_NUM_THREADS'], '1')
        self.worker.run_worker.assert_not_called()
        self.worker.start_job.assert_not_called()
        self.preflight.check.assert_not_called()

    def test_preflight_does_not_start_a_job_or_invoke_analysis(self):
        code, output, route, process = self.main(['preflight'])
        self.assertEqual(code, 0)
        self.assertEqual(output[-1]['status'], 'local_preflight_passed')
        self.preflight.check.assert_called_once_with(self.settings_path)
        self.worker.start_job.assert_not_called()
        self.worker.run_worker.assert_not_called()
        process.assert_not_called()

    def test_new_start_forwards_original_context_and_same_normalization_clock(self):
        code, output, route, process = self.main(['start','--text',TEXT,'--ccconnect-context',CONTEXT,
                                                '--received-at',ANCHOR])
        self.assertEqual(code, 0)
        forwarded = self.worker.start_job.call_args.args[0]
        self.assertEqual(forwarded.ccconnect_context, CONTEXT)
        self.assertEqual(forwarded.text, TEXT)
        self.assertEqual(forwarded.received_at, ANCHOR)
        self.assertEqual(Path(output[-1]['record_path']).parent, self.current_runtime)
        self.preflight.check.assert_called_once_with(self.settings_path)

    def test_legacy_no_final_stops_before_any_authorization_or_business_import(self):
        self.old(status='failed')
        output = []
        with patch.object(self.legacy, 'MANIFEST', self.manifest), \
                patch.object(self.legacy, 'emit', side_effect=output.append), \
                patch.object(sys, 'argv', ['legacy','resume','--record',str(self.old_record)]), \
                patch.object(self.legacy.subprocess, 'run', side_effect=AssertionError('no process')):
            code = self.legacy.main()
        self.assertEqual(code, 1)
        self.assertEqual(output[-1]['status'], 'failed')
        self.assertIn('不会自动重算', output[-1]['error'])
        self.worker.run_worker.assert_not_called()
        self.worker.start_job.assert_not_called()

    def test_legacy_existing_final_uses_delivery_worker_without_preflight_or_new_start(self):
        self.old(status='failed')
        self.old_record.with_suffix('.result.json').write_text('{}', encoding='utf-8')
        settings = {**self.settings, 'runtime_state_dir':str(self.old_runtime),
                    'local_analysis':{'backend':'memory','environment':'test'}}
        self.settings_path.write_text(json.dumps(settings), encoding='utf-8')
        installation = {**self.installation, 'runtime_state_dir':str(self.old_runtime)}
        self.manifest.write_text(json.dumps(installation), encoding='utf-8')
        output = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.legacy, 'WORKSPACE', self.root))
            stack.enter_context(patch.object(self.legacy, 'MANIFEST', self.manifest))
            stack.enter_context(patch.object(self.legacy, 'emit', side_effect=output.append))
            stack.enter_context(patch.object(self.legacy, 'inspect_static_settings', return_value={'status':'passed'}))
            stack.enter_context(patch.object(sys, 'argv', ['legacy','resume','--record',str(self.old_record)]))
            stack.enter_context(patch.object(sys, 'path', list(sys.path)))
            stack.enter_context(patch.dict('os.environ', {}, clear=False))
            stack.enter_context(patch.dict(sys.modules, {'walkthrough_worker':self.worker,'preflight':self.preflight}))
            stack.enter_context(patch.object(self.legacy.subprocess, 'run',
                side_effect=AssertionError('no external process in synthetic legacy worker')))
            code = self.legacy.main()
        self.assertEqual(code, 0)
        self.worker.run_worker.assert_called_once_with(self.old_record, self.settings_path)
        self.worker.start_job.assert_not_called()
        self.preflight.check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
