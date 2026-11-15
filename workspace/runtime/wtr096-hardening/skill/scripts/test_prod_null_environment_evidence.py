"""Offline client/selection provenance for the observed Prod success-null shape."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import cloud_log_client as cloud
import environment_selection as selection
from feishu_report import _environment_evidence
from request_model import InputError
from test_task_environment_selection import (REQUESTS, legacy_fixture, per_window_pipeline_fixture,
                                             per_window_task_fixture, task_fixture, pipeline_fixture)


class Response(io.BytesIO):
    status = 200
    headers = {}


class ProdNullEnvironmentEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'local_analysis': {'environment': 'prod_then_test'},
                         'cloud_settings_path': 'synthetic-unused-settings'}
        self.calls = []

    def item(self, log_class, start, index=0):
        return {'vin': REQUESTS['baseline']['vin'], 'fileKey': f'{start}-{log_class}-{index}',
                'fileName': ('AVM_Service-synthetic.zst' if log_class == cloud.WALKTHROUGH_LOG_CLASS
                             else log_class + '.zip'), 'fileSize': 1,
                'downloadURL': 'https://synthetic.download.invalid/log'}

    def select(self, response=None, name='case'):
        def open_request(request, **kwargs):
            fields = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            env = 'prod' if urllib.parse.urlsplit(request.full_url).hostname.startswith('prod.') else 'test'
            kind, start, page = fields['logClass'][0], fields['collectTimeMin'][0], int(fields['pageNo'][0])
            self.calls.append((env, kind, start, page))
            if response:
                payload = response(env, kind, start, page)
            elif env == 'prod' and start == REQUESTS['baseline']['start'].replace('T', ' '):
                payload = {'code': 200, 'success': True, 'message': '操作成功！', 'data': None}
            else:
                payload = {'code': 200, 'success': True, 'message': '操作成功！',
                           'data': {'list': [self.item(kind, start)], 'total': 1}}
            if isinstance(payload, Exception):
                raise payload
            return Response(json.dumps(payload).encode('utf-8'))
        environments = {env: {'origin': f'https://{env}.query.invalid',
            'providerEnvironment': 'prod' if env == 'prod' else 'testtwo', 'displayName': env,
            'trustedDownloadHosts': {'https': {'synthetic.download.invalid'}, 'http': set()}}
            for env in ('prod', 'test')}
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, environments, clear=True), \
             patch.object(cloud, 'load_settings', return_value={'username': 'SYNTHETIC-NOT-PERSISTED'}), \
             patch.object(cloud.urllib.request, 'build_opener', return_value=SimpleNamespace(open=open_request)):
            return selection.select_task(REQUESTS, self.settings, self.root / name)

    def test_real_clients_keep_five_prod_null_proofs_and_choose_each_window(self):
        task, discovery = self.select()
        baseline, replacement = (task['windows'][role] for role in selection.ROLES)
        self.assertEqual((baseline['environment'], replacement['environment']), ('test', 'prod'))
        first = baseline['attempts'][0]
        self.assertEqual(first['counts'], dict.fromkeys(selection.CLASSES, 0))
        evidence = first['empty_response_compat']
        self.assertEqual({row['log_class'] for row in evidence}, set(selection.CLASSES))
        self.assertTrue(all(row['role'] == 'baseline' and row['environment'] == 'prod'
                            and row['page_no'] == 1 for row in evidence))
        self.assertNotIn('empty_response_compat', baseline['attempts'][1])
        self.assertNotIn('empty_response_compat', replacement['attempts'][0])
        self.assertEqual(len(self.calls), 15)
        selection.validate_task_decision(task, REQUESTS, 'prod_then_test')
        saved = json.loads((self.root / 'case/task-environment-selection.json').read_text('utf-8'))
        self.assertEqual(saved, task)
        self.assertNotIn('SYNTHETIC-NOT-PERSISTED', json.dumps(task))
        self.assertEqual(len(discovery['baseline']['ad']['log_fsdA']), 1)

    def test_ordinary_empty_lists_never_gain_a_null_compat_marker(self):
        def respond(env, kind, start, page):
            items = [] if env == 'prod' else [self.item(kind, start)]
            return {'code': 200, 'success': True, 'message': '操作成功！',
                    'data': {'list': items, 'total': len(items)}}
        task, _ = self.select(respond)
        self.assertTrue(all('empty_response_compat' not in attempt for choice in task['windows'].values()
                            for attempt in choice['attempts']))

    def test_one_null_class_preserves_other_counts_and_does_not_force_fallback(self):
        def respond(env, kind, start, page):
            if kind == 'log_xcu_mcu':
                return {'code': 200, 'success': True, 'message': '操作成功！', 'data': None}
            return {'code': 200, 'success': True, 'message': '操作成功！',
                    'data': {'list': [self.item(kind, start)], 'total': 1}}
        task, _ = self.select(respond)
        self.assertTrue(all(row[0] == 'prod' for row in self.calls))
        self.assertEqual(len(self.calls), 10)
        for role in selection.ROLES:
            choice = task['windows'][role]
            self.assertEqual(choice['environment'], 'prod')
            self.assertEqual(len(choice['attempts']), 1)
            attempt = choice['attempts'][0]
            self.assertEqual(attempt['counts']['log_xcu_mcu'], 0)
            self.assertEqual(sum(attempt['counts'].values()), 4)
            self.assertEqual([row['log_class'] for row in attempt['empty_response_compat']], ['log_xcu_mcu'])

    def test_compat_proof_is_rejected_if_count_role_query_or_page_is_changed(self):
        task, _ = self.select()
        changes = [('count', 1), ('role', 'replacement'), ('page_no', 2),
                   ('environment', 'test'), ('http_status', True), ('page_size', 101),
                   ('query_end', '2026-10-21 15:01:00'), ('unknown_key', 'SYNTHETIC-SECRET')]
        for field, value in changes:
            changed = deepcopy(task)
            attempt = changed['windows']['baseline']['attempts'][0]
            row = attempt['empty_response_compat'][0]
            if field == 'count':
                attempt['counts'][row['log_class']] = value
            elif field == 'query_end':
                row['query']['collectTimeMax'] = value
            else:
                row[field] = value
            with self.subTest(field=field), self.assertRaises(InputError):
                selection.validate_task_decision(changed, REQUESTS, 'prod_then_test')
        for replacement in ([], [deepcopy(task['windows']['baseline']['attempts'][0]['empty_response_compat'][0])] * 2):
            changed = deepcopy(task)
            changed['windows']['baseline']['attempts'][0]['empty_response_compat'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(InputError):
                selection.validate_task_decision(changed, REQUESTS, 'prod_then_test')

    def test_later_page_null_is_fatal_and_cannot_create_fallback_or_selection(self):
        def respond(env, kind, start, page):
            if kind == 'log_fsdA' and page == 1:
                return {'code': 200, 'success': True, 'message': '操作成功！',
                        'data': {'list': [self.item(kind, start)], 'total': 2}}
            return {'code': 200, 'success': True, 'message': '操作成功！', 'data': None}
        with self.assertRaises(cloud.CloudResponseContractError):
            self.select(respond)
        self.assertEqual([row[0] for row in self.calls], ['prod', 'prod'])
        self.assertFalse((self.root / 'case/task-environment-selection.json').exists())

    def test_real_exception_after_a_null_does_not_fallback(self):
        def respond(env, kind, start, page):
            if kind == 'log_fsdA':
                return {'code': 200, 'success': True, 'message': '操作成功！', 'data': None}
            return urllib.error.URLError('SYNTHETIC-PRIVATE-DETAIL')
        with self.assertRaises(cloud.CloudNetworkError):
            self.select(respond)
        self.assertEqual([row[0] for row in self.calls], ['prod', 'prod'])
        audit = json.loads((self.root / 'case/task-environment-attempts.json').read_text('utf-8'))
        self.assertEqual(audit['status'], 'failed')
        failed = audit['windows']['baseline']
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['query_progress']['classification'], 'network_failed')
        self.assertEqual(failed['query_progress']['log_class'], 'log_fsdB')
        self.assertEqual(failed['query_progress']['role'], 'baseline')
        self.assertEqual(failed['query_progress']['page_no'], 1)
        self.assertEqual(len(failed['pending_empty_response_compat']), 1)
        self.assertEqual(failed['pending_empty_response_compat'][0]['log_class'], 'log_fsdA')
        self.assertNotIn('SYNTHETIC-PRIVATE-DETAIL', json.dumps(audit))

    def test_report_environment_validation_preserves_optional_evidence(self):
        task, _ = self.select()
        pipeline = per_window_pipeline_fixture(task)
        before = deepcopy(pipeline)
        self.assertEqual(selection.validate_task_pipeline(pipeline, REQUESTS, 'prod_then_test'), pipeline)
        environment, note = _environment_evidence(pipeline, 'baseline', {})
        self.assertEqual(environment, 'testtwo')
        self.assertIn('本时段改用 testtwo', note)
        self.assertEqual(pipeline, before)

    def test_older_finals_without_optional_evidence_are_not_rewritten(self):
        fixtures = [per_window_pipeline_fixture(per_window_task_fixture()),
                    pipeline_fixture(task_fixture()), legacy_fixture()]
        for final in fixtures:
            before = deepcopy(final)
            selection.validate_cached_pipeline(final, REQUESTS, 'test_then_prod')
            self.assertEqual(final, before)


if __name__ == '__main__':
    unittest.main()
