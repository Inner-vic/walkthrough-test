"""Real Windows regression for the failed 262-character private input copy."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zstandard

import native_inputs
import native_windows_host as host
import run_m100 as runner


@unittest.skipUnless(os.name == 'nt', 'Windows filesystem regression')
class WindowsLongInputPaths(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='wtr089-long-')).resolve()
        self.addCleanup(self.cleanup)
        self.logs = self.root / 'source'
        self.logs.mkdir()

    def cleanup(self):
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        shutil.rmtree(host.filesystem_path(self.root))

    def long_archive(self):
        name = ('backtrace.safety_fusion_container.264741.t_FsdFus_fusion.264790.'
                '2026-10-21.15_32_27.804.signal_6.log.zip')
        folder = self.logs / 'log_fsdB' / ('nested-' + 'x' * 135)
        host.filesystem_path(folder).mkdir(parents=True)
        archive = folder / name
        with zipfile.ZipFile(host.filesystem_path(archive), 'w') as stream:
            stream.writestr('native_2026_10_21_15_01_00.log', b'synthetic full content\n')
        self.assertGreaterEqual(len(str(archive)), 262)
        return archive

    def test_real_long_archive_enumeration_and_post_copy_inventory(self):
        source = self.long_archive()
        expected = runner.validate_log_inventory([source], input_root=self.logs, windows=True)
        private = self.root / 'private-input'
        destination = private / source.relative_to(self.logs)
        host.filesystem_path(destination.parent).mkdir(parents=True)
        shutil.copy2(host.filesystem_path(source), host.filesystem_path(destination))
        actual_files = list(runner.inputs(private))
        self.assertEqual(actual_files, [destination])
        self.assertFalse(str(actual_files[0]).startswith('\\\\?\\'))
        self.assertEqual(runner.validate_log_inventory(actual_files, input_root=private, windows=True), expected)
        self.assertEqual(host.sha(source), host.sha(destination))

    def test_execute_reaches_worker_after_exact_copy_and_inventory_checks(self):
        source = self.long_archive()
        inventory = runner.validate_log_inventory([source], input_root=self.logs, windows=True)
        analyzer = self.root / 'analyzer'
        (analyzer / 'common').mkdir(parents=True)
        (analyzer / 'general_performance').mkdir()
        (analyzer / 'general_performance/config.py').write_text('# fixture\n')
        (analyzer / 'general_performance/o.ini').write_text('[PARAMETER]\nupload=0\n')
        rules = self.root / 'rules.json'
        rules.write_text('{}')
        result = {'time_range': '20261021145000/20261021152400', 'input_inventory': inventory,
                  'analyzer_contract': {'contract_fingerprint': 'fixture'}}
        def reached_worker(argv, **kwargs):
            self.assertTrue(all('\\\\?\\' not in str(value) for value in argv))
            local = Path(argv[argv.index('--log-dir') + 1])
            copied = local / source.relative_to(self.logs)
            self.assertEqual(host.sha(copied), host.sha(source))
            raise RuntimeError('verified worker boundary')
        with patch('analyzer_contract.validate_analyzer_contract', return_value={'contract_fingerprint': 'fixture'}), \
             patch.object(runner, 'slim_platform_config'), \
             patch('native_windows_process.run_windows_tree', side_effect=reached_worker) as invoke:
            with self.assertRaisesRegex(RuntimeError, 'verified worker boundary'):
                runner.execute(analyzer, self.logs, self.root / 'work', result, [source], 10,
                               'DEM0VEH1CLE000001', rules, native_execution='windows')
        self.assertEqual(invoke.call_count, 1)

    def test_tampered_private_copy_still_fails_inventory_guard(self):
        source = self.long_archive()
        inventory = runner.validate_log_inventory([source], input_root=self.logs, windows=True)
        private = self.root / 'private'
        destination = private / source.relative_to(self.logs)
        host.filesystem_path(destination.parent).mkdir(parents=True)
        with zipfile.ZipFile(host.filesystem_path(destination), 'w') as stream:
            stream.writestr('native_2026_10_21_15_01_00.log', b'changed\n')
        self.assertNotEqual(runner.validate_log_inventory(list(runner.inputs(private)),
                            input_root=private, windows=True), inventory)

    def test_long_normalized_log_and_cache_are_read_without_renaming(self):
        relative = Path('log_xcu_mcu') / ('item-' + 'x' * 175) / 'XVM_MCU-synthetic.zst'
        source = self.logs / relative
        host.filesystem_path(source.parent).mkdir(parents=True)
        host.filesystem_path(source).write_bytes(zstandard.ZstdCompressor().compress(b'full line\n'))
        manifest = {'files': [{'relative_path': relative.as_posix(), 'sha256': host.sha(source),
                    'bytes': host.filesystem_path(source).stat().st_size, 'log_class': 'log_xcu_mcu'}]}
        destination = self.root / 'normalized'
        target = destination / relative.with_suffix('.log')
        self.assertGreater(len(str(target)), 260)
        first = native_inputs.prepare_native_inputs(self.logs, destination, manifest)
        self.assertEqual(host.filesystem_path(target).read_bytes(), b'full line\n')
        self.assertEqual(first['files'][0]['relative_path'], relative.with_suffix('.log').as_posix())
        self.assertEqual(native_inputs.prepare_native_inputs(self.logs, destination, manifest), first)

    def test_io_prefix_never_accepts_unc_relative_or_traversal(self):
        for value in ('relative.log', r'C:relative.log', r'\\server\share\x.log',
                      str(self.root / '..' / 'escape.log')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host.filesystem_path(value)


if __name__ == '__main__':
    unittest.main()
