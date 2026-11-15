"""Source-isolation and structural guards; no cloud or real vehicle fixtures."""
import ast
import copy
import inspect
import linecache
from pathlib import Path
import sys
import textwrap
import types
import unittest
from unittest import mock

import native_selection as selection
from test_source_selection_fixture import synthetic_selection_audit as selection_audit_fixture


FIXTURE = '''
from pathlib import Path as RealPath
import fnmatch

class InputRoot:
    def __init__(self, paths):
        self.paths = paths
    def glob(self, pattern):
        return [path for path in self.paths if fnmatch.fnmatchcase(path.name, pattern)]

def Path(value):
    return value if isinstance(value, InputRoot) else RealPath(value)

def should_keep_log(log_path, time_range, state):
    stamp = int(Path(log_path).stem.rsplit('_', 1)[1])
    if stamp < time_range[0]:
        return False
    if stamp > time_range[1]:
        if state.get("kept_first_after_end", False):
            return False
        state["kept_first_after_end"] = True
    return True

class RecordPerformance:
    def __init__(self, paths, time_range=(10, 20)):
        self.log_path = InputRoot(paths)
        self.time_range = time_range

    def extract_logs(self, theme, soc_type):
        """Synthetic collector documentation.
            Keep nested indentation to exercise inspect/dedent differences.
        """
        selected = []
        valid_filenames = soc_type
        for file_name in valid_filenames:
            state = {"kept_first_after_end": False}
            log_paths_list = list(Path(self.log_path).glob(f"{file_name}*.log"))
            log_paths_list.sort(key=lambda path: int(path.stem.rsplit('_', 1)[1]))
            for log_path in log_paths_list:
                if not should_keep_log(log_path, self.time_range, state):
                    continue
                selected.append(log_path.name)
        return selected
'''


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(__file__).resolve().parent / ('analyzer_fixture_' + str(id(self)) + '.py'))
        # Tests explicitly approve only this synthetic complete method. No
        # production option can bypass or learn an analyzer contract.
        method = textwrap.dedent(FIXTURE[FIXTURE.index('    def extract_logs'):])
        digest = selection.ast_digest(ast.parse(method))
        contract = mock.patch('analyzer_contract.RUNTIME_EXTRACT_LOGS_AST_SHA256', digest)
        contract.start()
        self.addCleanup(contract.stop)

    def module(self, source=FIXTURE):
        name = 'source_boundary_fixture_' + str(id(self))
        module = types.ModuleType(name)
        module.__file__ = self.path
        linecache.cache[self.path] = (len(source), None, source.splitlines(keepends=True), self.path)
        self.addCleanup(linecache.cache.pop, self.path, None)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        exec(compile(source, self.path, 'exec'), module.__dict__)
        return module

    def test_boundary_is_independent_per_source(self):
        module = self.module()
        paths = [Path(name) for name in ('vla_before_09.log', 'vla_inside_15.log',
                                        'vla_A_21.log', 'vla_B_25.log', 'vla_A_30.log', 'vla_B_31.log')]
        recorder = module.RecordPerformance(paths)
        self.assertEqual(recorder.extract_logs('x', ['vla_']), ['vla_inside_15.log', 'vla_A_21.log'])
        mapping = {path.name: ('log_fsdB' if '_B_' in path.name else 'log_fsdA') for path in paths}
        audit = selection.install_source_boundary_patch(module, mapping)
        self.assertEqual(recorder.extract_logs('x', ['vla_']),
                         ['vla_inside_15.log', 'vla_A_21.log', 'vla_B_25.log'])
        self.assertEqual(audit['policy'], selection.POLICY)
        self.assertEqual(audit['source_count'], 2)
        self.assertEqual(audit['ast_replacements'], 3)
        self.assertNotEqual(audit['original_extract_logs_ast_sha256'], audit['patched_extract_logs_ast_sha256'])
        self.assertEqual(''.join(linecache.cache[self.path][2]), FIXTURE)

    def test_prefix_state_still_resets(self):
        module = self.module()
        paths = [Path(name) for name in ('vla_A_21.log', 'parking_A_22.log')]
        selection.install_source_boundary_patch(module, {path.name: 'log_fsdA' for path in paths})
        self.assertEqual(module.RecordPerformance(paths).extract_logs('x', ['vla_', 'parking_']),
                         ['vla_A_21.log', 'parking_A_22.log'])

    def test_legacy_explicit_identity_preserves_original_result(self):
        module = self.module()
        paths = [Path(name) for name in ('vla_A_21.log', 'vla_B_25.log')]
        recorder = module.RecordPerformance(paths)
        before = recorder.extract_logs('x', ['vla_'])
        audit = selection.install_source_boundary_patch(module, {path.name: 'legacy' for path in paths})
        self.assertEqual(before, recorder.extract_logs('x', ['vla_']))
        self.assertTrue(audit['legacy_source_used'])

    def test_unknown_selected_source_is_not_legacy_fallback(self):
        module = self.module()
        selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'})
        with self.assertRaisesRegex(ValueError, '未审计来源'):
            module.RecordPerformance([Path('vla_B_25.log')]).extract_logs('x', ['vla_'])

    def test_no_second_patch(self):
        module = self.module()
        mapping = {'vla_A_21.log': 'log_fsdA'}
        selection.install_source_boundary_patch(module, mapping)
        with self.assertRaisesRegex(ValueError, '补丁已安装'):
            selection.install_source_boundary_patch(module, mapping)

    def test_failed_guard_does_not_mutate_class_or_globals(self):
        module = self.module(FIXTURE.replace('self.time_range, state', 'self.time_range, {}'))
        original = module.RecordPerformance.extract_logs
        with self.assertRaisesRegex(ValueError, '完整 AST 已变化'):
            selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'})
        self.assertIs(module.RecordPerformance.extract_logs, original)
        self.assertNotIn(selection._RESOLVER, module.__dict__)
        self.assertNotIn(selection._FILTER, module.__dict__)

    def test_guards_reject_changed_state_scope_or_reuse(self):
        original = textwrap.dedent(FIXTURE[FIXTURE.index('    def extract_logs'):])
        mutations = [
            original.replace('kept_first_after_end": False', 'kept_first_after_end": True'),
            original.replace('for file_name in valid_filenames:', 'for file_name in list(valid_filenames):'),
            original.replace('selected.append(log_path.name)', 'selected.append(state)'),
            original.replace('self.time_range, state', 'self.time_range, state.copy()'),
            original.replace('soc_type):', 'soc_type, extra=None):'),
            original.replace('list(Path(self.log_path).glob(f"{file_name}*.log"))',
                             'sorted(Path(self.log_path).glob(f"{file_name}*.log"))'),
            original.replace('Path(self.log_path).glob', 'Path(self.other_path).glob'),
        ]
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                selection._patch_ast(changed)

    def test_full_method_guard_rejects_reverse_order_and_predicate_bypass(self):
        method = textwrap.dedent(FIXTURE[FIXTURE.index('    def extract_logs'):])
        variants = [
            method.replace("int(path.stem.rsplit('_', 1)[1]))", "int(path.stem.rsplit('_', 1)[1]), reverse=True)"),
            method.replace('if not should_keep_log(', 'if False and not should_keep_log('),
            method.replace('selected.append(log_path.name)', 'selected.append("different")'),
        ]
        for source in variants:
            self.assertNotEqual(source, method)
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, '完整 AST 已变化'):
                selection._patch_ast(source)

    def test_full_method_guard_allows_only_comments_and_formatting(self):
        method = textwrap.dedent(FIXTURE[FIXTURE.index('    def extract_logs'):])
        before, _ = selection._patch_ast(method)
        after, _ = selection._patch_ast('# test comment\n' + method.replace('\n', '\r\n'))
        self.assertEqual(ast.dump(before, include_attributes=False), ast.dump(after, include_attributes=False))

    def test_class_inspect_dedent_docstring_representation_is_explicitly_approved(self):
        module = self.module()
        original = module.RecordPerformance.extract_logs
        raw = inspect.getsource(original)
        runtime_tree = ast.parse(textwrap.dedent(raw))
        cls = next(node for node in ast.parse(FIXTURE).body
                   if isinstance(node, ast.ClassDef) and node.name == 'RecordPerformance')
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'extract_logs')
        static_tree = ast.Module(body=[copy.deepcopy(method)], type_ignores=[])
        self.assertNotEqual(selection.ast_digest(static_tree), selection.ast_digest(runtime_tree))
        proof = copy.deepcopy(static_tree)
        proof.body[0].body[0].value.value = runtime_tree.body[0].body[0].value.value
        self.assertEqual(selection.canonical_ast_dump(proof), selection.canonical_ast_dump(runtime_tree))
        paths = [Path('vla_A_21.log'), Path('vla_B_25.log')]
        selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA', 'vla_B_25.log': 'log_fsdB'})
        self.assertEqual(module.RecordPerformance(paths).extract_logs('x', ['vla_']),
                         ['vla_A_21.log', 'vla_B_25.log'])

    def test_mapping_rejects_ambiguous_or_invalid_names_and_identities(self):
        for mapping in ({}, [], {'../vla.log': 'log_fsdA'}, {'a\\b.log': 'log_fsdA'},
                        {'a.log': 'untrusted'}, {'a.log': 'log_fsdA', 'A.log': 'log_fsdB'},
                        {'a.LOG': 'log_fsdA'}, {'a\x00.log': 'log_fsdA'}):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                selection.validate_source_map(mapping)

    def test_global_rebinding_still_visible_to_patched_method(self):
        module = self.module()
        selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'})
        module.should_keep_log = lambda *args: False
        self.assertEqual(module.RecordPerformance([Path('vla_A_21.log')]).extract_logs('x', ['vla_']), [])

    def test_explicit_excluded_conflict_is_removed_before_resolver_and_time_predicate(self):
        module = self.module()
        paths = [Path(name) for name in ('vla_A_21.log', 'vla_B_25.log', 'vla_conflict_30.log',
                                        'vla_A_35.log', 'vla_B_36.log')]
        mapping = {path.name: ('log_fsdB' if '_B_' in path.name else 'log_fsdA')
                   for path in paths if 'conflict' not in path.name}
        audit = selection.install_source_boundary_patch(module, mapping, ['vla_conflict_30.log'])
        resolved = []
        inspected = []
        original_resolver = module.__dict__[selection._RESOLVER]
        original_predicate = module.should_keep_log
        def resolver(path):
            resolved.append(path.name)
            return original_resolver(path)
        def predicate(path, window, state):
            inspected.append(path.name)
            return original_predicate(path, window, state)
        module.__dict__[selection._RESOLVER] = resolver
        module.should_keep_log = predicate
        self.assertEqual(module.RecordPerformance(paths).extract_logs('x', ['vla_']),
                         ['vla_A_21.log', 'vla_B_25.log'])
        self.assertNotIn('vla_conflict_30.log', resolved)
        self.assertEqual(resolved, inspected)
        self.assertEqual(len(resolved), 4)
        self.assertEqual(audit['excluded_basenames'], ['vla_conflict_30.log'])

    def test_unknown_not_explicitly_excluded_still_fails(self):
        module = self.module()
        selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'}, ['vla_conflict_30.log'])
        with self.assertRaisesRegex(ValueError, '未审计来源'):
            module.RecordPerformance([Path('vla_A_21.log'), Path('vla_conflict_30.log'),
                                      Path('vla_unknown_31.log')]).extract_logs('x', ['vla_'])

    def test_filter_preserves_original_path_objects_and_order(self):
        module = self.module()
        selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'}, ['vla_conflict_30.log'])
        late, first, conflict = [Path(name) for name in ('vla_A_40.log', 'vla_A_21.log', 'vla_conflict_30.log')]
        filtered = module.__dict__[selection._FILTER]([late, conflict, first])
        self.assertEqual(filtered, [late, first])
        self.assertIs(filtered[0], late)
        self.assertIs(filtered[1], first)

    def test_exclusion_mapping_overlap_and_wildcards_are_rejected(self):
        for excluded in (['vla_A_21.log'], ['VLA_A_21.log'], ['../late.log'],
                         ['vla_*.log'], ['vla_?.log'], ['vla_[AB].log'],
                         ['late.log', 'late.log'], 'late.log'):
            module = self.module()
            with self.subTest(excluded=excluded), self.assertRaises(ValueError):
                selection.install_source_boundary_patch(module, {'vla_A_21.log': 'log_fsdA'}, excluded)

    def test_nonempty_exclusion_audit_and_exact_case_variants_are_valid(self):
        for excluded in (['vla_conflict_30.log'], ['vla_CLASH_30.log', 'vla_clash_30.log']):
            audit = selection_audit_fixture(excluded_basenames=excluded)
            self.assertEqual(selection.validate_selection_audit(audit), audit)

    def test_exclusion_ledger_or_members_tampering_is_rejected(self):
        base = selection_audit_fixture(excluded_basenames=['vla_conflict_30.log'])
        cases = []
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'] = []; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['basename'] = 'other.log'; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['reason'] = 'assumed'; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['variants'][1]['source'] = 'log_fsdA'; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['variants'][1]['sha256'] = 'f' * 64; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['variants'][1]['name'] = 'other.log'; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['excluded_conflicts'][0]['variants'][1]['source'] = 'unknown'; cases.append(value)
        value = copy.deepcopy(base); value['input_inventory']['basename_hashes'].pop('vla_conflict_30.log'); cases.append(value)
        for index, audit in enumerate(cases):
            audit['input_inventory_sha256'] = selection._digest(audit['input_inventory'])
            with self.subTest(index=index), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)

    def test_audit_round_trip_preserves_additional_fields(self):
        audit = selection_audit_fixture()
        audit['extra_diagnostic'] = {'note': 'synthetic'}
        self.assertEqual(selection.validate_selection_audit(audit), audit)
        result = selection.validate_selection_audit(audit)
        result['source_by_basename']['other.log'] = 'legacy'
        self.assertNotIn('other.log', audit['source_by_basename'])

    def test_audit_rejects_each_missing_required_field(self):
        for key in selection_audit_fixture():
            audit = selection_audit_fixture()
            audit.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)

    def test_audit_rejects_tampered_mapping_hash_counts_flags_and_methods(self):
        mutations = [
            ('policy', 'older-policy'),
            ('source_by_basename', {'vla_synthetic_21.log': 'log_fsdB'}),
            ('source_map_sha256', '0' * 64),
            ('source_count', 2), ('source_count', True),
            ('mapped_log_count', 2), ('mapped_log_count', True),
            ('legacy_source_used', True), ('legacy_source_used', 0),
            ('ast_replacements', 1), ('ast_replacements', True),
            ('ast_replacements', 2),
            ('original_extract_logs_ast_sha256', 'bad'),
            ('patched_extract_logs_ast_sha256', 'a' * 64),
            ('patched_extract_logs_ast_sha256', 'B' * 64),
            ('input_inventory_sha256', '0' * 64),
            ('excluded_basenames', ['vla_conflict_30.log']),
            ('excluded_basenames_sha256', '0' * 64),
        ]
        for key, value in mutations:
            audit = selection_audit_fixture()
            audit[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)

    def test_audit_rejects_inventory_policy_mapping_or_members_even_with_updated_digest(self):
        for key, value in (
            ('selection_policy', 'older-policy'),
            ('source_by_basename', {'vla_synthetic_21.log': 'log_fsdB'}),
            ('basename_hashes', {}),
            ('basename_hashes', {'vla_synthetic_21.log': [['invalid-hash', 1]]}),
            ('basename_hashes', {'vla_synthetic_21.log': [['d' * 64, True]]}),
        ):
            audit = selection_audit_fixture()
            audit['input_inventory'][key] = value
            audit['input_inventory_sha256'] = selection._digest(audit['input_inventory'])
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)

    def test_selected_member_audit_is_bound_to_map_content_and_count(self):
        mutations = [
            ('selected_members', None), ('selected_members', {}),
            ('selected_log_count', 0), ('selected_log_count', True),
            ('selected_members', {'other.log': {'source': 'log_fsdA', 'sha256': 'd' * 64, 'bytes': 1}}),
            ('selected_members', {'vla_synthetic_21.log': {'source': 'log_fsdB', 'sha256': 'd' * 64, 'bytes': 1}}),
            ('selected_members', {'vla_synthetic_21.log': {'source': 'log_fsdA', 'sha256': 'e' * 64, 'bytes': 1}}),
            ('selected_members', {'vla_synthetic_21.log': {'source': 'log_fsdA', 'sha256': 'd' * 64, 'bytes': True}}),
            ('selected_members', {'vla_synthetic_21.log': {'source': 'log_fsdA', 'sha256': 'd' * 64, 'bytes': 2}}),
            ('basename_hashes', {'vla_synthetic_21.log': [['d' * 64, 1], ['e' * 64, 2]]}),
        ]
        for key, value in mutations:
            audit = selection_audit_fixture()
            audit['input_inventory'][key] = value
            audit['input_inventory_sha256'] = selection._digest(audit['input_inventory'])
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)

    def test_audit_rejects_unknown_complete_method_even_if_sha_well_formed(self):
        for key in ('original_extract_logs_ast_sha256', 'patched_extract_logs_ast_sha256'):
            audit = selection_audit_fixture()
            audit[key] = 'e' * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                selection.validate_selection_audit(audit)


if __name__ == '__main__':
    unittest.main()
