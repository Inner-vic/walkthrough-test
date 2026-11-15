"""Failure injection across the actual namespaced native boundary."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import unittest

from native_windows_host import filesystem_path
import source_runtime_audit as audit
from test_source_namespace import _Fixture, WINDOW


class SourceRuntimeAuditTests(_Fixture, unittest.TestCase):
    def make(self, *, receipt=True):
        self.stage()
        self.ini = self.folder / 'o.ini'
        self.ini.write_text('[PARAMETER]\ntime=' + WINDOW + '\nlogPath=' + str(self.flat) + '\n', encoding='utf-8')
        self.values = SimpleNamespace(get_theme_lists=lambda rules: list(rules),
            get_theme=lambda rules, theme: (True, rules[theme][next(iter(rules[theme]))]['soc_type'], []))
        self.observer = audit.install_runtime_audit(self.module, self.values,
            request_time_range=WINDOW, inventory=self.catalog, rules=self.rules, path_config=self.config,
            flat_log_root=self.flat, ini_path=self.ini,
            collector_receipt=(lambda: self.receipt) if receipt else None)
        return self.record()

    def complete(self):
        record = self.make()
        for theme in self.rules:
            record.extract_logs(theme, 'M')
        return self.observer.finalize()

    def test_actual_collector_and_two_same_basename_members_are_independently_proven(self):
        value = self.complete()
        self.assertEqual(value['schema_version'], 3)
        self.assertEqual(len(value['actual_collector_members']), 8)
        self.assertEqual(len(value['actual_selected_members']), 4)
        shared = {key: row for key, row in value['actual_selected_members'].items() if row['basename'] == self.shared}
        self.assertEqual({row['source'] for row in shared.values()}, {'log_fsdA', 'log_fsdB'})
        for row in value['extractions']:
            for key in shared:
                self.assertEqual(row['actual_paths'][key], 'log/M/' + key + '/' + self.shared)
        audit.validate_runtime_execution(value, request_time_range=WINDOW, inventory=self.catalog, rules=self.rules)

    def test_missing_native_theme_is_not_success(self):
        record = self.make()
        record.extract_logs('first', 'M')
        with self.assertRaisesRegex(ValueError, '所有'):
            self.observer.finalize()

    def test_swallowed_wrong_window_error_remains_latched_after_restore(self):
        record = self.make()
        original = record.time_range
        record.time_range = (original[0] - 60, original[1])
        with self.assertRaises(ValueError):
            record.extract_logs('first', 'M')
        record.time_range = original
        for theme in self.rules:
            record.extract_logs(theme, 'M')
        with self.assertRaisesRegex(ValueError, '吞下'):
            self.observer.finalize()

    def test_actual_predicate_bypass_is_detected_and_latched(self):
        record = self.make()
        adapter = self.module.RecordPerformance.extract_logs.__source_namespace_adapter__
        original = adapter.extract
        # Replacing the original predicate after audit installation prevents
        # observation even when a bypass returns a plausible output list.
        predicate = self.module.should_keep_log
        self.module.should_keep_log = lambda path, epochs, state: False
        with self.assertRaisesRegex(ValueError, '谓词'):
            record.extract_logs('first', 'M')
        self.module.should_keep_log = predicate
        adapter.extract = original
        with self.assertRaisesRegex(ValueError, '吞下'):
            self.observer.finalize()

    def test_collector_extra_and_tampered_files_fail_even_if_not_selected(self):
        record = self.make()
        (self.flat / 'members' / 'extra.log').write_bytes(b'extra nonselected')
        with self.assertRaisesRegex(ValueError, 'collector'):
            self.observer.finalize()

    def test_nonselected_collector_member_hash_is_still_verified(self):
        record = self.make()
        member = next(row for row in self.catalog['members'].values() if row['basename'] == 'vin_metadata.log')
        filesystem_path(self.flat / member['namespace_path']).write_bytes(b'mutated metadata')
        with self.assertRaisesRegex(ValueError, 'collector'):
            self.observer.finalize()

    def test_incorrect_materialization_receipt_fails_at_constructor(self):
        self.stage()
        self.ini = self.folder / 'o.ini'
        self.ini.write_text('[PARAMETER]\ntime=' + WINDOW + '\nlogPath=' + str(self.flat) + '\n', encoding='utf-8')
        values = SimpleNamespace(get_theme_lists=lambda rules: list(rules), get_theme=lambda rules, theme: (True, 'M', []))
        observer = audit.install_runtime_audit(self.module, values, request_time_range=WINDOW,
            inventory=self.catalog, rules=self.rules, path_config=self.config, flat_log_root=self.flat,
            ini_path=self.ini, collector_receipt=lambda: {'version': 'forged'})
        with self.assertRaisesRegex(ValueError, '回执'):
            self.record()
        with self.assertRaises(ValueError):
            observer.finalize()

    def test_bound_collector_callable_must_return_exact_receipt(self):
        record = self.make()
        self.observer.receipt_source = lambda: None
        with self.assertRaisesRegex(ValueError, '回执'):
            self.observer.finalize()

    def test_extra_empty_namespace_directory_is_rejected(self):
        record = self.make()
        (self.flat / 'members' / 'unknown').mkdir()
        with self.assertRaisesRegex(ValueError, '未知'):
            self.observer.finalize()

    def test_returned_member_omission_is_detected_before_statistics(self):
        record = self.make()
        adapter = self.module.RecordPerformance.extract_logs.__source_namespace_adapter__
        original = adapter.extract
        adapter.extract = lambda record, theme, soc_type: original(record, theme, soc_type)[:-1]
        with self.assertRaisesRegex(ValueError, '实际消费'):
            record.extract_logs('first', 'M')
        with self.assertRaisesRegex(ValueError, '吞下'):
            self.observer.finalize()

    def test_ini_change_is_detected_and_retained(self):
        record = self.make()
        text = self.ini.read_text(encoding='utf-8')
        self.ini.write_text(text.replace('20261102172900', '20261102172800'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'INI'):
            record.extract_logs('first', 'M')
        self.ini.write_text(text, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '吞下'):
            self.observer.finalize()

    def test_concurrent_same_scope_observations_remain_complete(self):
        record = self.make()
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda theme: record.extract_logs(theme, 'M'), self.rules))
        self.assertEqual(values[0], values[1])
        self.assertEqual(len(self.observer.finalize()['extractions']), 2)

    def test_retained_audit_rejects_identity_path_predicate_and_union_forgery(self):
        original = self.complete()
        cases = []
        row = copy.deepcopy(original); row['extractions'][0]['actual_paths'][next(iter(row['extractions'][0]['actual_paths']))] = 'log/M/other.log'; cases.append(row)
        row = copy.deepcopy(original); row['extractions'][0]['actual_predicate_calls'] = []; cases.append(row)
        row = copy.deepcopy(original); row['actual_selected_members'] = {}; cases.append(row)
        row = copy.deepcopy(original); row['actual_collector_members'] = {}; cases.append(row)
        row = copy.deepcopy(original); row['auditor_module_sha256'] = '0' * 64; cases.append(row)
        for value in cases:
            with self.subTest(index=cases.index(value)), self.assertRaises(ValueError):
                audit.validate_runtime_execution(value, request_time_range=WINDOW, inventory=self.catalog, rules=self.rules)

    def test_every_retained_checksum_rejects_non_hex_and_wrong_types(self):
        original = self.complete()
        for invalid in ('z' * 64, 'A' * 64, '0' * 63, '0' * 65, None, True, 64, ['0' * 64]):
            for field in ('input_inventory_sha256', 'auditor_module_sha256', 'rules_sha256', 'path_config_sha256', 'ini_sha256', 'final_ini_sha256'):
                value = copy.deepcopy(original)
                if field == 'ini_sha256':
                    value['initializations'][0]['ini_sha256'] = invalid
                elif field == 'final_ini_sha256':
                    value['final_ini']['sha256'] = invalid
                else:
                    value[field] = invalid
                with self.subTest(field=field, invalid=repr(invalid)), self.assertRaises(ValueError):
                    # Rules omitted exercises strict digest syntax independently
                    # of equality to an externally supplied rules dictionary.
                    audit.validate_runtime_execution(value, request_time_range=WINDOW, inventory=self.catalog)

    def test_malformed_retained_structures_fail_as_bounded_value_errors(self):
        original = self.complete()
        cases = []
        for key, invalid in (('native_skipped_themes', [{}]), ('native_theme_names', [{}]),
                             ('path_config', []), ('theme_specs', []), ('extractions', {}),
                             ('initializations', [None]), ('collector_receipt', None)):
            value = copy.deepcopy(original)
            value[key] = invalid
            cases.append(value)
        value = copy.deepcopy(original); value['extractions'][0]['theme'] = []; cases.append(value)
        value = copy.deepcopy(original); value['theme_specs']['first']['soc_type'] = [{}]; cases.append(value)
        for index, value in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                audit.validate_runtime_execution(value, request_time_range=WINDOW, inventory=self.catalog, rules=self.rules)


if __name__ == '__main__':
    unittest.main()
