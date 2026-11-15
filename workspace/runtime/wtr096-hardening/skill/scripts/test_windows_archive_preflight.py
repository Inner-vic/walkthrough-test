"""Synthetic ZIP/TAR admission checks; no extraction, credentials or network."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

import run_m100 as runner


class Selection:
    fingerprint = 'synthetic-request-selection'

    def __init__(self, excluded=()):
        self.excluded = set(excluded)

    def archive_selected(self, path):
        return path.name not in self.excluded

    def selected_names(self, names, source_by_basename=None):
        return set(names)


class WindowsArchivePreflightTests(unittest.TestCase):
    def setUp(self):
        # No mode0700 TemporaryDirectory: Windows sandbox may deny its children.
        self.root = Path(__file__).resolve().parent / ('archive-fixture-' + uuid.uuid4().hex[:12])
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.logs = self.root / 'input'
        self.source = self.logs / 'log_fsdA'
        self.source.mkdir(parents=True)

    def cleanup(self):
        assert self.root.resolve().parent == Path(__file__).resolve().parent
        shutil.rmtree(self.root)

    def archive(self, fmt, members, name=None):
        path = self.source / (name or ('native_2026_10_23_15_20_00.' + fmt))
        if fmt == 'zip':
            with zipfile.ZipFile(path, 'w') as archive:
                for name, data in members:
                    archive.writestr(name, data)
        else:
            with tarfile.open(path, 'w:') as archive:
                for name, data in members:
                    member = tarfile.TarInfo(name)
                    if name.endswith('/'):
                        member.type = tarfile.DIRTYPE
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        return path

    def inventory(self, paths, *, windows=True, selection=None):
        return runner.validate_log_inventory(paths, selection, self.logs, windows=windows)

    def rejected(self, fmt, name):
        path = self.archive(fmt, [('safe.log', b'synthetic\n'), (name, b'synthetic bad\n')])
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            self.inventory([path])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.source.iterdir()), [path], 'admission must not extract or rename')

    def test_safe_zip_and_tar_keep_exact_inventory_and_original_bytes(self):
        for fmt in ('zip', 'tar'):
            path = self.archive(fmt, [('dir with space/日志.log', b'line one\nline two\n')])
            before = path.read_bytes()
            self.assertEqual(self.inventory([path]), self.inventory([path], windows=False))
            self.assertEqual(path.read_bytes(), before)

    def test_ads_colon_and_illegal_characters_are_rejected_in_zip_and_tar(self):
        for fmt in ('zip', 'tar'):
            for name in ('log:stream.log', 'dir:stream/safe.log', 'bad?.log', 'bad*.log', 'bad|.log', 'bad\x01.log'):
                with self.subTest(fmt=fmt, name=name):
                    self.rejected(fmt, name)
                    next(self.source.iterdir()).unlink()

    def test_reserved_device_names_in_any_component_are_rejected(self):
        for fmt in ('zip', 'tar'):
            for name in ('CON.log', 'prn.txt', 'COM1.log', 'LPT9/x.log', 'COM¹.log', 'NUL .log', 'CONIN$.log'):
                with self.subTest(fmt=fmt, name=name):
                    self.rejected(fmt, name)
                    next(self.source.iterdir()).unlink()

    def test_trailing_dot_and_space_are_rejected(self):
        for fmt in ('zip', 'tar'):
            for name in ('bad.log.', 'bad.log ', 'dir./safe.log'):
                with self.subTest(fmt=fmt, name=name):
                    self.rejected(fmt, name)
                    next(self.source.iterdir()).unlink()

    def test_casefold_file_alias_is_rejected(self):
        for fmt in ('zip', 'tar'):
            path = self.archive(fmt, [('same.log', b'one'), ('SAME.LOG', b'two')])
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                self.inventory([path])

    def test_generated_dos_short_file_and_directory_aliases_are_rejected(self):
        for fmt in ('zip', 'tar'):
            for pair in ([('longfilename.log', b'one'), ('LONGFI~1.log', b'two')],
                         [('LongParentDirectory/safe.log', b'one'), ('LONGPA~1/safe.log', b'two')]):
                path = self.archive(fmt, pair)
                with self.subTest(fmt=fmt, pair=pair), self.assertRaisesRegex(ValueError, '8.3'):
                    self.inventory([path])

    def test_regular_tilde_long_name_is_not_arbitrarily_rejected(self):
        for fmt in ('zip', 'tar'):
            path = self.archive(fmt, [('native~long_name_2026.log', b'long real name')])
            self.assertEqual(self.inventory([path]), self.inventory([path], windows=False))

    def test_explicit_directory_file_alias_in_both_orders_is_rejected(self):
        for fmt in ('zip', 'tar'):
            for pair in ([('alias/', b''), ('ALIAS', b'file')], [('ALIAS', b'file'), ('alias/', b'')]):
                path = self.archive(fmt, [('safe.log', b'safe'), *pair])
                with self.subTest(fmt=fmt, pair=pair), self.assertRaisesRegex(ValueError, '别名'):
                    self.inventory([path])

    def test_implicit_parent_file_alias_in_both_orders_is_rejected(self):
        for fmt in ('zip', 'tar'):
            for pair in ([('parent', b'file'), ('PARENT/child.log', b'log')],
                         [('PARENT/child.log', b'log'), ('parent', b'file')]):
                path = self.archive(fmt, pair)
                with self.subTest(fmt=fmt, pair=pair), self.assertRaisesRegex(ValueError, '别名'):
                    self.inventory([path])

    def test_identical_explicit_directory_repeat_does_not_add_statistics_input(self):
        for fmt in ('zip', 'tar'):
            path = self.archive(fmt, [('dir/', b''), ('dir/', b''), ('dir/safe.log', b'log')])
            self.assertEqual(self.inventory([path]), self.inventory([path], windows=False))

    def test_unsafe_unknown_nonlog_is_not_silently_ignored(self):
        for fmt in ('zip', 'tar'):
            self.rejected(fmt, 'metadata:unknown.txt')
            next(self.source.iterdir()).unlink()

    def test_eligible_unsafe_core_is_not_implicitly_discarded(self):
        for fmt in ('zip', 'tar'):
            self.rejected(fmt, 'core.t:synthetic.123')
            next(self.source.iterdir()).unlink()

    def test_original_archive_predicate_exclusion_preserves_unsafe_core_without_extracting(self):
        for fmt in ('zip', 'tar'):
            safe = self.archive(fmt, [('safe.log', b'safe')])
            core = self.archive(fmt, [('core.t:synthetic.123', b'synthetic core')], 'core.no-time.' + fmt)
            files = [safe, core]
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
            files_on_disk = set(self.source.iterdir())
            selection = Selection(excluded=[core.name])
            self.assertEqual(self.inventory(files, selection=selection),
                             self.inventory(files, windows=False, selection=selection))
            self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}, before)
            self.assertEqual(set(self.source.iterdir()), files_on_disk)

    def test_ineligible_windows_unsafe_log_does_not_become_selected(self):
        for fmt in ('zip', 'tar'):
            safe = self.archive(fmt, [('safe.log', b'safe')])
            excluded = self.archive(fmt, [('bad:log.log', b'outside')], 'outside.' + fmt)
            selection = Selection(excluded=[excluded.name])
            actual = self.inventory([safe, excluded], selection=selection)
            self.assertEqual(actual, self.inventory([safe, excluded], windows=False, selection=selection))
            self.assertNotIn('bad:log.log', actual['selected_members'])

    def test_no_arbitrary_total_path_length_cutoff(self):
        member = '/'.join(['segment' * 8] * 6) + '/safe.log'
        self.assertGreater(len(member), 260)
        for fmt in ('zip', 'tar'):
            path = self.archive(fmt, [(member, b'legal components')])
            self.assertEqual(self.inventory([path]), self.inventory([path], windows=False))

    def test_intrinsic_component_utf16_limit_is_checked(self):
        for fmt in ('zip', 'tar'):
            self.rejected(fmt, 'a' * 252 + '.log')
            next(self.source.iterdir()).unlink()
            self.rejected(fmt, '😀' * 126 + '.log')
            next(self.source.iterdir()).unlink()

    def test_absolute_or_traversal_paths_remain_rejected_in_both_modes(self):
        for fmt in ('zip', 'tar'):
            for name in ('../outside.log', '/absolute.log', 'C:/drive.log'):
                path = self.archive(fmt, [(name, b'bad')])
                for windows in (False, True):
                    with self.subTest(fmt=fmt, name=name, windows=windows), self.assertRaises(ValueError):
                        self.inventory([path], windows=windows)

    def test_windows_missing_full_rules_fails_before_source_or_file_access(self):
        source = self.archive('zip', [('safe.log', b'safe')])
        rules = self.root / 'existing-rules.json'
        rules.write_text('{"do_not_modify":true}', encoding='utf-8')
        before = {p: p.read_bytes() for p in (source, rules)}
        with patch('analyzer_contract.validate_analyzer_contract', side_effect=AssertionError('must fail before source read')):
            with self.assertRaisesRegex(ValueError, '完整规则快照'):
                runner.preflight(self.root / 'not-a-source', self.logs, '20261023150000', '20261023153500',
                                 'm100-ultra', native_execution='windows')
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_cli_passes_explicit_execution_to_preflight(self):
        args = ['run_m100.py', '--analyzer-root', str(self.root), '--log-dir', str(self.logs),
                '--start', '20261023150000', '--end', '20261023153500', '--native-execution', 'windows',
                '--rules-json', str(self.root / 'rules.json')]
        with patch.object(runner.sys, 'argv', args), \
             patch.object(runner, 'preflight', return_value=({'status': 'preflight_ok'}, [])) as preflight, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(preflight.call_args.kwargs['native_execution'], 'windows')

    @unittest.skipUnless(os.name == 'nt', 'Windows execution branch')
    def test_execute_rechecks_windows_gate_after_copy_before_starting_worker(self):
        path = self.archive('zip', [('bad:input.log', b'synthetic')])
        inventory = self.inventory([path], windows=False)
        analyzer = self.root / 'analyzer'
        (analyzer / 'common').mkdir(parents=True)
        (analyzer / 'general_performance').mkdir()
        (analyzer / 'general_performance/o.ini').write_text('[PARAMETER]\nupload=0\n', encoding='utf-8')
        (analyzer / 'general_performance/config.py').write_text('# synthetic\n', encoding='utf-8')
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        result = {'time_range': '20261023150000/20261023153500', 'input_inventory': inventory,
                  'analyzer_contract': {'contract_fingerprint': 'synthetic'}}
        with patch('analyzer_contract.validate_analyzer_contract', return_value={'contract_fingerprint': 'synthetic'}), \
             patch.object(runner, 'slim_platform_config'), \
             patch('native_windows_process.run_windows_tree', side_effect=AssertionError('unsafe worker must not start')):
            with self.assertRaisesRegex(RuntimeError, 'Windows 归档成员'):
                runner.execute(analyzer, self.logs, self.root / 'work', result, [path], 10,
                               'DEM0VEH1CLE000001', rules, native_execution='windows')
        self.assertEqual(path.read_bytes(), (next((self.root / 'work').iterdir()) / 'input/log_fsdA' / path.name).read_bytes())


if __name__ == '__main__':
    unittest.main()
