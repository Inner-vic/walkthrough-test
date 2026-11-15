"""Synthetic authentication boundaries and finite errors; no cloud/CLI calls."""
import contextlib
import ast
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import cloud_log_client as cloud
import walkthrough_worker as worker
import window_pipeline as pipeline
import cloud_pipeline as windows
from test_task_environment_selection import per_window_task_fixture


SECRET = 'SYNTHETIC-DO-NOT-EXPOSE'
ENVIRONMENTS = {
    env: {'origin': 'https://' + env + '.query.invalid',
          'providerEnvironment': 'testtwo' if env == 'test' else env,
          'displayName': env,
          'trustedDownloadHosts': {'https': {env + '.download.invalid'}, 'http': set()}}
    for env in ('test', 'prod')
}


class CloudAuthDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def failure(self, *, operation='query', environment='test', http_status=401):
        error = cloud.CloudAuthError(operation=operation, environment=environment, http_status=http_status)
        # The mapping must not trust exception text, args, chain, or arbitrary metadata.
        error.args = (SECRET + ' https://private.invalid/?token=' + SECRET,)
        error.__cause__ = RuntimeError('Cookie: ' + SECRET)
        error.extra = {'Authorization': 'Bearer ' + SECRET}
        return error

    def test_metadata_and_messages_never_use_exception_text_or_unknown_fields(self):
        error = self.failure()
        fields = worker.safe_failure_fields(error)
        self.assertEqual(fields['error_diagnostic'], {
            'category': 'cloud_auth_failed', 'retryable': False, 'operation': 'query',
            'environment': 'test', 'http_status': 401})
        self.assertIn('认证失败', fields['error'])
        self.assertNotIn(SECRET, json.dumps(fields))
        self.assertNotIn('https://', json.dumps(fields))
        error.environment = SECRET
        error.operation = SECRET
        error.http_status = True
        self.assertEqual(cloud.auth_failure_diagnostic(error),
                         {'category': 'cloud_auth_failed', 'retryable': False})
        self.assertNotIn(SECRET, json.dumps(worker.safe_failure_fields(error)))

    def test_business_auth_code_is_not_misrepresented_as_http_status(self):
        error = cloud.CloudAuthError(operation='query', environment='prod', business_code=403)
        fields = worker.safe_failure_fields(error)
        self.assertEqual(fields['error_diagnostic']['business_code'], 403)
        self.assertNotIn('http_status', fields['error_diagnostic'])
        self.assertIn('业务码 403', fields['error'])

    def test_only_finite_auth_metadata_can_be_constructed(self):
        for kwargs in ({'operation': SECRET}, {'environment': SECRET}, {'http_status': True},
                       {'http_status': 500}, {'business_code': SECRET}):
            values = {'operation': 'query', 'environment': 'test', 'http_status': 401, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError) as caught:
                cloud.CloudAuthError(**values)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_download_auth_is_not_retried_and_leaves_no_file_or_part(self):
        for code in (401, 403):
            with self.subTest(code=code):
                target = self.root / str(code) / 'source.bin'
                def denied(request, **kwargs):
                    self.assertIsNone(request.get_header('Authorization'))
                    self.assertIsNone(request.get_header('Cookie'))
                    raise urllib.error.HTTPError(request.full_url, code, SECRET, {}, None)
                opener = SimpleNamespace(open=denied)
                with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                        patch.object(cloud.urllib.request, 'build_opener', return_value=opener) as build, \
                        patch.object(cloud.time, 'sleep') as sleep:
                    with self.assertRaises(cloud.CloudAuthError) as caught:
                        cloud.download_to_file('https://test.download.invalid/file?token=' + SECRET,
                                               target, environment='test', attempts=3)
                self.assertEqual(build.call_count, 1)
                sleep.assert_not_called()
                self.assertFalse(target.exists())
                self.assertFalse(target.with_name(target.name + '.part').exists())
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(cloud.auth_failure_diagnostic(caught.exception)['operation'], 'download')

    def test_worker_persists_finite_auth_failure_without_publication_or_result(self):
        path = self.root / 'synthetic-task.json'
        record = {'kind': 'walkthrough_test_local', 'status': 'queued',
                  'report_operator_open_id': 'synthetic-user', 'analysis_profile': 'synthetic',
                  'vin': 'SYNTHETIC', 'request_sha256': 'synthetic'}
        path.write_text(json.dumps(record), encoding='utf-8')
        settings = {'runtime_state_dir': str(self.root), 'local_analysis': {}}
        failure = self.failure()
        with patch.object(worker, 'load_settings', return_value=settings), \
                patch.object(worker.policy, 'activate_lark_environment'), \
                patch.object(worker.policy, 'ensure_user_authorization'), \
                patch.object(worker, 'analysis_profile', return_value='synthetic'), \
                patch.object(worker, 'check_native_environment', return_value={'status': 'synthetic'}), \
                patch.object(worker, 'analyze_task', side_effect=failure), \
                patch.object(worker, 'publish') as publish:
            with self.assertRaises(cloud.CloudAuthError):
                worker.run_worker(path, 'unused')
        publish.assert_not_called()
        persisted = json.loads(path.read_text('utf-8'))
        self.assertEqual(persisted['status'], 'failed')
        self.assertEqual(persisted['error'], worker.safe_failure_fields(failure)['error'])
        self.assertEqual(persisted['error_diagnostic'], worker.safe_failure_fields(failure)['error_diagnostic'])
        self.assertNotIn(SECRET, path.read_text('utf-8'))
        self.assertFalse(path.with_suffix('.result.json').exists())
        self.assertFalse(path.with_suffix('.worker.lock').exists())

    def test_main_uses_the_same_safe_mapping_as_persisted_record(self):
        failure = self.failure()
        stderr = io.StringIO()
        with patch.object(worker.sys, 'argv', ['worker', 'worker', '--record', 'synthetic', '--settings', 'synthetic']), \
                patch.object(worker, 'run_worker', side_effect=failure), contextlib.redirect_stderr(stderr):
            self.assertEqual(worker.main(), 1)
        response = json.loads(stderr.getvalue())
        self.assertEqual(response, {'status': 'failed', **worker.safe_failure_fields(failure)})
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())

    def test_pcm_download_auth_is_not_downgraded_to_partial(self):
        metadata = {'vin': 'SYNTHETIC', 'start': '2026-10-21T15:00:00', 'end': '2026-10-21T15:01:00'}
        decision = {'environment': 'test'}
        discovery = {'ad': {}, 'pcm': [], 'pcm_error': False}
        with patch.object(pipeline.selection, 'select_window', return_value=(decision, discovery)), \
                patch.object(pipeline.ad, 'cached_result', return_value=None), \
                patch.object(pipeline.pcm, 'cached_result', return_value=None), \
                patch.object(pipeline.ad, 'fetch_window', return_value={'counts': {}}), \
                patch.object(pipeline.pcm, 'prepare', side_effect=self.failure(operation='download')), \
                patch.object(pipeline, 'pcm_failure') as partial:
            with self.assertRaises(cloud.CloudAuthError):
                pipeline.prepare_window(metadata, {'local_analysis': {}}, self.root / 'window', self.root / 'rules')
        partial.assert_not_called()

    def test_pcm_parse_auth_is_not_downgraded_to_partial(self):
        prepared = pipeline.PreparedWindow({}, self.root, {'status': 'ok'}, None, None, {}, None,
                                           {}, 'test', {})
        with patch.object(pipeline.pcm, 'parse', side_effect=self.failure()), \
                patch.object(pipeline, 'pcm_failure') as partial:
            with self.assertRaises(cloud.CloudAuthError):
                pipeline.parse_window(prepared, {}, self.root / 'rules', lambda *args: None)
        partial.assert_not_called()

    def mixed_window_failure(self, late_error):
        metadata = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:00:00',
                    'end': '2026-10-21T15:01:00'}
        metadata_by_role = {'baseline': metadata,
                            'replacement': {**metadata, 'start': '2026-10-22T15:00:00',
                                            'end': '2026-10-22T15:01:00'}}
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        both_running = threading.Barrier(2, timeout=3)
        ordinary_consumed = threading.Event()
        early_error = RuntimeError(SECRET + ' normal failure')
        original_as_completed = windows.as_completed
        events = []

        def process(metadata, settings, directory, rules, **kwargs):
            role = Path(directory).name
            both_running.wait()
            if role == 'baseline':
                raise early_error
            self.assertTrue(ordinary_consumed.wait(3), 'earlier ordinary failure was not collected')
            raise late_error

        def completed_in_order(futures):
            for future in original_as_completed(futures):
                yield future
                if futures[future] == 'baseline':
                    ordinary_consumed.set()

        with patch.object(windows, 'validate_config', return_value={'window_workers': 2}), \
                patch.object(windows.selection, 'select_task', return_value=(
                    per_window_task_fixture(policy='test', requests=metadata_by_role), None)), \
                patch.object(windows, 'process_window', side_effect=process), \
                patch.object(windows, 'as_completed', side_effect=completed_in_order):
            try:
                windows.run_windows(metadata_by_role, {'local_analysis': {'environment': 'test'}},
                                    self.root / 'pipeline', rules, on_stage=events.append)
            except Exception as error:
                caught = error
            else:
                self.fail('two failed windows returned a successful result')
        self.assertTrue(ordinary_consumed.is_set())
        manifest_path = self.root / 'pipeline/pipeline-manifest.json'
        self.assertNotIn(SECRET, manifest_path.read_text('utf-8'))
        return caught, early_error, json.loads(manifest_path.read_text('utf-8')), events

    def test_later_auth_failure_is_visible_above_earlier_generic_window_failure(self):
        failure = self.failure()
        caught, early, manifest, events = self.mixed_window_failure(failure)
        self.assertIs(caught, failure)
        self.assertEqual(manifest['stage'], 'failed')
        for role in ('baseline', 'replacement'):
            self.assertEqual(manifest['windows'][role]['status'], 'failed')
        self.assertNotIn('error_diagnostic', manifest['windows']['baseline'])
        diagnostic = manifest['windows']['replacement']['error_diagnostic']
        self.assertEqual(diagnostic, cloud.auth_failure_diagnostic(failure))
        window_events = [event for event in events if event['scope'] == 'window']
        self.assertEqual([event['window'] for event in window_events], ['baseline', 'replacement'])
        self.assertEqual(window_events[-1]['error_diagnostic'], diagnostic)
        self.assertNotIn(SECRET, json.dumps(worker.safe_failure_fields(caught)))

    def test_two_non_auth_window_failures_keep_original_first_failure_order(self):
        caught, early, manifest, events = self.mixed_window_failure(ValueError(SECRET + ' later failure'))
        self.assertIs(caught, early)
        self.assertTrue(all('error_diagnostic' not in state for state in manifest['windows'].values()))

    def candidate_host(self):
        path = Path(__file__).absolute().parents[2] / 'deployment/walkthrough_entry.py'
        spec = importlib.util.spec_from_file_location('synthetic_candidate_walkthrough_entry', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, path

    def test_host_helper_reuses_safe_auth_mapping_without_stringifying_error(self):
        host, _ = self.candidate_host()
        error = self.failure()
        result = host.execution_failure_fields(error, worker, worker.InputError)
        self.assertEqual(result, worker.safe_failure_fields(error))
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('https://', json.dumps(result))

    def test_host_helper_preserves_existing_input_and_generic_failure_behavior(self):
        host, _ = self.candidate_host()
        self.assertEqual(host.execution_failure_fields(worker.InputError('synthetic validation'),
                                                       worker, worker.InputError),
                         {'error': 'synthetic validation'})
        ordinary = host.execution_failure_fields(RuntimeError(SECRET), worker, worker.InputError)
        self.assertEqual(ordinary, {'error': '走查测试检查或执行失败，请查看独立配置和任务诊断。'})
        self.assertNotIn(SECRET, json.dumps(ordinary))

    def test_host_main_exception_handler_is_wired_to_the_bounded_helper_without_running_main(self):
        _, path = self.candidate_host()
        tree = ast.parse(path.read_text('utf-8-sig'))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        calls = [node for handler in ast.walk(main) if isinstance(handler, ast.ExceptHandler)
                 for node in ast.walk(handler) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'execution_failure_fields']
        self.assertEqual(len(calls), 1)
        self.assertEqual([argument.id for argument in calls[0].args], ['exc', 'worker', 'InputError'])


if __name__ == '__main__':
    unittest.main()
