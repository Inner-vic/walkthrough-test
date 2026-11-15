"""Finite Prod null-envelope compatibility and query audit; in-memory HTTP only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import cloud_log_client as cloud


META = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-30T15:00:00', 'end': '2026-10-30T15:35:00'}
SECRET = 'SYNTHETIC-DO-NOT-PERSIST'
ENVIRONMENTS = {name: {'origin': 'https://' + name + '.query.invalid',
    'providerEnvironment': 'prod' if name == 'prod' else 'testtwo', 'displayName': name,
    'trustedDownloadHosts': {'https': {name + '.download.invalid'}, 'http': set()}}
    for name in ('prod', 'test')}


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        content = json.dumps(payload).encode('utf-8')
        super().__init__(content)
        self.status = status
        self.headers = {'Content-Length': str(len(content))}


def envelope():
    return {'success': True, 'code': 200, 'message': '操作成功！', 'data': None, 'timestamp': 1793430000000}


def params(log_class='log_fsdA_service', metadata=None):
    metadata = metadata or META
    query = cloud._planned_query(metadata, log_class)
    return {'vin': metadata['vin'], 'logClass': log_class, **query, 'userName': SECRET,
            'remoteMode': 'false', 'domain': '', 'eeaPlatform': 'EEA3.0', 'pageNo': 1, 'pageSize': 100}


class ProdNullClientCompatTests(unittest.TestCase):
    @contextmanager
    def http(self, reply=None, *, status=200, opener=None):
        if opener is None:
            opener = SimpleNamespace(open=lambda *args, **kwargs: Response(reply, status))
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                patch.object(cloud.urllib.request, 'build_opener', return_value=opener):
            yield opener

    def test_exact_envelope_normalizes_without_losing_original_data_type(self):
        original = envelope()
        with self.http(original):
            result = cloud.request_json(params(), SECRET, environment='prod')
        self.assertIsNone(original['data'])
        self.assertEqual(result['data'], {'list': [], 'total': 0})
        self.assertEqual(result.source_data_type, 'null')
        self.assertEqual(result.compatibility_evidence['source_data_type'], 'null')
        cloud.validate_empty_response_compat([result.compatibility_evidence], META, environment='prod')
        self.assertNotIn('compatibility_evidence', result)

    def test_each_observed_envelope_field_has_a_strict_negative_boundary(self):
        changes = [
            lambda value: value.update(code='200'), lambda value: value.update(code=True),
            lambda value: value.update(code=False), lambda value: value.update(code=0),
            lambda value: value.pop('code'), lambda value: value.update(code=None),
            lambda value: value.update(success=1), lambda value: value.update(success='true'),
            lambda value: value.update(success=None), lambda value: value.pop('success'),
            lambda value: value.update(success=False), lambda value: value.update(message='unknown'),
            lambda value: value.pop('message'), lambda value: value.pop('data'),
            lambda value: value.update(timestamp=True), lambda value: value.update(timestamp='1793430000000'),
            lambda value: value.update(error=None), lambda value: value.update(errors=[]),
            lambda value: value.update(status=200),
            lambda value: value.update(_empty_response_compat={'verified': True}),
        ]
        for index, change in enumerate(changes):
            reply = envelope()
            change(reply)
            with self.subTest(change=index), self.http(reply), self.assertRaises(cloud.CloudQueryError):
                cloud.request_json(params(), SECRET, environment='prod')
        for environment, status in (('test', 200), ('prod', 201), ('prod', None), ('prod', True)):
            with self.subTest(environment=environment, status=status), self.http(envelope(), status=status), \
                    self.assertRaises(cloud.CloudResponseContractError):
                cloud.request_json(params(), SECRET, environment=environment)

    def test_only_current_approved_first_page_query_shape_is_compatible(self):
        changes = [{'pageNo': 2}, {'pageNo': True}, {'pageNo': '1'}, {'pageSize': 20},
            {'pageSize': '100'}, {'vin': 'OTHER'}, {'logClass': 'unknown'}, {'userName': 'other'},
            {'domain': 'other'}, {'remoteMode': False}, {'eeaPlatform': 'OTHER'}, {'token': SECRET}]
        for fields in changes:
            query = {**params(), **fields}
            with self.subTest(fields=list(fields)), self.http(envelope()), \
                    self.assertRaises(cloud.CloudResponseContractError):
                cloud.request_json(query, SECRET, environment='prod')

    def test_correct_object_envelopes_remain_supported_but_boolean_confusion_is_rejected(self):
        for code in (None, 0, '0', 200, '200'):
            reply = {'code': code, 'data': {'list': [], 'total': 0}}
            with self.subTest(code=code), self.http(reply):
                result = cloud.request_json(params(), SECRET, environment='prod')
            self.assertEqual(result.source_data_type, 'object')
            self.assertIsNone(result.compatibility_evidence)
        for fields in ({'code': False}, {'code': True}, {'success': 1}, {'success': 'true'}, {'success': None}):
            with self.subTest(fields=fields), self.http({'code': 200, 'data': {'list': [], 'total': 0}, **fields}), \
                    self.assertRaises(cloud.CloudResponseError):
                cloud.request_json(params(), SECRET, environment='prod')

    def test_server_json_markers_do_not_manufacture_internal_provenance(self):
        reply = {'code': 200, 'data': {'list': [], 'total': 0}, 'source_data_type': 'null',
                 'compatibility_evidence': {'verified': True, 'source_message_class': 'operation_success'}}
        with self.http(reply), cloud.query_audit_context(request=META, environment='prod') as evidence:
            result = cloud.request_json(params(), SECRET, environment='prod')
        self.assertEqual(result.source_data_type, 'object')
        self.assertIsNone(result.compatibility_evidence)
        self.assertEqual(evidence, [])

    def test_completed_audit_covers_auth_network_and_server_failures_without_raw_details(self):
        failures = [
            (urllib.error.HTTPError('https://private.invalid/' + SECRET, 401, SECRET, {}, None),
             cloud.CloudAuthError, 'authentication_failed', 401),
            (urllib.error.HTTPError('https://private.invalid/' + SECRET, 503, SECRET, {}, None),
             cloud.CloudServerError, 'server_failed', 503),
            (urllib.error.URLError(SECRET), cloud.CloudNetworkError, 'network_failed', None),
            (TimeoutError(SECRET), cloud.CloudNetworkError, 'network_failed', None),
        ]
        for failure, error_type, classification, status in failures:
            events = []
            def opened(*args, **kwargs):
                raise failure
            with self.subTest(classification=classification, error_type=type(failure).__name__), \
                    self.http(opener=SimpleNamespace(open=opened)), \
                    cloud.query_audit_context(request=META, environment='prod', on_event=events.append) as evidence:
                with self.assertRaises(error_type):
                    cloud.request_json(params(), SECRET, environment='prod')
            self.assertEqual(evidence, [])
            self.assertEqual([event['phase'] for event in events], ['dispatched', 'completed'])
            self.assertEqual(events[-1]['classification'], classification)
            self.assertEqual(events[-1]['http_status'], status)
            self.assertNotIn(SECRET, json.dumps(events))

    def test_audit_precedes_dispatch_and_preserves_raw_null_before_completion(self):
        events = []
        def opened(request, **kwargs):
            self.assertEqual([event['phase'] for event in events], ['dispatched'])
            return Response(envelope())
        def completed(event):
            cloud.validate_query_audit_event(event, META, environment='prod', role='baseline')
            if event['phase'] == 'completed':
                self.assertEqual(len(evidence), 1)
            events.append(event)
        with self.http(opener=SimpleNamespace(open=opened)), \
                cloud.query_audit_context(role='baseline', request=META, environment='prod', on_event=completed) as evidence:
            result = cloud.request_json(params('log_fsdA'), SECRET, environment='prod')
        self.assertEqual([event['phase'] for event in events], ['dispatched', 'completed'])
        self.assertEqual(events[-1]['source_data_type'], 'null')
        self.assertEqual(events[-1]['classification'], 'known_prod_success_null_empty')
        self.assertEqual(evidence, [result.compatibility_evidence])
        self.assertEqual(evidence[0]['role'], 'baseline')
        serialized = json.dumps({'events': events, 'evidence': evidence})
        for sensitive in (SECRET, 'https://', 'userName', '操作成功！', META['vin']):
            self.assertNotIn(sensitive, serialized)

    def test_audit_keeps_first_compat_when_a_later_request_fails(self):
        events = []
        replies = iter((envelope(), {**envelope(), 'message': SECRET}))
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response(next(replies)))
        with self.http(opener=opener), \
                cloud.query_audit_context(role='replacement', request=META, environment='prod', on_event=events.append) as evidence:
            cloud.request_json(params('log_fsdA'), SECRET, environment='prod')
            with self.assertRaises(cloud.CloudResponseContractError):
                cloud.request_json(params('log_fsdB'), SECRET, environment='prod')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(len(events), 4)
        self.assertEqual(events[-1]['classification'], 'response_contract_failed')
        self.assertEqual(events[-1]['log_class'], 'log_fsdB')
        self.assertNotIn(SECRET, json.dumps(events))

    def test_audit_does_not_dispatch_wrong_vin_environment_or_time(self):
        for fields, environment in (({'vin': 'OTHER'}, 'prod'), ({}, 'test'),
                ({'collectTimeMax': '2026-10-30 15:36:00', 'collectTimeMaxMs': 1793345760000}, 'prod')):
            opened = []
            opener = SimpleNamespace(open=lambda *args, **kwargs: opened.append(True))
            with self.subTest(fields=list(fields), environment=environment), self.http(opener=opener), \
                    cloud.query_audit_context(request=META, environment='prod'), \
                    self.assertRaises(cloud.CloudConfigError):
                cloud.request_json({**params(), **fields}, SECRET, environment=environment)
            self.assertEqual(opened, [])

    def test_contexts_are_thread_local_and_are_cleared_after_exceptions(self):
        barrier = threading.Barrier(2, timeout=3)
        def opened(*args, **kwargs):
            barrier.wait()
            return Response(envelope())
        def run(role):
            events = []
            with cloud.query_audit_context(role=role, request=META, environment='prod', on_event=events.append) as evidence:
                cloud.request_json(params(), SECRET, environment='prod')
            return evidence, events
        with self.http(opener=SimpleNamespace(open=opened)), ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(zip(('baseline', 'replacement'), pool.map(run, ('baseline', 'replacement'))))
        for role, (evidence, events) in results.items():
            self.assertEqual(evidence[0]['role'], role)
            self.assertEqual({event['role'] for event in events}, {role})
            self.assertEqual([event['sequence'] for event in events], [1, 1])
        with self.assertRaisesRegex(ValueError, 'synthetic'):
            with cloud.query_audit_context(request=META, environment='prod'):
                raise ValueError('synthetic')
        with self.http(envelope()):
            self.assertIsNone(cloud.request_json(params(), SECRET, environment='prod').compatibility_evidence['role'])

    def test_evidence_and_audit_validation_reject_forged_extra_or_detached_fields(self):
        events = []
        with self.http(envelope()), cloud.query_audit_context(request=META, environment='prod', on_event=events.append) as evidence:
            cloud.request_json(params(), SECRET, environment='prod')
        mutations = [lambda row: row.update(role='baseline'), lambda row: row.update(environment='test'),
            lambda row: row.update(page_no=True), lambda row: row.update(page_size='100'),
            lambda row: row.update(http_status=True), lambda row: row.update(business_code=True),
            lambda row: row.update(success=1), lambda row: row.update(raw_message=SECRET),
            lambda row: row['query'].update(collectTimeMaxMs=True)]
        for index, mutate in enumerate(mutations):
            invalid = deepcopy(evidence)
            mutate(invalid[0])
            with self.subTest(evidence_mutation=index), self.assertRaises(ValueError):
                cloud.validate_empty_response_compat(invalid, META, environment='prod')
        with self.assertRaises(ValueError):
            cloud.validate_empty_response_compat(evidence * 2, META, environment='prod')
        for mutate in (lambda row: row.update(raw_message=SECRET), lambda row: row.update(classification='unknown'),
                lambda row: row.update(sequence=True), lambda row: row.update(success=1)):
            invalid = deepcopy(events[-1])
            mutate(invalid)
            with self.assertRaises(ValueError):
                cloud.validate_query_audit_event(invalid, META, environment='prod')


if __name__ == '__main__':
    unittest.main()
