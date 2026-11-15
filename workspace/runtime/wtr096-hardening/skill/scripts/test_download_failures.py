"""Bounded download diagnostics and retries against in-memory HTTP only."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

import cloud_log_client as cloud
from download_scheduler import DownloadBudget


SECRET = 'SYNTHETIC-DO-NOT-DISCLOSE'
URL = 'https://prod.download.invalid/object?opaque=' + SECRET
ENVIRONMENTS = {name: {'origin': 'https://' + name + '.query.invalid',
    'providerEnvironment': 'prod' if name == 'prod' else 'testtwo', 'displayName': name,
    'trustedDownloadHosts': {'https': {name + '.download.invalid'}, 'http': set()}}
    for name in ('prod', 'test')}


class ErrorBody(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.read_bounds = []

    def read(self, maximum=-1):
        if not 0 <= maximum <= 16 * 1024:
            raise AssertionError('error body read must be bounded')
        self.read_bounds.append(maximum)
        return super().read(maximum)


class DownloadResponse(io.BytesIO):
    def __init__(self, value=b'bytes', *, content_length=None):
        super().__init__(value)
        self.headers = {} if content_length is None else {'Content-Length': str(content_length)}

    def geturl(self):
        return URL


class DownloadFailureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'source.zip'
        for name in ('socket.create_connection', 'socket.getaddrinfo', 'socket.socket.connect'):
            guard = patch(name, side_effect=AssertionError('no live network'))
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def http(self, respond):
        calls = []
        def opened(request, **kwargs):
            self.assertEqual(request.full_url, URL)
            calls.append(True)
            return respond()
        with patch.dict(cloud.CLOUD_ENVIRONMENTS, ENVIRONMENTS, clear=True), \
                patch.object(cloud.urllib.request, 'build_opener', return_value=SimpleNamespace(open=opened)), \
                patch.object(cloud.time, 'sleep') as sleep:
            yield calls, sleep

    def run_download(self, **kwargs):
        return cloud.download_to_file(URL, self.target, environment='prod', **kwargs)

    def assert_no_output(self):
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_http400_known_code_is_structured_not_retried_and_never_leaks_body(self):
        body = ErrorBody(json.dumps({'errors': [{'error_code': 'DOWNLOAD_FILE_FAILURE',
            'message': SECRET, 'help_url': URL}]}).encode())
        def denied():
            raise urllib.error.HTTPError(URL, 400, SECRET, {}, body)
        with self.http(denied) as (calls, sleep), self.assertRaises(cloud.CloudDownloadError) as caught:
            self.run_download()
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertEqual(body.read_bounds, [16 * 1024])
        self.assertIsInstance(caught.exception, cloud.CloudResponseError)
        diagnostic = cloud.download_failure_diagnostic(caught.exception)
        self.assertEqual(diagnostic, {'category': 'cloud_download_failed', 'operation': 'download',
            'environment': 'prod', 'reason': 'http_bad_request', 'retryable': False,
            'http_status': 400, 'attempts': 1, 'upstream_code': 'DOWNLOAD_FILE_FAILURE'})
        self.assertIn('生产环境日志文件下载失败（HTTP 400）', cloud.download_failure_message(diagnostic))
        self.assertIn('尚未完成分析', str(caught.exception))
        self.assertNotIn(SECRET, json.dumps(diagnostic) + str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_no_output()

    def test_http400_unknown_invalid_and_oversized_bodies_do_not_supply_a_code(self):
        bodies = [b'not json', b'[]', b'{}', json.dumps({'errors': [{'error_code': SECRET}]}).encode(),
            json.dumps({'errors': [{'error_code': 'download_file_failure'}]}).encode(),
            json.dumps({'errors': {'error_code': 'DOWNLOAD_FILE_FAILURE'}}).encode(),
            b' ' * (16 * 1024) + b'{"errors":[{"error_code":"DOWNLOAD_FILE_FAILURE"}]}']
        for index, value in enumerate(bodies):
            body = ErrorBody(value)
            def denied():
                raise urllib.error.HTTPError(URL, 400, SECRET, {}, body)
            with self.subTest(body=index), self.http(denied), self.assertRaises(cloud.CloudDownloadError) as caught:
                self.run_download()
            self.assertNotIn('upstream_code', cloud.download_failure_diagnostic(caught.exception))
            self.assert_no_output()
        body = ErrorBody(b'{"errors":[{"error_code":"DOWNLOAD_FILE_FAILURE"}]}')
        def too_large():
            raise urllib.error.HTTPError(URL, 400, SECRET, {'Content-Length': '999999'}, body)
        with self.http(too_large), self.assertRaises(cloud.CloudDownloadError) as caught:
            self.run_download()
        self.assertEqual(body.read_bounds, [])
        self.assertNotIn('upstream_code', cloud.download_failure_diagnostic(caught.exception))

    def test_rate_limit_and_server_failures_retry_at_most_three_identical_gets(self):
        for code in (429, 500, 502, 503, 504, 599):
            bodies = []
            def unavailable():
                body = ErrorBody(SECRET.encode())
                bodies.append(body)
                raise urllib.error.HTTPError(URL, code, SECRET, {}, body)
            with self.subTest(code=code), self.http(unavailable) as (calls, sleep), \
                    self.assertRaises(cloud.CloudDownloadError) as caught:
                self.run_download()
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(all(body.read_bounds == [] for body in bodies))
            diagnostic = cloud.download_failure_diagnostic(caught.exception)
            self.assertEqual(diagnostic['http_status'], code)
            self.assertEqual(diagnostic['attempts'], 3)
            self.assertTrue(diagnostic['retryable'])
            self.assertEqual(diagnostic['reason'], 'http_rate_limited' if code == 429 else 'http_server_error')
            if code >= 500:
                self.assertIsInstance(caught.exception, cloud.CloudServerError)
            self.assert_no_output()

    def test_retries_can_finish_atomically_and_release_partial_network_budget(self):
        budget = DownloadBudget(5)
        count = 0
        class Interrupted(DownloadResponse):
            def read(self, maximum):
                if self.tell():
                    raise ConnectionResetError(SECRET)
                return super().read(2)
        def respond():
            nonlocal count
            count += 1
            if count == 1:
                return Interrupted(b'ab')
            if count == 2:
                raise urllib.error.HTTPError(URL, 503, SECRET, {}, None)
            return DownloadResponse(b'bytes', content_length=5)
        with self.http(respond) as (calls, sleep):
            self.assertEqual(self.run_download(byte_budget=budget), 5)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.target.read_bytes(), b'bytes')
        self.assertEqual(budget.used, 5)
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_exhausted_network_is_structured_without_retaining_exception_text(self):
        def disconnected():
            raise urllib.error.URLError(SECRET + URL)
        with self.http(disconnected) as (calls, sleep), self.assertRaises(cloud.CloudDownloadError) as caught:
            self.run_download()
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIsInstance(caught.exception, cloud.CloudNetworkError)
        diagnostic = cloud.download_failure_diagnostic(caught.exception)
        self.assertEqual(diagnostic['reason'], 'network_error')
        self.assertEqual(diagnostic['attempts'], 3)
        self.assertNotIn('http_status', diagnostic)
        self.assertNotIn(SECRET, json.dumps(diagnostic) + str(caught.exception))
        self.assert_no_output()

    def test_auth_redirect_unknown_http_and_invalid_content_are_not_retried(self):
        for code in (301, 400, 401, 403, 404):
            body = ErrorBody(SECRET.encode())
            def rejected():
                raise urllib.error.HTTPError(URL, code, SECRET, {}, body)
            expected = cloud.CloudAuthError if code in (401, 403) else cloud.CloudResponseError
            with self.subTest(code=code), self.http(rejected) as (calls, sleep), self.assertRaises(expected) as caught:
                self.run_download()
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()
            if code != 400:
                self.assertEqual(body.read_bounds, [])
                self.assertNotIsInstance(caught.exception, cloud.CloudDownloadError)
            self.assert_no_output()
        for response in (DownloadResponse(b''), DownloadResponse(b'bytes', content_length='invalid'),
                         DownloadResponse(b'bytes', content_length=100000)):
            with self.subTest(response=response.headers), self.http(lambda: response) as (calls, sleep), \
                    self.assertRaises(cloud.CloudResponseError):
                self.run_download(max_bytes=10)
            self.assertEqual(len(calls), 1)
            sleep.assert_not_called()
            self.assert_no_output()

    def test_retry_limit_and_failure_constructor_accept_only_bounded_exact_types(self):
        for attempts in (0, -1, 4, True, '3', None):
            with self.subTest(attempts=attempts), patch.object(cloud, '_download_once') as once, \
                    self.assertRaises(cloud.CloudConfigError):
                self.run_download(attempts=attempts)
            once.assert_not_called()
        for fields in ({'reason': SECRET}, {'environment': SECRET}, {'http_status': True},
                {'http_status': 500}, {'attempts': True}, {'attempts': 4}, {'upstream_code': SECRET}):
            values = {'reason': 'http_bad_request', 'environment': 'prod', 'http_status': 400, **fields}
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError) as caught:
                cloud.CloudDownloadError(**values)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_diagnostics_ignore_mutated_args_payload_chains_and_unvalidated_fields(self):
        error = cloud.CloudDownloadError(reason='http_bad_request', environment='prod', http_status=400,
                                         upstream_code='DOWNLOAD_FILE_FAILURE')
        error.args = (SECRET + URL,)
        error.payload = {'error_message': SECRET}
        error.__cause__ = RuntimeError(SECRET)
        self.assertNotIn(SECRET, json.dumps(cloud.download_failure_diagnostic(error)))
        for name in ('reason', 'environment', 'http_status', 'attempts', 'upstream_code'):
            setattr(error, name, SECRET)
        diagnostic = cloud.download_failure_diagnostic(error)
        self.assertEqual(diagnostic, {'category': 'cloud_download_failed', 'operation': 'download', 'retryable': False})
        self.assertNotIn(SECRET, cloud.download_failure_message(diagnostic))
        with self.assertRaises(TypeError):
            cloud.download_failure_diagnostic(RuntimeError(SECRET))


if __name__ == '__main__':
    unittest.main()
