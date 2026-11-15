"""Real filesystem input-boundary tests; no cloud or publication."""
import ast
import copy
from datetime import datetime, timedelta, timezone
import inspect
import linecache
from pathlib import Path
import sys
import shutil
import tempfile
import textwrap
import types
import unittest
from unittest import mock

import analyzer_contract
import source_catalog
import source_namespace as namespace
from native_windows_host import filesystem_path

WINDOW = '20261102172900/20261102180400'

FIXTURE = '''
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

def _parse_cn_dt_from_log_path(path):
    matches = re.findall(r'(\\d{4}_\\d{2}_\\d{2}_\\d{2}_\\d{2}_\\d{2})(?=\\D|$)', path.name)
    return datetime.strptime(matches[-1], '%Y_%m_%d_%H_%M_%S').replace(tzinfo=timezone(timedelta(hours=8))) if matches else None

def should_keep_log(path, epochs, state):
    stamp = _parse_cn_dt_from_log_path(path)
    if stamp is None:
        return True
    seconds = stamp.timestamp()
    if seconds < epochs[0]:
        return False
    if seconds <= epochs[1]:
        return True
    if state['kept_first_after_end']:
        return False
    state['kept_first_after_end'] = True
    return True

class RecordPerformance:
    def __init__(self, log_path, theme_lists, time_range, path_config, config_date):
        self.log_path = log_path
        self.time_range = time_range
        self.path_config = path_config
        self.config_date = config_date

    def _resolve_log_dir_name(self, theme, soc_type):
        return theme if isinstance(soc_type, list) else soc_type

    def extract_logs(self, theme, soc_type):
        return []
'''


class _Fixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.input = self.folder / 'input'
        self.input.mkdir()
        self.flat = self.folder / 'staged' / 'perf'
        self.rules = {'first': {'metric': {'soc_type': 'M'}}, 'second': {'metric2': {'soc_type': 'M'}}}
        self.config = {'M': ['mach_']}
        self.shared = 'mach_shared_2026_11_02_17_47_52.log'
        self.rows = [
            ('log_fsdA', self.shared, b'A distinct event stream\n'),
            ('log_fsdB', self.shared, b'B distinct event stream\n'),
            ('log_fsdA', 'mach_A_first_2026_11_02_18_05_00.log', b'A first boundary\n'),
            ('log_fsdA', 'mach_A_tie_2026_11_02_18_05_00.log', b'A tied but not selected\n'),
            ('log_fsdB', 'mach_B_first_2026_11_02_18_06_00.log', b'B first boundary\n'),
            ('log_fsdA', 'mach_A_later_2026_11_02_18_07_00.log', b'A later excluded\n'),
            ('log_fsdA', 'vin_metadata.log', b'metadata retained by collector\n'),
            ('log_fsdA', 'mach_old_2026_11_02_16_00_00.log', b'old excluded\n')]
        self.module = self.make_module()
        module = self.module
        class Selection:
            fingerprint = 'a' * 64
            time_range = WINDOW
            epochs = tuple(datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
                           for value in WINDOW.split('/'))
            prefixes = ['mach_']
            archive_selected = staticmethod(lambda path: True)
            parse_time = staticmethod(module._parse_cn_dt_from_log_path)
            keep_log = staticmethod(module.should_keep_log)
        self.selection = Selection()

    def make_module(self, source=FIXTURE):
        name = 'source_namespace_fixture_' + str(id(self)) + '_' + str(len(sys.modules))
        module = types.ModuleType(name)
        module.__file__ = str(self.folder / (name + '.py'))
        linecache.cache[module.__file__] = (len(source), None, source.splitlines(keepends=True), module.__file__)
        self.addCleanup(linecache.cache.pop, module.__file__, None)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
        original = inspect.getsource(module.RecordPerformance.extract_logs)
        digest = analyzer_contract.ast_digest(ast.parse(textwrap.dedent(original)))
        approval = mock.patch.object(analyzer_contract, 'RUNTIME_EXTRACT_LOGS_AST_SHA256', digest)
        approval.start()
        self.addCleanup(approval.stop)
        return module

    def stage(self, rows=None):
        paths = []
        for index, (source, name, content) in enumerate(self.rows if rows is None else rows):
            path = self.input / source / ('item-' + str(index)) / name
            filesystem_path(path.parent).mkdir(parents=True, exist_ok=True)
            filesystem_path(path).write_bytes(content)
            paths.append(path)
        self.catalog = source_catalog.build_catalog(paths, self.selection, self.input, windows=sys.platform == 'win32')
        self.receipt = source_catalog.materialize_catalog(self.catalog, self.input, self.flat)
        self.audit = namespace.install_source_namespace_patch(self.module, self.catalog)
        return self.catalog

    def record(self):
        return self.module.RecordPerformance(str(self.flat), list(self.rules), self.selection.epochs, self.config, self.rules)


