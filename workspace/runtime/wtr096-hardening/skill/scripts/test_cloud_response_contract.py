"""Synthetic successful-envelope failures; no configuration, CLI or HTTP access."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import cloud_log_client as cloud
import cloud_pipeline as windows
import environment_selection as selection
import native_log_source as native
import walkthrough_worker as worker
import window_pipeline as pipeline
from test_task_environment_selection import per_window_task_fixture
from request_model import InputError


SECRET = 'SYNTHETIC-CREDENTIAL-DO-NOT-EXPOSE'
METADATA = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:00:00',
            'end': '2026-10-21T15:01:00'}
ENVIRONMENTS = {
    env: {'origin': 'https://' + env + '.query.invalid',
          'providerEnvironment': 'testtwo' if env == 'test' else env,
          'displayName': env,
          'trustedDownloadHosts': {'https': {env + '.download.invalid'}, 'http': set()}}
    for env in ('test', 'prod')
}


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        raw = json.dumps(payload).encode('utf-8')
        super().__init__(raw)
        self.status = status
        self.headers = {'Content-Length': str(len(raw))}


class CloudResponseContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'local_analysis': {'environment': 'test_then_prod'},
                         'cloud_settings_path': 'unused-synthetic-cloud-settings'}

    def failure(self, environment='test'):
        error = cloud.CloudResponseContractError(reason='data_null', operation='query',
                                                 environment=environment, http_status=200,
                                                 business_code=200)
        error.args = (SECRET + ' https://private.invalid/?userName=' + SECRET,)
        error.__cause__ = RuntimeError('Cookie: ' + SECRET)
        error.payload = {'data': SECRET}
        return error

    def request(self, payload, environment='test'):
        response = Response(payload)
        opener = SimpleNamespace(open=lambda *args, **kwargs: response)
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                patch.object(cloud.urllib.request, 'build_opener', return_value=opener):
            return cloud.request_json({'userName': SECRET}, SECRET, environment=environment)

    def test_successful_missing_null_and_non_object_data_are_finite_failures(self):
        cases = [({}, 'data_missing'), ({'data': None}, 'data_null'),
                 ({'data': []}, 'data_not_object'), ({'data': SECRET}, 'data_not_object'),
                 ({'data': 0}, 'data_not_object'), ({'data': False}, 'data_not_object')]
        for fields, reason in cases:
            with self.subTest(reason=reason, data_type=type(fields.get('data')).__name__):
                with self.assertRaises(cloud.CloudResponseContractError) as caught:
                    self.request({'success': True, 'code': 200, **fields}, 'prod')
                diagnostic = cloud.response_contract_diagnostic(caught.exception)
                self.assertEqual(diagnostic, {'category': 'cloud_response_contract_failed',
                    'retryable': False, 'reason': reason, 'operation': 'query',
                    'environment': 'prod', 'http_status': 200, 'business_code': 200})
                public = worker.safe_failure_fields(caught.exception)
                self.assertIn('不能判断为无日志', public['error'])
                self.assertNotIn('认证失败', public['error'])
                self.assertNotIn(SECRET, json.dumps(public))
                self.assertNotIn('https://', json.dumps(public))

    def test_success_business_codes_are_normalized_without_inventing_http_status(self):
        for code in (0, '0', 200, '200'):
            with self.subTest(code=code), self.assertRaises(cloud.CloudResponseContractError) as caught:
                self.request({'code': code, 'data': None})
            self.assertEqual(cloud.response_contract_diagnostic(caught.exception)['business_code'], int(code))
        diagnostic = cloud.response_contract_diagnostic(cloud.CloudResponseContractError(
            reason='data_missing', operation='query', environment='test', business_code=200))
        self.assertNotIn('http_status', diagnostic)

    def test_genuine_empty_list_remains_a_successful_query(self):
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response(
            {'code': 200, 'data': {'list': [], 'total': 0}}))
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                patch.object(cloud.urllib.request, 'build_opener', return_value=opener):
            result = cloud.query_files(vin=METADATA['vin'], start=METADATA['start'],
                end=METADATA['end'], log_class=cloud.WALKTHROUGH_LOG_CLASS,
                username=SECRET, environment='test')
        self.assertEqual(result.items, [])
        self.assertEqual(result.total, 0)

    def test_business_auth_and_ordinary_service_failure_keep_their_categories(self):
        for code in (401, '403'):
            with self.subTest(code=code), self.assertRaises(cloud.CloudAuthError):
                self.request({'code': code, 'data': None})
        with self.assertRaises(cloud.CloudResponseError) as caught:
            self.request({'success': False, 'code': 500, 'data': None})
        self.assertNotIsInstance(caught.exception, cloud.CloudResponseContractError)

    def test_diagnostics_never_use_mutated_text_payload_chain_or_unknown_fields(self):
        error = self.failure()
        fields = worker.safe_failure_fields(error)
        self.assertEqual(fields['error_diagnostic']['reason'], 'data_null')
        self.assertNotIn(SECRET, json.dumps(fields))
        self.assertNotIn('https://', json.dumps(fields))
        error.reason = SECRET
        error.operation = SECRET
        error.environment = SECRET
        error.http_status = True
        error.business_code = SECRET
        self.assertEqual(cloud.response_contract_diagnostic(error),
                         {'category': 'cloud_response_contract_failed', 'retryable': False})
        self.assertNotIn(SECRET, json.dumps(worker.safe_failure_fields(error)))

    def test_constructor_only_accepts_finite_contract_evidence(self):
        for extra in ({'reason': SECRET}, {'reason': []}, {'operation': 'download'}, {'environment': SECRET},
                      {'http_status': True}, {'http_status': 401}, {'business_code': True},
                      {'business_code': 500}):
            values = {'reason': 'data_null', 'operation': 'query', 'environment': 'test', **extra}
            with self.subTest(fields=list(extra)), self.assertRaises(ValueError) as caught:
                cloud.CloudResponseContractError(**values)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_native_ad_query_propagates_the_contract_failure(self):
        response = Response({'code': 200, 'data': None})
        opener = SimpleNamespace(open=lambda *args, **kwargs: response)
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                patch.object(cloud.urllib.request, 'build_opener', return_value=opener) as build:
            with self.assertRaises(cloud.CloudResponseContractError):
                native.query_class(METADATA, native.LOG_CLASSES[0], SECRET, environment='test')
        self.assertEqual(build.call_count, 1)

    def test_ad_contract_failure_never_reaches_pcm_or_fallback(self):
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': SECRET}), \
                patch.object(selection.ad, 'query_class', side_effect=self.failure()) as query, \
                patch.object(selection.pcm, 'discover') as pcm:
            with self.assertRaises(cloud.CloudResponseContractError):
                selection.select_window(METADATA, self.settings, self.root)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.kwargs['environment'], 'test')
        pcm.assert_not_called()
        self.assertFalse((self.root / 'environment-selection.json').exists())

    def test_pcm_contract_failure_is_not_partial_and_never_falls_back(self):
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': SECRET}), \
                patch.object(selection.ad, 'query_class', return_value=[{}]) as query, \
                patch.object(selection.pcm, 'discover', side_effect=self.failure()) as pcm:
            with self.assertRaises(cloud.CloudResponseContractError):
                selection.select_window(METADATA, self.settings, self.root)
        self.assertTrue(all(call.kwargs['environment'] == 'test' for call in query.call_args_list))
        self.assertEqual(pcm.call_count, 1)
        self.assertFalse((self.root / 'environment-selection.json').exists())

    def test_real_test_empty_can_fallback_but_prod_null_must_fail(self):
        def query(metadata, log_class, username, *, environment):
            if environment == 'prod':
                raise self.failure('prod')
            return []
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': SECRET}), \
                patch.object(selection.ad, 'query_class', side_effect=query) as ad, \
                patch.object(selection.pcm, 'discover', return_value=[]) as pcm:
            with self.assertRaises(cloud.CloudResponseContractError) as caught:
                selection.select_window(METADATA, self.settings, self.root)
        self.assertEqual([c.kwargs['environment'] for c in ad.call_args_list], ['test'] * 4 + ['prod'])
        self.assertEqual([c.kwargs['environment'] for c in pcm.call_args_list], ['test'])
        self.assertEqual(cloud.response_contract_diagnostic(caught.exception)['environment'], 'prod')
        self.assertFalse((self.root / 'environment-selection.json').exists())

    def prepared(self):
        return pipeline.PreparedWindow({}, self.root, {'status': 'ok'}, None, None, {}, None,
                                       {}, 'test', {})

    def test_pcm_prepare_and_parse_preserve_contract_failure(self):
        with patch.object(pipeline.selection, 'select_window', return_value=(
                {'environment': 'test'}, {'ad': {}, 'pcm': [], 'pcm_error': False})), \
                patch.object(pipeline.ad, 'cached_result', return_value=None), \
                patch.object(pipeline.pcm, 'cached_result', return_value=None), \
                patch.object(pipeline.ad, 'fetch_window', return_value={'counts': {}}), \
                patch.object(pipeline.pcm, 'prepare', side_effect=self.failure()), \
                patch.object(pipeline, 'pcm_failure') as partial:
            with self.assertRaises(cloud.CloudResponseContractError):
                pipeline.prepare_window(METADATA, {'local_analysis': {}}, self.root, self.root / 'rules')
        partial.assert_not_called()
        with patch.object(pipeline.pcm, 'parse', side_effect=self.failure()), \
                patch.object(pipeline, 'pcm_failure') as partial:
            with self.assertRaises(cloud.CloudResponseContractError):
                pipeline.parse_window(self.prepared(), {}, self.root / 'rules', lambda *args: None)
        partial.assert_not_called()

    def test_ordinary_pcm_query_500_stops_selection_in_the_same_environment(self):
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': SECRET}), \
                patch.object(selection.ad, 'query_class', return_value=[{}]) as query, \
                patch.object(selection.pcm, 'discover', side_effect=cloud.CloudServerError('synthetic 500')):
            with self.assertRaisesRegex(InputError, 'PCM/CPU 查询失败'):
                selection.select_window(METADATA, self.settings, self.root)
        self.assertTrue(all(call.kwargs['environment'] == 'test' for call in query.call_args_list))
        self.assertFalse((self.root / 'environment-selection.json').exists())

    def test_worker_persists_contract_diagnostic_without_final_or_publication(self):
        path = self.root / 'synthetic-task.json'
        path.write_text(json.dumps({'kind': 'walkthrough_test_local', 'status': 'queued',
            'report_operator_open_id': 'synthetic-user', 'analysis_profile': 'synthetic',
            'vin': METADATA['vin'], 'request_sha256': 'synthetic'}), encoding='utf-8')
        settings = {'runtime_state_dir': str(self.root), 'local_analysis': {}}
        with patch.object(worker, 'load_settings', return_value=settings), \
                patch.object(worker.policy, 'activate_lark_environment'), \
                patch.object(worker.policy, 'ensure_user_authorization'), \
                patch.object(worker, 'analysis_profile', return_value='synthetic'), \
                patch.object(worker, 'check_native_environment', return_value={'status': 'synthetic'}), \
                patch.object(worker, 'analyze_task', side_effect=self.failure()), \
                patch.object(worker, 'publish') as publish:
            with self.assertRaises(cloud.CloudResponseContractError):
                worker.run_worker(path, 'unused')
        publish.assert_not_called()
        persisted = json.loads(path.read_text('utf-8'))
        self.assertEqual(persisted['status'], 'failed')
        self.assertEqual(persisted['error_diagnostic']['category'], 'cloud_response_contract_failed')
        self.assertNotIn(SECRET, path.read_text('utf-8'))
        self.assertFalse(path.with_suffix('.result.json').exists())
        self.assertFalse(path.with_suffix('.worker.lock').exists())

    def test_dual_windows_preserve_contract_diagnostic_above_generic_failure(self):
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        requests = {'baseline': METADATA, 'replacement': {**METADATA, 'start': '2026-10-22T15:00:00',
                                                          'end': '2026-10-22T15:01:00'}}
        contract = self.failure('prod')
        def process(metadata, settings, directory, rules, **kwargs):
            if Path(directory).name == 'baseline':
                raise RuntimeError(SECRET)
            raise contract
        with patch.object(windows, 'validate_config', return_value={'window_workers': 2}), \
                patch.object(windows.selection, 'select_task', return_value=(
                    per_window_task_fixture({role:'prod' for role in requests}, requests=requests), None)), \
                patch.object(windows, 'process_window', side_effect=process), \
                patch.object(windows, 'as_completed', side_effect=lambda futures: iter(futures)):
            with self.assertRaises(cloud.CloudResponseContractError):
                windows.run_windows(requests, {'local_analysis': {'environment': 'test_then_prod'}},
                                    self.root / 'pipeline', rules)
        manifest_path = self.root / 'pipeline/pipeline-manifest.json'
        manifest = json.loads(manifest_path.read_text('utf-8'))
        self.assertEqual(manifest['windows']['replacement']['error_diagnostic'],
                         cloud.response_contract_diagnostic(contract))
        self.assertNotIn(SECRET, manifest_path.read_text('utf-8'))

    def test_main_and_host_use_bounded_contract_mapping(self):
        failure = self.failure()
        stderr = io.StringIO()
        with patch.object(worker.sys, 'argv', ['worker', 'worker', '--record', 'synthetic', '--settings', 'synthetic']), \
                patch.object(worker, 'run_worker', side_effect=failure), contextlib.redirect_stderr(stderr):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(json.loads(stderr.getvalue()), {'status': 'failed', **worker.safe_failure_fields(failure)})
        self.assertNotIn(SECRET, stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        host_path = Path(__file__).absolute().parents[2] / 'deployment/walkthrough_entry.py'
        spec = importlib.util.spec_from_file_location('synthetic_contract_host', host_path)
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        self.assertEqual(host.execution_failure_fields(failure, worker, worker.InputError),
                         worker.safe_failure_fields(failure))

    def test_completed_delivery_clears_old_contract_diagnostic(self):
        path = self.root / 'synthetic-delivery.json'
        record = {'vin': METADATA['vin'], 'source_message_id': 'om_synthetic',
                  'report_url': 'https://docs.invalid/docx/synthetic',
                  'report_permissions': {'collaborator_management': True, 'source_chat_view': True,
                                         'owner_transfer': True},
                  'error': 'synthetic old failure', 'failed_at': 'synthetic',
                  'error_diagnostic': cloud.response_contract_diagnostic(self.failure())}
        with patch.object(worker.policy, 'ensure_report_bot_authorization'), \
                patch.object(worker.policy, 'configure_report_secure_label'), \
                patch.object(worker, 'build_report_notification', return_value={'text': 'synthetic result'}), \
                patch.object(worker.policy, 'run_lark', return_value={'data': {'message_id': 'om_delivered'}}):
            worker.publish({'report_lark_profile': 'synthetic-bot'}, path, record, {})
        self.assertEqual(record['status'], 'completed')
        for name in ('error', 'error_diagnostic', 'failed_at'):
            self.assertNotIn(name, record)


if __name__ == '__main__':
    unittest.main()
