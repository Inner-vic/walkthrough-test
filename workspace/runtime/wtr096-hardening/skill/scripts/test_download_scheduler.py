"""Offline checks of real transfer boundaries and bounded file concurrency.

All HTTP responses are in memory. The socket guards make accidental live I/O fail.
"""
from concurrent.futures import CancelledError
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import cloud_log_client as cloud
import download_scheduler as scheduler
import native_log_source as native
import service_log_source as service
from request_model import InputError


URL = 'https://test.download.invalid/'
META = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:48:00',
        'end': '2026-10-21T16:22:00'}


class MemoryResponse:
    def __init__(self, chunks, *, url=URL + 'input.bin', headers=None, barrier=None):
        self.chunks = iter(chunks)
        self.url = url
        self.headers = headers or {}
        self.barrier = barrier
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, maximum):
        self.reads += 1
        if self.barrier is not None and self.reads == 1:
            self.barrier.wait(timeout=5)
        value = next(self.chunks, b'')
        if isinstance(value, BaseException):
            raise value
        if len(value) > maximum:
            raise AssertionError('fixture must obey the requested read bound')
        return value


class TrackingBudget(scheduler.DownloadBudget):
    def __init__(self, maximum, first_charge_barrier=None):
        super().__init__(maximum)
        self.peak = 0
        self.history = []
        self.observation_lock = threading.Lock()
        self.first_charge_barrier = first_charge_barrier
        self.local = threading.local()

    def consume(self, count):
        super().consume(count)
        with self.observation_lock:
            self.peak = max(self.peak, self.used)
            self.history.append(self.used)
        if self.first_charge_barrier is not None and not getattr(self.local, 'charged', False):
            self.local.charged = True
            self.first_charge_barrier.wait(timeout=5)


class TransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ('socket.create_connection', 'socket.getaddrinfo', 'socket.socket.connect'):
            guard = patch(target, side_effect=AssertionError('live networking forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def http(self, respond):
        environments = {
            env: {'origin': 'https://' + env + '.query.invalid',
                  'providerEnvironment': 'testtwo' if env == 'test' else 'prod',
                  'displayName': env,
                  'trustedDownloadHosts': {'https': {env + '.download.invalid'},
                                           'http': {env + '.http.invalid'}}}
            for env in ('test', 'prod')}

        def open_request(request, timeout):
            return respond(request.full_url)

        with patch.object(cloud, 'CLOUD_ENVIRONMENTS', environments), \
                patch.object(cloud.urllib.request, 'build_opener',
                             return_value=SimpleNamespace(open=open_request)) as opener:
            yield opener

    def download(self, response, *, budget=None, maximum=100, attempts=1, name='input.bin'):
        path = self.root / name
        with self.http(lambda url: response):
            count = cloud.download_to_file(URL + name, path, environment='test',
                                           max_bytes=maximum, attempts=attempts, byte_budget=budget)
        return path, count

    def assert_no_parts(self):
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_worker_configuration_is_bounded_and_rejects_bool(self):
        self.assertEqual(scheduler.download_workers({}), 4)
        for count in (1, 2, 3, 4):
            self.assertEqual(scheduler.download_workers({'local_analysis': {'download_workers': count}}), count)
        for value in (0, 5, -1, True, '4', 1.5, None):
            with self.subTest(value=value), self.assertRaises(InputError):
                scheduler.download_workers({'local_analysis': {'download_workers': value}})

    def test_four_workers_overlap_and_never_exceed_limit(self):
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        active = 0
        peak = 0

        def task(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=5)
                return value * value
            finally:
                with lock:
                    active -= 1

        self.assertEqual(scheduler.run_bounded(list(range(12)), task, 4), [i * i for i in range(12)])
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)

    def test_completion_order_does_not_change_manifest_order(self):
        gate = threading.Event()
        completed = []

        def task(value):
            if value == 0:
                self.assertTrue(gate.wait(timeout=5))
            if value == 1:
                completed.append(value)
                gate.set()
            else:
                completed.append(value)
            return 'file-' + str(value)

        self.assertEqual(scheduler.run_bounded(list(range(9)), task, 4), ['file-' + str(i) for i in range(9)])
        self.assertLess(completed.index(1), completed.index(0))

    def test_first_failure_stops_queued_work_and_returns_no_partial_result(self):
        barrier = threading.Barrier(4)
        started = []
        lock = threading.Lock()

        def task(value):
            with lock:
                started.append(value)
            barrier.wait(timeout=5)
            if value == 0:
                raise InputError('fixture-first-failure')
            time.sleep(0.03)
            return value

        with self.assertRaisesRegex(InputError, 'fixture-first-failure'):
            scheduler.run_bounded(list(range(40)), task, 4)
        self.assertEqual(sorted(started), [0, 1, 2, 3])

    def test_serial_failure_also_stops_before_later_items(self):
        visited = []

        def task(value):
            visited.append(value)
            if value == 2:
                raise InputError('stop')
            return value

        with self.assertRaises(InputError):
            scheduler.run_bounded(list(range(10)), task, 1)
        self.assertEqual(visited, [0, 1, 2])

    def test_empty_work_has_empty_results(self):
        self.assertEqual(scheduler.run_bounded([], lambda item: self.fail('no work'), 4), [])

    def test_download_function_cancellation_is_an_error_not_partial_success(self):
        def cancelled(item):
            raise CancelledError('fixture cancellation')

        with self.assertRaisesRegex(CancelledError, 'fixture cancellation'):
            scheduler.run_bounded([0], cancelled, 4)

    def test_budget_inputs_and_failed_consume_do_not_change_usage(self):
        for maximum in (0, -1, True, 1.2):
            with self.assertRaises(InputError):
                scheduler.DownloadBudget(maximum)
        budget = scheduler.DownloadBudget(9)
        budget.consume(4)
        for count in (-1, 6, True):
            with self.assertRaises(InputError):
                budget.consume(count)
            self.assertEqual(budget.used, 4)
        for count in (-1, 5, True):
            with self.assertRaises(InputError):
                budget.release(count)
            self.assertEqual(budget.used, 4)
        budget.release(4)
        self.assertEqual(budget.used, 0)

    def test_unknown_length_parallel_streams_cannot_exceed_aggregate_cap(self):
        budget = TrackingBudget(12, threading.Barrier(4))

        def respond(url):
            return MemoryResponse([b'abc', b'def'], url=url)

        def task(index):
            return cloud.download_to_file(URL + str(index), self.root / str(index),
                                          environment='test', max_bytes=100,
                                          attempts=1, byte_budget=budget)

        with self.http(respond), self.assertRaises(InputError):
            scheduler.run_bounded(list(range(4)), task, 4)
        self.assertLessEqual(budget.peak, budget.maximum)
        self.assertTrue(all(0 <= used <= 12 for used in budget.history))
        self.assertEqual(sum(path.stat().st_size for path in self.root.iterdir()), budget.used)
        self.assert_no_parts()

    def test_successful_files_remain_charged_for_following_downloads(self):
        budget = scheduler.DownloadBudget(10)
        first, count = self.download(MemoryResponse([b'abcdef']), budget=budget, name='first.bin')
        self.assertEqual(count, 6)
        self.assertEqual(budget.used, 6)
        with self.assertRaises(InputError):
            self.download(MemoryResponse([b'ghi', b'jk']), budget=budget, name='second.bin')
        self.assertEqual(first.read_bytes(), b'abcdef')
        self.assertFalse((self.root / 'second.bin').exists())
        self.assertEqual(budget.used, 6)
        self.assert_no_parts()

    def test_transient_retry_releases_partial_bytes_before_retry(self):
        budget = scheduler.DownloadBudget(6)
        calls = []

        def respond(url):
            calls.append(budget.used)
            chunks = [b'abc', ConnectionResetError('fixture')] if len(calls) == 1 else [b'abcdef']
            return MemoryResponse(chunks, url=url)

        with self.http(respond), patch.object(cloud.time, 'sleep'):
            count = cloud.download_to_file(URL + 'retry.bin', self.root / 'retry.bin',
                                           environment='test', max_bytes=6, attempts=2,
                                           byte_budget=budget)
        self.assertEqual(calls, [0, 0])
        self.assertEqual(count, 6)
        self.assertEqual(budget.used, 6)
        self.assertEqual((self.root / 'retry.bin').read_bytes(), b'abcdef')
        self.assert_no_parts()

    def test_retry_exhaustion_releases_bytes_and_removes_partial_file(self):
        budget = scheduler.DownloadBudget(6)
        with self.http(lambda url: MemoryResponse([b'abc', socket.timeout('fixture')], url=url)), \
                patch.object(cloud.time, 'sleep'), self.assertRaises(cloud.CloudNetworkError):
            cloud.download_to_file(URL + 'failed.bin', self.root / 'failed.bin', environment='test',
                                   max_bytes=6, attempts=3, byte_budget=budget)
        self.assertEqual(budget.used, 0)
        self.assertFalse((self.root / 'failed.bin').exists())
        self.assert_no_parts()

    def test_disk_write_failure_releases_charged_chunk(self):
        budget = scheduler.DownloadBudget(6)

        class FailedWriter:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, chunk):
                raise OSError('fixture disk full')

        with patch.object(Path, 'open', return_value=FailedWriter()), self.assertRaisesRegex(OSError, 'disk full'):
            self.download(MemoryResponse([b'abc']), budget=budget)
        self.assertEqual(budget.used, 0)
        self.assertFalse((self.root / 'input.bin').exists())
        self.assert_no_parts()

    def test_budget_error_is_not_retried(self):
        budget = scheduler.DownloadBudget(2)
        response = MemoryResponse([b'abc'])
        with self.http(lambda url: response) as opener, self.assertRaises(InputError):
            cloud.download_to_file(URL + 'input.bin', self.root / 'input.bin', environment='test',
                                   max_bytes=9, attempts=3, byte_budget=budget)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(budget.used, 0)
        self.assert_no_parts()

    def test_size_limit_enforced_for_declared_and_unknown_lengths(self):
        for headers, chunks in (({'Content-Length': '7'}, [b'abcdefg']), ({}, [b'abc', b'defg'])):
            with self.subTest(headers=headers):
                budget = scheduler.DownloadBudget(100)
                with self.assertRaises(cloud.CloudResponseError):
                    self.download(MemoryResponse(chunks, headers=headers), budget=budget, maximum=6)
                self.assertEqual(budget.used, 0)
                self.assertFalse((self.root / 'input.bin').exists())
                self.assert_no_parts()

    def test_declared_size_mismatch_releases_charge(self):
        budget = scheduler.DownloadBudget(100)
        with self.assertRaises(cloud.CloudNetworkError):
            self.download(MemoryResponse([b'abc', b'def'], headers={'Content-Length': '7'}), budget=budget)
        self.assertEqual(budget.used, 0)
        self.assert_no_parts()

    def test_invalid_content_length_and_empty_payload_fail_closed(self):
        for headers in ({'Content-Length': '-1'}, {'Content-Length': 'broken'}, {}):
            with self.subTest(headers=headers):
                budget = scheduler.DownloadBudget(100)
                with self.assertRaises(cloud.CloudResponseError):
                    self.download(MemoryResponse([], headers=headers), budget=budget)
                self.assertEqual(budget.used, 0)
                self.assert_no_parts()

    def test_white_lists_and_credentials_still_checked_before_open(self):
        denied = ('https://prod.download.invalid/input.bin',
                  'https://unknown.invalid/input.bin', 'http://test.download.invalid/input.bin',
                  'https://user:secret@test.download.invalid/input.bin',
                  'file:///local/input.bin')
        with self.http(lambda url: self.fail('untrusted URL must never open')) as opener:
            for url in denied:
                with self.subTest(url=url), self.assertRaises(cloud.CloudResponseError):
                    cloud.download_to_file(url, self.root / 'denied.bin', environment='test', attempts=1)
        opener.assert_not_called()

    def test_http_redirect_and_changed_resolved_endpoint_still_rejected(self):
        for resolved in ('https://prod.download.invalid/input.bin',
                         'http://test.download.invalid/input.bin',
                         'https://test.download.invalid:444/input.bin'):
            with self.subTest(resolved=resolved):
                budget = scheduler.DownloadBudget(10)
                with self.assertRaises(cloud.CloudResponseError):
                    self.download(MemoryResponse([b'abc'], url=resolved), budget=budget)
                self.assertEqual(budget.used, 0)
                self.assert_no_parts()

        def redirect(url):
            raise urllib.error.HTTPError(url, 302, 'fixture redirect', {}, None)

        with self.http(redirect), self.assertRaises(cloud.CloudResponseError):
            cloud.download_to_file(URL + 'input.bin', self.root / 'input.bin',
                                   environment='test', attempts=1)
        self.assert_no_parts()

    @staticmethod
    def zip_bytes(name='entry.log', payload=b'synthetic payload'):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(name, payload)
        return output.getvalue()

    @staticmethod
    def item(name, payload, *, known_size=True):
        result = {'vin': META['vin'], 'fileName': name, 'downloadURL': URL + name}
        if known_size:
            result['fileSize'] = len(payload)
        return result

    def native_fetch(self, bodies, *, workers=4, known_size=True):
        selected = {'log_fsdA': [self.item(name, body, known_size=known_size) for name, body in bodies.items()],
                    'log_fsdB': [], 'log_xcu_mcu': [], 'log_bzcu_eth': []}
        return native.fetch_window(META, {'local_analysis': {'download_workers': workers}},
                                   self.root / 'ad', selected=selected)

    def test_native_parallel_fetch_keeps_order_hashes_and_one_shared_budget(self):
        bodies = {'file-' + str(i) + '.zip': self.zip_bytes(payload=str(i).encode()) for i in range(4)}
        barrier = threading.Barrier(4)

        def respond(url):
            return MemoryResponse([bodies[url.rsplit('/', 1)[1]]], url=url, barrier=barrier)

        with self.http(respond):
            result = self.native_fetch(bodies)
        self.assertEqual(result['bytes'], sum(map(len, bodies.values())))
        self.assertEqual([Path(item['relative_path']).name for item in result['files']], list(bodies))
        for item in result['files']:
            payload = bodies[Path(item['relative_path']).name]
            self.assertEqual(item['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual((self.root / 'ad' / item['relative_path']).read_bytes(), payload)
        cached_path = self.root / 'ad' / result['files'][0]['relative_path']
        cached_path.write_bytes(b'tampered')
        with self.assertRaisesRegex(InputError, '哈希变化'):
            self.native_fetch(bodies)

    def test_native_unknown_lengths_obey_aggregate_budget_before_manifest(self):
        bodies = {'one.bin': b'abcdef', 'two.bin': b'ghijkl'}
        with self.http(lambda url: MemoryResponse([bodies[url.rsplit('/', 1)[1]]], url=url)), \
                patch.object(native, 'MAX_BYTES', 10), self.assertRaises(InputError):
            self.native_fetch(bodies, known_size=False)
        self.assertFalse((self.root / 'ad/download-manifest.json').exists())
        self.assert_no_parts()

    def test_native_zip_crc_is_verified_before_manifest(self):
        raw = bytearray(self.zip_bytes(payload=b'known-original-payload'))
        offset = raw.index(b'known-original-payload')
        raw[offset] ^= 1
        with self.http(lambda url: MemoryResponse([bytes(raw)], url=url)), self.assertRaises(InputError):
            self.native_fetch({'crc.zip': bytes(raw)})
        self.assertFalse((self.root / 'ad/download-manifest.json').exists())

    def test_native_zip_traversal_is_rejected_before_manifest(self):
        for index, name in enumerate(('../entry.log', '/entry.log', 'C:/entry.log', '..\\entry.log')):
            body = self.zip_bytes(name=name)
            with self.subTest(name=name), self.http(lambda url: MemoryResponse([body], url=url)), \
                    self.assertRaises(InputError):
                selected = {'log_fsdA': [self.item('unsafe.zip', body)], 'log_fsdB': [], 'log_xcu_mcu': [], 'log_bzcu_eth': []}
                native.fetch_window(META, {'local_analysis': {'download_workers': 4}},
                                    self.root / str(index), selected=selected)
            self.assertFalse((self.root / str(index) / 'download-manifest.json').exists())

    def test_declared_native_size_mismatch_never_commits_manifest(self):
        body = self.zip_bytes()
        selected = {'log_fsdA': [{**self.item('mismatch.zip', body), 'fileSize': len(body) + 1}], 'log_fsdB': [], 'log_xcu_mcu': [], 'log_bzcu_eth': []}
        with self.http(lambda url: MemoryResponse([body], url=url)), self.assertRaises(InputError):
            native.fetch_window(META, {'local_analysis': {'download_workers': 4}},
                                self.root / 'mismatch', selected=selected)
        self.assertFalse((self.root / 'mismatch/download-manifest.json').exists())

    def test_service_parallel_fetch_keeps_order_hashes_and_cache_checks(self):
        bodies = {'AVM_Service-' + str(i) + '.zst': ('source-' + str(i)).encode() for i in range(4)}
        selected = [self.item(name, body) for name, body in bodies.items()]
        barrier = threading.Barrier(4)

        def respond(url):
            return MemoryResponse([bodies[url.rsplit('/', 1)[1]]], url=url, barrier=barrier)

        settings = {'local_analysis': {'download_workers': 4}}
        with self.http(respond):
            result = service.prepare(META, settings, self.root / 'pcm', selected=selected)
        self.assertEqual(result['bytes'], sum(map(len, bodies.values())))
        self.assertEqual([Path(item['relative_path']).name for item in result['files']], list(bodies))
        for item in result['files']:
            self.assertEqual(item['sha256'], hashlib.sha256(bodies[Path(item['relative_path']).name]).hexdigest())
        first = self.root / 'pcm/service-input' / result['files'][0]['relative_path']
        first.write_bytes(b'tampered')
        with self.assertRaisesRegex(InputError, '哈希变化'):
            service.prepare(META, settings, self.root / 'pcm', selected=selected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