class NamespaceTests(_Fixture, unittest.TestCase):
    def test_cross_source_same_name_retains_both_original_files_in_one_result(self):
        self.stage()
        result = self.record().extract_logs('first', 'M')
        self.assertEqual(len(result), 4)
        shared = [Path(value) for value in result if Path(value).name == self.shared]
        self.assertEqual(len(shared), 2)
        self.assertEqual({filesystem_path(path).read_bytes() for path in shared},
                         {b'A distinct event stream\n', b'B distinct event stream\n'})
        self.assertEqual(len({path.parent.name for path in shared}), 2)
        self.assertTrue(all(path.parent.parent == self.flat / 'log' / 'M' for path in shared))

    def test_first_after_end_tie_keeps_one_per_source_and_late_files_stay_out(self):
        self.stage()
        names = [Path(path).name for path in self.record().extract_logs('first', 'M')]
        self.assertIn('mach_A_first_2026_11_02_18_05_00.log', names)
        self.assertIn('mach_B_first_2026_11_02_18_06_00.log', names)
        self.assertNotIn('mach_A_tie_2026_11_02_18_05_00.log', names)
        self.assertNotIn('mach_A_later_2026_11_02_18_07_00.log', names)
        self.assertNotIn('mach_old_2026_11_02_16_00_00.log', names)
        self.assertNotIn('vin_metadata.log', names)

    def test_repeated_scope_revalidates_original_names_and_contents(self):
        self.stage()
        record = self.record()
        first = record.extract_logs('first', 'M')
        self.assertEqual(record.extract_logs('second', 'M'), first)
        filesystem_path(Path(first[0])).write_bytes(b'corrupt cached theme member')
        with self.assertRaisesRegex(ValueError, '缓存|输入'):
            record.extract_logs('second', 'M')

    def test_extra_theme_member_cannot_join_native_statistics(self):
        self.stage()
        record = self.record()
        record.extract_logs('first', 'M')
        (self.flat / 'log' / 'M' / 'extra.log').write_bytes(b'unknown')
        with self.assertRaisesRegex(ValueError, '未审计'):
            record.extract_logs('first', 'M')

    def test_catalog_input_tamper_fails_before_return(self):
        self.stage()
        member = self.catalog['members'][self.catalog['selected_member_ids'][0]]
        filesystem_path(self.flat / member['namespace_path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '内容变化'):
            self.record().extract_logs('first', 'M')

    def test_cross_source_identical_content_keeps_two_logical_inputs(self):
        self.stage([('log_fsdA', self.shared, b'same\n'), ('log_fsdB', self.shared, b'same\n')])
        result = self.record().extract_logs('first', 'M')
        self.assertEqual(len(result), 2)
        self.assertEqual(len({Path(path).parent.name for path in result}), 2)

    def test_multi_alias_overlapping_prefixes_union_does_not_duplicate_inputs(self):
        self.stage()
        record = self.record()
        record.path_config = {'one': ['mach_'], 'two': ['mach_']}
        values = record.extract_logs('combined', ['one', 'two'])
        self.assertEqual(len(values), 4)
        self.assertEqual(len(values), len(set(values)))

    def test_changed_original_ast_is_rejected_without_patching_class(self):
        self.stage()
        other = self.make_module()
        original = other.RecordPerformance.extract_logs
        with mock.patch.object(analyzer_contract, 'RUNTIME_EXTRACT_LOGS_AST_SHA256', '0' * 64):
            with self.assertRaisesRegex(ValueError, '完整 AST'):
                namespace.install_source_namespace_patch(other, self.catalog)
        self.assertIs(other.RecordPerformance.extract_logs, original)

    def test_selection_audit_rejects_code_and_catalog_rebinding(self):
        self.stage()
        namespace.validate_selection_audit(self.audit)
        for key in ('adapter_module_sha256', 'input_inventory_sha256', 'original_extract_logs_ast_sha256'):
            forged = copy.deepcopy(self.audit)
            forged[key] = '0' * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                namespace.validate_selection_audit(forged)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows long-path I/O boundary')
    def test_windows_long_path_preserves_original_basename(self):
        self.flat = self.folder / ('p' * 100) / ('q' * 100) / 'perf'
        cleanup_root = self.folder / ('p' * 100)
        def cleanup():
            if cleanup_root.absolute().parent != self.folder:
                raise ValueError('Test cleanup escaped its own temporary directory')
            shutil.rmtree(filesystem_path(cleanup_root))
        self.addCleanup(cleanup)
        self.stage()
        result = self.record().extract_logs('first', 'M')
        self.assertTrue(any(len(path) > 260 for path in result))
        self.assertEqual(len(result), 4)
        for path in result:
            self.assertTrue(filesystem_path(Path(path)).is_file())


if __name__ == '__main__':
    unittest.main()
