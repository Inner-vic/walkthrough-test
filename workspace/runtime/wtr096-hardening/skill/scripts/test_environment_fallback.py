"""Synthetic checks for whole-window environment selection; no cloud access."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import zipfile

import environment_selection as selection
import direct_ad
import window_pipeline
from test_full_integration import native_boundary
from request_model import InputError


META = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:48:00',
        'end': '2026-10-21T16:22:00'}
CLASSES = ('log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth', 'log_fsdA_service')


class EnvironmentFallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'cloud_settings_path': 'synthetic-identity.json',
                         'local_analysis': {'environment': 'test_then_prod',
                                            'backend': 'native_full','execution':'wsl','distribution':'Synthetic',
                                            'python':'/venv/python','analyzer_root':'/latest',
                                            'rules_json': 'synthetic-rules.json'}}
        self.calls = []
        self.lock = threading.Lock()

    @staticmethod
    def item(label):
        # Signed download URLs must remain transient, never in the decision.
        return {'vin': META['vin'], 'fileName': label,
                'downloadURL': 'https://synthetic.invalid/log?secret=do-not-persist'}

    @contextmanager
    def discovery(self, rows=None, errors=None, barrier=None):
        rows = rows or {}
        errors = errors or {}

        def get(kind, metadata, environment):
            with self.lock:
                self.calls.append((environment, kind, dict(metadata)))
            if barrier is not None and environment == 'test' and kind == 'log_fsdA':
                barrier.wait(timeout=5)
            key = (environment, kind)
            if key in errors:
                raise errors[key]
            value = rows.get(key, [])
            return value(metadata) if callable(value) else list(value)

        def query(metadata, log_class, username, *, environment='test'):
            self.assertEqual(username, 'synthetic-operator')
            return get(log_class, metadata, environment)

        def pcm(metadata, settings, *, environment='test'):
            return get('log_fsdA_service', metadata, environment)

        with patch.object(selection.ad.cloud, 'load_settings',
                          return_value={'username': 'synthetic-operator'}), \
                patch.object(selection.ad, 'query_class', side_effect=query), \
                patch.object(selection.pcm, 'discover', side_effect=pcm):
            yield

    def select(self, name='window', metadata=None, settings=None):
        return selection.select_window(metadata or META, settings or self.settings,
                                       self.root / name)

    def selection_path(self, name='window'):
        return self.root / name / 'environment-selection.json'

    def assert_only_environment(self, environment):
        self.assertTrue(self.calls)
        self.assertEqual({row[0] for row in self.calls}, {environment})

    @contextmanager
    def http_client(self, responder):
        """Run real request/pagination code against an in-memory HTTP boundary."""
        cloud = selection.ad.cloud
        environments = {env: {'origin': 'https://' + env + '.query.invalid',
                               'providerEnvironment': 'testtwo' if env == 'test' else env,
                               'displayName': env,
                               'trustedDownloadHosts': {'https': {env + '.download.invalid'},
                                                        'http': set()}}
                        for env in ('test', 'prod')}

        class Response(io.BytesIO):
            def __init__(self, content):
                super().__init__(content)
                self.headers = {'Content-Length': str(len(content))}

        def open_request(request, **kwargs):
            parsed = urllib.parse.urlsplit(request.full_url)
            env = parsed.hostname.split('.')[0]
            params = urllib.parse.parse_qs(parsed.query)
            kind = params['logClass'][0]
            with self.lock:
                self.calls.append((env, kind, params))
            answer = responder(env, kind, int(params['pageNo'][0]))
            if isinstance(answer, Exception):
                raise answer
            return Response(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

        with patch.dict(cloud.CLOUD_ENVIRONMENTS, environments, clear=True), \
                patch.object(cloud, 'load_settings', return_value={'username': 'synthetic-operator'}), \
                patch.object(cloud.urllib.request, 'build_opener',
                             return_value=SimpleNamespace(open=open_request)):
            yield

    def test_test_data_selects_test_without_querying_prod(self):
        rows = {('test', kind): [self.item(kind)] for kind in CLASSES}
        with self.discovery(rows):
            decision, discovered = self.select()
        self.assertEqual(decision['environment'], 'test')
        self.assertEqual(decision['provider_environment'], 'testtwo')
        self.assertEqual(decision['request'], META)
        self.assertEqual(len(decision['attempts']), 1)
        self.assertEqual(decision['attempts'][0]['counts'], dict.fromkeys(CLASSES, 1))
        self.assertEqual(discovered['ad']['log_fsdA'], rows[('test', 'log_fsdA')])
        self.assertEqual(discovered['pcm'], rows[('test', 'log_fsdA_service')])
        self.assertFalse(discovered['pcm_error'])
        self.assert_only_environment('test')
        self.assertEqual({row[1] for row in self.calls}, set(CLASSES))

    def test_default_policy_is_prod_then_test(self):
        settings = deepcopy(self.settings)
        del settings['local_analysis']['environment']
        with self.discovery({('prod', 'log_fsdA'): [self.item('prod.zip')]}):
            decision, _ = self.select(settings=settings)
        self.assertEqual(decision['policy'], 'prod_then_test')
        self.assertEqual(decision['environment'], 'prod')
        self.assert_only_environment('prod')

    def test_all_five_successfully_empty_falls_back_as_a_whole_window(self):
        rows = {('prod', kind): [self.item('prod-' + kind)] for kind in CLASSES}
        with self.discovery(rows):
            decision, discovered = self.select()
        self.assertEqual(decision['environment'], 'prod')
        self.assertEqual(decision['provider_environment'], 'prod')
        self.assertEqual([item['environment'] for item in decision['attempts']],
                         ['testtwo', 'prod'])
        self.assertEqual(decision['attempts'][0]['counts'], dict.fromkeys(CLASSES, 0))
        self.assertEqual(decision['attempts'][0]['pcm_status'], 'ok')
        self.assertEqual(discovered['ad']['log_fsdB'], rows[('prod', 'log_fsdB')])
        self.assertEqual(discovered['pcm'], rows[('prod', 'log_fsdA_service')])
        first_prod = next(i for i, row in enumerate(self.calls) if row[0] == 'prod')
        self.assertEqual({row[1] for row in self.calls[:first_prod]}, set(CLASSES))

    def test_both_environments_empty_has_actionable_error_and_no_selection(self):
        with self.discovery():
            with self.assertRaises(InputError) as error:
                self.select()
        text = str(error.exception)
        for fragment in (META['vin'], '2026-10-21', '15:48', '16:22', 'testtwo', 'prod'):
            self.assertIn(fragment, text)
        self.assertFalse(self.selection_path().exists())
        self.assertEqual({(env, kind) for env, kind, _ in self.calls},
                         {(env, kind) for env in ('test', 'prod') for kind in CLASSES})

    def test_any_single_source_prevents_fallback_and_preserves_missing_sources(self):
        for kind in CLASSES:
            with self.subTest(kind=kind):
                self.calls.clear()
                with self.discovery({('test', kind): [self.item(kind)]}):
                    decision, discovered = self.select(name=kind)
                self.assert_only_environment('test')
                self.assertEqual(decision['environment'], 'test')
                counts = decision['attempts'][0]['counts']
                self.assertEqual(counts, {key: int(key == kind) for key in CLASSES})
                self.assertEqual(len(discovered['ad']['log_fsdA']), int(kind == 'log_fsdA'))
                self.assertEqual(len(discovered['pcm']), int(kind == 'log_fsdA_service'))

    def test_fixed_policies_never_try_another_environment(self):
        for environment in ('test', 'prod'):
            with self.subTest(environment=environment):
                self.calls.clear()
                settings = deepcopy(self.settings)
                settings['local_analysis']['environment'] = environment
                with self.discovery({(environment, 'log_fsdA'): [self.item('one.zip')]}):
                    decision, _ = self.select(name=environment, settings=settings)
                self.assert_only_environment(environment)
                self.assertEqual(decision['environment'], environment)
                self.assertEqual(decision['policy'], environment)
                self.calls.clear()
                with self.discovery():
                    with self.assertRaises(InputError):
                        self.select(name=environment + '-empty', settings=settings)
                self.assert_only_environment(environment)

    def test_ad_errors_never_trigger_fallback(self):
        for kind in ('log_fsdA', 'log_fsdB'):
            for index, error in enumerate((TimeoutError('timeout'), ConnectionError('network'),
                                           InputError('unauthorized or malformed response'))):
                with self.subTest(kind=kind, error=type(error).__name__):
                    self.calls.clear()
                    with self.discovery(errors={('test', kind): error}):
                        with self.assertRaises((InputError, OSError, RuntimeError)):
                            self.select(name=kind + str(index))
                    self.assert_only_environment('test')
                    self.assertFalse(self.selection_path(kind + str(index)).exists())

    def test_pcm_error_with_ad_data_stops_without_fallback_or_selection(self):
        with self.discovery({('test', 'log_fsdA'): [self.item('ad.zip')]},
                            {('test', 'log_fsdA_service'): TimeoutError('secret-url')}):
            with self.assertRaisesRegex(InputError, 'PCM/CPU 查询失败') as error:
                self.select()
        self.assert_only_environment('test')
        self.assertFalse(self.selection_path().exists())
        self.assertNotIn('secret-url', str(error.exception))

    def test_pcm_error_without_ad_cannot_be_treated_as_empty(self):
        for index, error in enumerate((TimeoutError('timeout'), ConnectionError('network'),
                                       InputError('invalid PCM response'))):
            with self.subTest(error=type(error).__name__):
                self.calls.clear()
                with self.discovery(errors={('test', 'log_fsdA_service'): error}):
                    with self.assertRaises(InputError):
                        self.select(name=str(index))
                self.assert_only_environment('test')
                self.assertFalse(self.selection_path(str(index)).exists())

    def test_fallback_prod_error_is_not_a_successful_empty_result(self):
        with self.discovery(errors={('prod', 'log_fsdA'): TimeoutError('prod timeout')}):
            with self.assertRaises((InputError, OSError, RuntimeError)):
                self.select()
        self.assertFalse(self.selection_path().exists())
        self.assertEqual([env for env, kind, _ in self.calls if kind == 'log_fsdA'],
                         ['test', 'prod'])

    def test_query_windows_extend_ad_only_and_selection_stores_no_urls(self):
        with self.discovery({('test', 'log_fsdA'): [self.item('ad.zip')]}):
            decision, _ = self.select()
        for _, kind, metadata in self.calls:
            self.assertEqual(metadata['vin'], META['vin'])
            self.assertEqual(metadata['start'], META['start'])
            self.assertEqual(metadata['end'], META['end'] if kind == 'log_fsdA_service'
                             else '2026-10-21T17:22:00')
        text = self.selection_path().read_text(encoding='utf-8')
        self.assertEqual(json.loads(text), decision)
        for secret in ('downloadURL', 'https://', 'do-not-persist', 'synthetic-operator'):
            self.assertNotIn(secret, text)

    def test_cached_selection_locks_environment_without_requerying(self):
        with self.discovery({('prod', 'log_fsdA'): [self.item('prod.zip')]}):
            first, _ = self.select()
        self.calls.clear()
        with self.discovery({('test', 'log_fsdA'): [self.item('new-test.zip')]}):
            second, discovered = self.select()
        self.assertEqual(second, first)
        self.assertEqual(second['environment'], 'prod')
        self.assertIsNone(discovered)
        self.assertEqual(self.calls, [])

    def test_cached_selection_rejects_policy_request_version_and_environment_tampering(self):
        mutations = (
            lambda value: value.update(policy='prod'),
            lambda value: value.update(environment='prod'),
            lambda value: value.update(provider_environment='prod'),
            lambda value: value.update(version='unrecognized'),
            lambda value: value['request'].update(start='2026-10-21T14:50:00'),
            lambda value: value.update(environment='prod', provider_environment='prod'),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                name = 'tamper-' + str(index)
                with self.discovery({('test', 'log_fsdA'): [self.item('test.zip')]}):
                    decision, _ = self.select(name=name)
                mutate(decision)
                self.selection_path(name).write_text(json.dumps(decision), encoding='utf-8')
                self.calls.clear()
                with self.discovery():
                    with self.assertRaises(InputError):
                        self.select(name=name)
                self.assertEqual(self.calls, [])

    def test_changing_configured_policy_cannot_reuse_cached_selection(self):
        with self.discovery({('test', 'log_fsdA'): [self.item('test.zip')]}):
            self.select()
        changed = deepcopy(self.settings)
        changed['local_analysis']['environment'] = 'test'
        self.calls.clear()
        with self.discovery():
            with self.assertRaises(InputError):
                self.select(settings=changed)
        self.assertEqual(self.calls, [])

    def test_missing_selection_with_existing_inputs_or_results_is_rejected(self):
        for index, relative in enumerate(('input/download-manifest.json',
                                          'service-input/download-manifest.json',
                                          'native-result.json', 'pcm-result.json',
                                          'window-result.json')):
            with self.subTest(relative=relative):
                name = 'old-' + str(index)
                path = self.root / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}', encoding='utf-8')
                self.calls.clear()
                with self.discovery({('test', 'log_fsdA'): [self.item('test.zip')]}):
                    with self.assertRaises(InputError):
                        self.select(name=name)
                self.assertEqual(self.calls, [])

    def test_parallel_windows_choose_independent_environments_and_keep_roles(self):
        replacement = {**META, 'start': '2026-10-21T14:50:00', 'end': '2026-10-21T15:24:00'}
        rows = {('test', 'log_fsdA'): lambda meta: [] if meta['start'] == META['start']
                else [self.item('replacement-test.zip')],
                ('prod', 'log_fsdA'): [self.item('baseline-prod.zip')]}
        with self.discovery(rows, barrier=threading.Barrier(2)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                jobs = {'baseline': executor.submit(self.select, 'baseline', META),
                        'replacement': executor.submit(self.select, 'replacement', replacement)}
                results = {role: job.result(timeout=8) for role, job in jobs.items()}
        self.assertEqual(results['baseline'][0]['environment'], 'prod')
        self.assertEqual(results['replacement'][0]['environment'], 'test')
        self.assertEqual(results['baseline'][0]['request'], META)
        self.assertEqual(results['replacement'][0]['request'], replacement)
        self.assertEqual(results['baseline'][1]['ad']['log_fsdA'][0]['fileName'], 'baseline-prod.zip')
        self.assertEqual(results['replacement'][1]['ad']['log_fsdA'][0]['fileName'], 'replacement-test.zip')
        for role in results:
            self.assertEqual(json.loads(self.selection_path(role).read_text(encoding='utf-8')),
                             results[role][0])

    def test_real_clients_reject_error_or_incomplete_responses_without_prod_fallback(self):
        empty = {'code': 0, 'data': {'list': [], 'total': 0}}
        invalid = [
            ('http401', urllib.error.HTTPError('https://test.query.invalid/', 401, 'denied', {}, None)),
            ('http500', urllib.error.HTTPError('https://test.query.invalid/', 500, 'server', {}, None)),
            ('network', urllib.error.URLError('offline')),
            ('json', b'{broken'),
            ('nonobject', []),
            ('successfalse', {'success': False, 'data': {'list': [], 'total': 0}}),
            ('businesscode', {'code': 403, 'data': {'list': [], 'total': 0}}),
            ('missingdata', {'code': 0}),
            ('missingtotal', {'code': 0, 'data': {'list': []}}),
            ('missinglist', {'code': 0, 'data': {'total': 0}}),
            ('nulllist', {'code': 0, 'data': {'list': None, 'total': 0}}),
            ('booleantotal', {'code': 0, 'data': {'list': [], 'total': False}}),
            ('fractionaltotal', {'code': 0, 'data': {'list': [], 'total': 0.5}}),
            ('incompletepage', {'code': 0, 'data': {'list': [], 'total': 1}}),
        ]
        for kind in ('log_fsdA', 'log_fsdA_service'):
            for name, payload in invalid:
                with self.subTest(kind=kind, payload=name):
                    self.calls.clear()
                    def respond(env, actual, page):
                        return payload if actual == kind else empty
                    with self.http_client(respond):
                        with self.assertRaises((InputError, selection.ad.cloud.CloudQueryError)):
                            self.select(name=kind + '-' + name)
                    self.assert_only_environment('test')
                    self.assertFalse(self.selection_path(kind + '-' + name).exists())

    def test_real_clients_validated_empty_records_allow_fallback(self):
        def respond(env, kind, page):
            items = [self.item('prod.zip')] if env == 'prod' and kind == 'log_fsdA' else []
            return {'code': 0, 'data': {'records': items, 'total': str(len(items))}}
        with self.http_client(respond):
            decision, discovery = self.select()
        self.assertEqual(decision['environment'], 'prod')
        self.assertEqual(discovery['ad']['log_fsdA'][0]['fileName'], 'prod.zip')
        self.assertEqual(len(self.calls), 2 * len(CLASSES))
        self.assertEqual([row['environment'] for row in decision['attempts']], ['testtwo', 'prod'])

    def test_real_pcm_non_auth_business_error_with_ad_data_stops_selection(self):
        def respond(env, kind, page):
            if kind == 'log_fsdA_service':
                return {'code': 500, 'data': {'list': [], 'total': 0}}
            items = [self.item('test.zip')] if kind == 'log_fsdA' else []
            return {'code': 0, 'data': {'list': items, 'total': len(items)}}
        with self.http_client(respond):
            with self.assertRaisesRegex(InputError, 'PCM/CPU 查询失败'):
                self.select()
        self.assert_only_environment('test')
        self.assertFalse(self.selection_path().exists())

    def test_ad_http_auth_failure_on_first_or_later_page_never_falls_back(self):
        cloud = selection.ad.cloud
        for code in (401, 403):
            for failed_page in (1, 2):
                with self.subTest(code=code, failed_page=failed_page):
                    self.calls.clear()
                    def respond(env, kind, page):
                        if kind == 'log_fsdA' and page == failed_page:
                            return urllib.error.HTTPError('https://test.query.invalid/?token=synthetic-secret',
                                                          code, 'synthetic-secret', {}, None)
                        if kind == 'log_fsdA':
                            return {'code': 0, 'data': {'list': [self.item('one.zip')], 'total': 2}}
                        return {'code': 0, 'data': {'list': [], 'total': 0}}
                    name = f'auth-ad-{code}-{failed_page}'
                    with self.http_client(respond), patch.object(cloud, 'download_to_file') as download:
                        with self.assertRaises(cloud.CloudAuthError) as caught:
                            self.select(name=name)
                    download.assert_not_called()
                    self.assert_only_environment('test')
                    self.assertFalse(self.selection_path(name).exists())
                    self.assertEqual(cloud.auth_failure_diagnostic(caught.exception), {
                        'category': 'cloud_auth_failed', 'retryable': False, 'operation': 'query',
                        'environment': 'test', 'http_status': code})
                    self.assertNotIn('synthetic-secret', str(caught.exception))
                    self.assertEqual(len(self.calls), failed_page)

    def test_pcm_auth_with_ad_data_is_fatal_for_http_and_business_codes(self):
        cloud = selection.ad.cloud
        cases = [('http', code) for code in (401, 403)] + [
            ('business', code) for code in (401, 403, '401', '403')]
        for index, (kind, code) in enumerate(cases):
            with self.subTest(kind=kind, code=code):
                self.calls.clear()
                def respond(env, log_class, page):
                    if log_class == 'log_fsdA_service':
                        if kind == 'http':
                            return urllib.error.HTTPError('https://test.query.invalid/?token=synthetic-secret',
                                                          code, 'synthetic-secret', {}, None)
                        return {'code': code, 'success': False,
                                'message': 'synthetic-secret https://private.invalid/?token=synthetic-secret',
                                'data': {'list': [], 'total': 0}}
                    items = [self.item('ad.zip')] if log_class == 'log_fsdA' else []
                    return {'code': 0, 'data': {'list': items, 'total': len(items)}}
                name = 'auth-pcm-' + str(index)
                with self.http_client(respond), patch.object(cloud, 'download_to_file') as download:
                    with self.assertRaises(cloud.CloudAuthError) as caught:
                        self.select(name=name)
                download.assert_not_called()
                self.assert_only_environment('test')
                self.assertFalse(self.selection_path(name).exists())
                diagnostic = cloud.auth_failure_diagnostic(caught.exception)
                self.assertEqual(diagnostic['operation'], 'query')
                self.assertEqual(diagnostic['http_status' if kind == 'http' else 'business_code'], int(code))
                self.assertNotIn('http_status' if kind != 'http' else 'business_code', diagnostic)
                self.assertNotIn('synthetic-secret', str(caught.exception))

    def test_prod_auth_after_successful_empty_test_is_not_reported_as_no_data(self):
        cloud = selection.ad.cloud
        def respond(env, kind, page):
            if env == 'prod':
                return urllib.error.HTTPError('https://prod.query.invalid/', 401, 'denied', {}, None)
            return {'code': 0, 'data': {'list': [], 'total': 0}}
        with self.http_client(respond), self.assertRaises(cloud.CloudAuthError) as caught:
            self.select(name='empty-test-auth-prod')
        self.assertEqual(cloud.auth_failure_diagnostic(caught.exception)['environment'], 'prod')
        self.assertEqual([env for env, _, _ in self.calls], ['test'] * 5 + ['prod'])
        self.assertFalse(self.selection_path('empty-test-auth-prod').exists())

    def test_real_pcm_non_avm_wrong_or_missing_vin_cannot_trigger_fallback(self):
        for name, record in (
                ('wrong', {**self.item('unrelated-service.log'), 'vin': 'OTHER000000000001'}),
                ('missing', {'fileName': 'AVM_Service-netbw-other.zst'})):
            with self.subTest(identity=name):
                self.calls.clear()
                def respond(env, kind, page):
                    self.assertEqual(env, 'test', 'invalid source identity must never permit fallback')
                    items = [record] if kind == 'log_fsdA_service' else []
                    return {'code': 0, 'data': {'list': items, 'total': len(items)}}
                with self.http_client(respond):
                    with self.assertRaises(InputError):
                        self.select(name='non-avm-' + name)
                self.assert_only_environment('test')
                self.assertFalse(self.selection_path('non-avm-' + name).exists())

    def test_download_allowlists_remain_separate_for_test_and_prod(self):
        with self.http_client(lambda *args: {'data': {'list': [], 'total': 0}}):
            for env in ('test', 'prod'):
                other = 'prod' if env == 'test' else 'test'
                self.assertEqual(selection.ad.cloud.validate_download_url(
                    'https://' + env + '.download.invalid/file.zip', environment=env),
                    ('https', env + '.download.invalid'))
                with self.assertRaises(selection.ad.cloud.CloudResponseError):
                    selection.ad.cloud.validate_download_url(
                        'https://' + other + '.download.invalid/file.zip', environment=env)
        self.assertEqual(self.calls, [])

    def test_complete_prod_window_preserves_environment_through_download_parse_and_cache(self):
        import zstandard
        rules = {'fixture': {name: {'func': 'custom', 'calculate_type': '', 'ps_type': 'ps_time',
                                  'soc_type': 'synthetic_container', 'log_tag': 'SYNTHETIC',
                                  'input': 'var = "" $10 "," find_digit($7);max_threshold=1000'}
                             for name in ('header_delay', 'vla_parking_header_delay',
                                          'VlaModel_FPS', 'VlaParkingDLM_FPS')}}
        rule_path = self.root / 'rules.json'
        rule_path.write_text(json.dumps(rules), encoding='utf-8')
        settings = deepcopy(self.settings)
        settings['local_analysis'].update(rules_json=str(rule_path), keep_raw_logs=True)
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, 'w') as archive:
            archive.writestr('FSDA_M100-' + META['vin'] + '-2026_10_21_16_22_00.log', 'identity')
            archive.writestr('synthetic_container_2026_10_21_16_22_00.log',
                             '10-21 15:50:00.000 42 x x x state:0 x x 5.0 SYNTHETIC\n' * 20)
        body = {'log_fsdA': zipped.getvalue(),
                'log_fsdA_service': zstandard.ZstdCompressor().compress(b'synthetic nonmetric log\n')}
        rows = {'log_fsdA': [{**self.item('synthetic_2026_10_21_16_22_00.zip'),
                             'downloadURL': 'https://prod.download.invalid/log_fsdA',
                             'fileSize': len(body['log_fsdA'])}],
                'log_fsdB': [], 'log_xcu_mcu': [], 'log_bzcu_eth': [],
                'log_fsdA_service': [{**self.item('AVM_Service-synthetic.zst'),
                                     'downloadURL': 'https://prod.download.invalid/log_fsdA_service',
                                     'fileSize': len(body['log_fsdA_service'])}]}
        downloads = []
        def respond(env, kind, page):
            items = rows[kind] if env == 'prod' else []
            return {'code': 0, 'data': {'list': items, 'total': len(items)}}
        def download(url, destination, *, environment, max_bytes, byte_budget=None):
            self.assertEqual(environment, 'prod')
            selection.ad.cloud.validate_download_url(url, environment=environment)
            kind = urllib.parse.urlsplit(url).path.lstrip('/')
            self.assertLessEqual(len(body[kind]), max_bytes)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if byte_budget is not None:
                byte_budget.consume(len(body[kind]))
            destination.write_bytes(body[kind])
            downloads.append((environment, kind))
            return len(body[kind])
        directory = self.root / 'prod-window'
        with self.http_client(respond), patch.object(selection.ad.cloud, 'download_to_file', side_effect=download), \
                patch.object(selection.ad.subprocess,'run',side_effect=native_boundary) as execute:
            result = window_pipeline.process_window(META, settings, directory, rule_path)
        self.assertEqual(execute.call_count,1)
        self.assertTrue(Path(result['ad']['full_statistics']['path']).is_file())
        self.assertEqual(result['ad']['full_statistics']['sha256'],selection.ad.digest(result['ad']['full_statistics']['path']))
        self.assertEqual(result['environment'], 'prod')
        self.assertEqual(result['environment_selection']['environment'], 'prod')
        self.assertEqual(result['ad']['environment'], 'prod')
        self.assertEqual(result['ad']['source']['environment'], 'prod')
        self.assertEqual(result['pcm']['source']['environment'], 'prod')
        self.assertEqual(result['ad']['metrics']['slow_driver']['p99_ms'], 5.0)
        self.assertEqual({env for env, _ in downloads}, {'prod'})
        self.assertEqual({kind for _, kind in downloads}, {'log_fsdA', 'log_fsdA_service'})
        for name in ('ad', 'pcm'):
            self.assertEqual(result['sources'][name]['environment'], 'prod')
        self.calls.clear()
        with self.http_client(lambda *args: self.fail('cached window must not query')), \
                patch.object(selection.ad.cloud, 'download_to_file', side_effect=AssertionError('no download')), \
                patch.object(selection.ad.subprocess,'run',side_effect=AssertionError('no native retry')):
            repeated = window_pipeline.process_window(META, settings, directory, rule_path)
        self.assertEqual(repeated['ad'], result['ad'])
        self.assertEqual(repeated['pcm'], result['pcm'])
        self.assertEqual(self.calls, [])
        with self.assertRaises(InputError):
            selection.ad.cached_result(META, directory, rule_path, environment='test')
        with self.assertRaises(InputError):
            selection.pcm.cached_result(META, directory, environment='test')
        with self.assertRaises(InputError):
            selection.ad.fetch_window(META, settings, directory / 'input', environment='test')
        with self.assertRaises(InputError):
            selection.pcm.prepare(META, settings, directory, environment='test')
        with self.assertRaises(ValueError):
            direct_ad.analyze(directory / 'input', META, rules, environment='testtwo')

    def test_window_result_rejects_mixed_environment_sources(self):
        with self.discovery({('prod', 'log_fsdA'): [self.item('prod.zip')]}):
            decision, _ = self.select()
        good = {'version': window_pipeline.RESULT_VERSION, 'request': META, 'environment': 'prod',
                'environment_selection': decision, 'rules_sha256': 'synthetic-rules', 'status': 'partial',
                'ad': {'environment': 'prod', 'source': {'environment': 'prod'}, 'status': 'ok'},
                'pcm': {'source': {'environment': 'prod'}, 'status': 'failed'},
                'sources': window_pipeline.source_plan(META, 'prod')}
        window_pipeline.validate_result(good, META, 'synthetic-rules', environment_policy='test_then_prod')
        paths = [('environment',), ('ad', 'environment'), ('ad', 'source', 'environment'),
                 ('pcm', 'source', 'environment'), ('sources', 'ad', 'environment'),
                 ('sources', 'pcm', 'environment')]
        for path in paths:
            with self.subTest(path=path):
                value = deepcopy(good)
                target = value
                for name in path[:-1]:
                    target = target[name]
                target[path[-1]] = 'testtwo'
                with self.assertRaises(InputError):
                    window_pipeline.validate_result(value, META, 'synthetic-rules',
                                                    environment_policy='test_then_prod')


if __name__ == '__main__':
    unittest.main()
