"""Interrupted AD transfers must reuse only independently verified receipts."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile

import download_checkpoint as checkpoint
import native_log_source as native
from native_windows_host import filesystem_path
from request_model import InputError

META = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-30T17:00:00', 'end': '2026-10-30T17:35:00'}


class DownloadCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='wt-receipts-')).resolve()
        self.addCleanup(self.cleanup)
        self.directory = self.root / 'input'
        self.settings = {'local_analysis': {'download_workers': 1}, 'execution_release': 'synthetic-release-11'}
        self.body = {}
        self.selected = {name: [] for name in native.LOG_CLASSES}
        for index in range(3):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr(f'native_{index}.log', b'complete record\n' * (index + 1))
            name = f'source-{index}.zip'
            self.body[name] = output.getvalue()
            self.selected['log_fsdA'].append({'vin': META['vin'], 'fileKey': f'object-{index}',
                'fileName': name, 'collectTime': '2026-10-30 17:20:00', 'fileSize': len(self.body[name]),
                'downloadURL': f'https://download.invalid/{name}?signature=DO-NOT-PERSIST'})
        self.calls = []
        self.budget_observations = []
        self.call_lock = threading.Lock()

    def cleanup(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        shutil.rmtree(filesystem_path(self.root))

    def downloader(self, fail=None):
        def download(url, path, *, environment, max_bytes, byte_budget):
            name = urllib.parse.urlsplit(url).path.lstrip('/')
            with self.call_lock:
                self.calls.append(name)
                self.budget_observations.append(byte_budget.used)
            filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
            if name == fail:
                raise native.cloud.CloudNetworkError('synthetic interrupted transfer')
            byte_budget.consume(len(self.body[name]))
            filesystem_path(path).write_bytes(self.body[name])
            return len(self.body[name])
        return download

    def fetch(self, *, fail=None, selected=None, metadata=None, settings=None, directory=None, environment='prod'):
        with patch.object(native.cloud, 'download_to_file', side_effect=self.downloader(fail)):
            return native.fetch_window(metadata or META, settings or self.settings, directory or self.directory,
                                       environment=environment, selected=selected or self.selected)

    def interrupt(self):
        with self.assertRaises(native.cloud.CloudNetworkError):
            self.fetch(fail='source-1.zip')
        self.assertFalse((self.directory / 'download-manifest.json').exists())
        self.assertEqual(len(list((self.directory / checkpoint.RECEIPTS_NAME).glob('*.json'))), 1)

    def test_resume_reuses_verified_file_with_new_signature_and_charges_its_budget(self):
        self.interrupt()
        self.calls.clear(); self.budget_observations.clear()
        renewed = deepcopy(self.selected)
        for item in renewed['log_fsdA']:
            item['downloadURL'] += '-renewed'
        result = self.fetch(selected=renewed)
        self.assertEqual(self.calls, ['source-1.zip', 'source-2.zip'])
        self.assertEqual(self.budget_observations[0], len(self.body['source-0.zip']))
        self.assertEqual(result['bytes'], sum(map(len, self.body.values())))
        self.assertEqual(result['timings']['reused_files'], 1)
        self.assertEqual(len(result['files']), 3)
        text = (self.directory / checkpoint.PLAN_NAME).read_text('utf-8')
        self.assertNotIn('https://', text)
        self.assertNotIn('DO-NOT-PERSIST', text)
        self.assertNotIn('downloadURL', text)
        self.assertNotIn('userName', text)

    def test_verified_reuse_cannot_bypass_the_shared_total_budget(self):
        for item in self.selected['log_fsdA']:
            item['fileSize'] = None
        self.interrupt()
        self.calls.clear()
        with patch.object(native, 'MAX_BYTES', len(self.body['source-0.zip']) - 1):
            with self.assertRaises(InputError):
                self.fetch()
        self.assertEqual(self.calls, [])

    def test_existing_unplanned_directory_is_not_absorbed(self):
        self.directory.mkdir()
        (self.directory / 'legacy.zip').write_bytes(self.body['source-0.zip'])
        with self.assertRaises(InputError):
            self.fetch()
        self.assertEqual(self.calls, [])
        self.assertTrue((self.directory / 'legacy.zip').is_file())

    def test_object_order_identity_request_environment_and_release_drift_fail_closed(self):
        self.interrupt(); self.calls.clear()
        for field in ('order', 'fileKey', 'collectTime', 'fileSize', 'request', 'environment', 'release', 'profile'):
            selected = deepcopy(self.selected); settings = deepcopy(self.settings); metadata = deepcopy(META)
            environment = 'prod'
            if field == 'order':
                selected['log_fsdA'].reverse()
            elif field in ('fileKey', 'collectTime'):
                selected['log_fsdA'][0][field] = 'different-object'
            elif field == 'fileSize':
                selected['log_fsdA'][0][field] += 1
            elif field == 'request':
                metadata['end'] = '2026-10-30T17:36:00'
            elif field == 'environment':
                environment = 'test'
            elif field == 'release':
                settings['execution_release'] = 'other-release'
            else:
                settings['local_analysis']['execution'] = 'windows'
            with self.subTest(field=field), self.assertRaises(InputError):
                self.fetch(selected=selected, metadata=metadata, settings=settings, environment=environment)
            self.assertEqual(self.calls, [])

    def test_changed_missing_or_unreceipted_files_and_pollution_are_rejected(self):
        self.interrupt(); self.calls.clear()
        receipt = next((self.directory / checkpoint.RECEIPTS_NAME).glob('*.json'))
        saved_receipt = receipt.read_bytes()
        target = self.directory / 'log_fsdA/item-0001/source-0.zip'
        original = target.read_bytes()
        target.write_bytes(original[:-1])
        with self.assertRaises((InputError, OSError)):
            self.fetch()
        target.write_bytes(original)
        receipt.unlink()
        with self.assertRaises(InputError):
            self.fetch()
        receipt.write_bytes(saved_receipt)
        foreign = self.directory / 'extra-file.log'
        foreign.write_text('do not absorb')
        with self.assertRaises(InputError):
            self.fetch()
        foreign.unlink()
        target.unlink()
        with self.assertRaises((InputError, OSError)):
            self.fetch()
        self.assertEqual(self.calls, [])

    def test_hash_matched_receipt_does_not_bypass_zip_crc(self):
        self.interrupt(); self.calls.clear()
        target = self.directory / 'log_fsdA/item-0001/source-0.zip'
        payload = bytearray(target.read_bytes())
        offset = payload.index(b'complete record')
        payload[offset] ^= 1
        target.write_bytes(payload)
        receipt_path = next((self.directory / checkpoint.RECEIPTS_NAME).glob('*.json'))
        receipt = json.loads(receipt_path.read_text('utf-8'))
        receipt['file']['sha256'] = hashlib.sha256(payload).hexdigest()
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        with self.assertRaisesRegex(InputError, 'ZIP'):
            self.fetch()
        self.assertEqual(self.calls, [])

    def test_receipt_commits_are_atomic_under_concurrent_transfers(self):
        self.settings['local_analysis']['download_workers'] = 4
        self.selected['log_fsdB'] = deepcopy(self.selected['log_fsdA'])
        for item in self.selected['log_fsdB']:
            item['fileKey'] += '-fsdb'
        result = self.fetch()
        self.assertEqual(len(result['files']), 6)
        self.assertEqual(len(list((self.directory / checkpoint.RECEIPTS_NAME).glob('*.json'))), 6)
        self.assertEqual(list(self.directory.rglob('*.tmp')), [])
        self.calls.clear()
        repeated = self.fetch()
        self.assertEqual(repeated, result)
        self.assertEqual(self.calls, [])

    def test_reparse_root_is_rejected_before_any_download(self):
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if str(path).removeprefix('\\\\?\\') == str(self.directory):
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', lstat), self.assertRaises(InputError):
            self.fetch()
        self.assertEqual(self.calls, [])

    def test_windows_long_path_receipt_reuse_keeps_original_filename(self):
        if os.name != 'nt':
            self.skipTest('Windows extended path I/O')
        long_name = 'archive-' + 'x' * 115 + '.zip'
        first = self.selected['log_fsdA'][0]
        self.body[long_name] = self.body.pop('source-0.zip')
        first.update(fileName=long_name, downloadURL='https://download.invalid/' + long_name)
        self.directory = self.root / ('deep-' + 'd' * 90) / 'input'
        with self.assertRaises(native.cloud.CloudNetworkError):
            self.fetch(fail='source-1.zip')
        self.calls.clear()
        result = self.fetch()
        self.assertEqual(self.calls, ['source-1.zip', 'source-2.zip'])
        copied = self.directory / result['files'][0]['relative_path']
        self.assertGreater(len(str(copied)), 260)
        self.assertEqual(copied.name, long_name)


if __name__ == '__main__':
    unittest.main()
