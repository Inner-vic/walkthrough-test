"""Synthetic failure injection for observed native windows and actual bytes."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace, FunctionType
import unittest
import os
import threading
from unittest.mock import patch

import runtime_audit as audit

WINDOW = '20261023150000/20261023153500'


def synthetic_runtime_execution(*, inventory, request_time_range=WINDOW, rules=None):
    """Mock receipt for cache/adapter unit tests, never a native success proof."""
    rules = rules or {'synthetic': {'metric': {'soc_type': 'vla_'}}}
    members = copy.deepcopy(inventory['selected_members'])
    names = sorted(members)
    themes = list(rules)
    root = str(Path(tempfile.gettempdir()).resolve() / 'synthetic-native-perf')
    epochs = audit.request_epochs(request_time_range)
    rules_sha = audit.canonical_sha(rules)
    # External-process unit fixtures still carry a complete schema. Exact full
    # name prefixes intentionally select these synthetic members only.
    config = {}
    specs, proofs, extractions = {}, {}, []
    for theme, metrics in rules.items():
        soc_type = audit._rule_soc_type(metrics)
        for alias in soc_type if isinstance(soc_type, list) else [soc_type]:
            config[alias] = [name[:-4] for name in names] or ['synthetic_no_input_']
        prefixes = audit._prefix_plan(soc_type, config)
        specs[theme] = {'soc_type': soc_type, 'prefixes': prefixes, 'candidate_names': names}
        events, chosen = [], set()
        for prefix in prefixes:
            matching = [name for name in inventory['source_by_basename'] if name.startswith(prefix) and name.endswith('.log')]
            ordered, selected = audit._exact_selection(matching, inventory['source_by_basename'], epochs)
            events.append({'prefix': prefix, 'glob_names': matching, 'filtered_names': matching[:],
                           'sorted_names': ordered, 'selected_names': selected})
            chosen.update(selected)
        scope = audit._scope_name(theme, soc_type)
        exact = sorted(chosen)
        proof = {'origin_theme': theme, 'scope': scope, 'prefix_plan': prefixes,
                 'glob_observations': events, 'expected_names': exact}
        proof_id = audit.canonical_sha(proof)
        proofs[proof_id] = proof
        extractions.append({'theme': theme, 'scope': scope, 'scope_proof_id': proof_id,
            'selection_mode': 'observed_glob', 'record_epoch_range': epochs, 'ini_time_range': request_time_range,
            'expected_names': exact, 'actual_members': {name: members[name] for name in exact},
            'actual_paths': {name: 'log/' + scope + '/' + name for name in exact}})
    config_sha = audit.canonical_sha(config)
    receipt = {
        'schema_version': 2, 'policy': audit.POLICY, 'status': 'verified',
        'request_time_range': request_time_range, 'expected_epoch_range': epochs,
        'input_inventory_sha256': audit.canonical_sha(inventory), 'rules_sha256': rules_sha,
        'path_config': config, 'path_config_sha256': config_sha, 'flat_log_root': root,
        'collector_candidate_members': members, 'actual_selected_members': copy.deepcopy(members),
        'native_theme_names': themes, 'native_skipped_themes': [],
        'theme_specs': specs, 'scope_proofs': proofs,
        'initializations': [{'constructor_epoch_range': epochs, 'record_epoch_range': epochs,
            'ini_time_range': request_time_range, 'ini_sha256': 'a' * 64, 'ini_log_path': root,
            'record_log_path': root, 'native_theme_names': themes, 'record_rules_sha256': rules_sha,
            'record_path_config_sha256': config_sha}],
        'extractions': extractions,
        'errors': [], 'final_ini': {'time_range': request_time_range, 'log_path': root, 'sha256': 'a' * 64},
        'hash_work': {'physical_files_hashed': 0, 'bytes_hashed': 0, 'stable_stat_cache_hits': 0},
        'observation_boundary': 'synthetic external-process boundary fixture only',
        'continuous_log_body_coverage_proven': False,
    }
    return audit.validate_runtime_execution(receipt, request_time_range=request_time_range,
                                            inventory=inventory, rules=rules)


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.flat = self.folder / 'input' / 'perf'
        self.flat.mkdir(parents=True)
        self.ini = self.folder / 'o.ini'
        self.ini.write_text('[PARAMETER]\ntime=' + WINDOW + '\nlogPath=' + str(self.flat) + '\n', encoding='utf-8')
        self.config = {'VLA': ['vla_']}
        self.rules = {'first': {'one': {'soc_type': 'VLA'}}, 'second': {'two': {'soc_type': 'VLA'}}}
        rows = (
            ('vla_A_2026_10_23_15_20_00.log', 'log_fsdA', b'inside'),
            ('vla_A_2026_10_23_15_36_34.log', 'log_fsdA', b'A-boundary'),
            ('vla_B_2026_10_23_15_39_16.log', 'log_fsdB', b'B-boundary'),
            ('vla_A_2026_10_23_15_50_00.log', 'log_fsdA', b'A-later-not-selected'),
        )
        self.mapping, self.members, hashes = {}, {}, {}
        for index, (name, source, payload) in enumerate(rows):
            (self.flat / name).write_bytes(payload)
            sha = hashlib.sha256(payload).hexdigest()
            self.mapping[name] = source
            hashes[name.casefold()] = [[sha, len(payload)]]
            if index < 3:
                self.members[name] = {'sha256': sha, 'bytes': len(payload), 'source': source}
        self.inventory = {'selection_policy': 'synthetic-source-boundary', 'source_by_basename': self.mapping,
                          'selected_members': self.members, 'basename_hashes': hashes, 'excluded_conflicts': []}
        self.behavior = {}

    def make(self, *, inventory=None, rules=None):
        behavior, mapping = self.behavior, (inventory or self.inventory)['source_by_basename']

        def parse(path):
            matches = list(re.finditer(r'(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})(?=\D|$)', path.name))
            return datetime.strptime(matches[-1][1], '%Y_%m_%d_%H_%M_%S').replace(
                tzinfo=timezone(timedelta(hours=8))) if matches else None

        def keep(path, window, state):
            stamp = parse(path)
            if stamp is None:
                return True
            if stamp.timestamp() < window[0]:
                return False
            if stamp.timestamp() > window[1]:
                if state['kept_first_after_end']:
                    return False
                state['kept_first_after_end'] = True
            return True

        class Recorder:
            def __init__(self, log_path, theme_lists, time_range, path_config, config_date):
                self.log_path, self.theme_lists = log_path, theme_lists
                self.time_range = behavior.get('record_time', time_range)
                self.path_config, self.config_date = path_config, config_date
                self.cached = set()

            def extract_logs(self, theme, soc_type):
                if behavior.get('raise'):
                    raise OSError('synthetic upstream failure')
                scope = theme if isinstance(soc_type, list) else soc_type
                target = Path(self.log_path) / 'log' / scope
                target.mkdir(parents=True, exist_ok=True)
                if scope not in self.cached and not behavior.get('preexisting_cache'):
                    for alias in soc_type if isinstance(soc_type, list) else [soc_type]:
                        for prefix in self.path_config.get(alias, [alias]):
                            paths = list(Path(self.log_path).glob(prefix + '*.log'))
                            if behavior.get('glob_reverse'):
                                paths.reverse()
                            paths = [path for path in paths if path.name != behavior.get('glob_drop')]
                            paths = _source_boundary_filter_v2(paths)
                            paths.sort(key=lambda path: (parse(path) is not None, parse(path)))
                            states = {}
                            for path in paths:
                                if not keep(path, self.time_range, states.setdefault(mapping[path.name], {'kept_first_after_end': False})):
                                    continue
                                if path.name == behavior.get('copy_failure'):
                                    # Original collector/extractor may log and swallow this.
                                    continue
                                destination = target / path.name
                                if not destination.exists():
                                    os.link(path, destination)
                    self.cached.add(scope)
                if behavior.get('mutate_time_during_extract'):
                    self.time_range = [0, 1]
                if behavior.get('extra'):
                    outside = target / 'unexpected.log'
                    outside.write_text('unexpected', encoding='utf-8')
                if behavior.get('extra_later'):
                    path = Path(self.log_path) / behavior['extra_later']
                    os.link(path, target / path.name)
                return [str(path) for path in sorted(target.rglob('*.log'), key=lambda path: path.name)]

        # Each fake module has its own globals just as independent real native
        # worker processes do; no observer is shared across concurrent windows.
        method = Recorder.extract_logs
        namespace = method.__globals__.copy()
        excluded = {row['basename'] for row in (inventory or self.inventory).get('excluded_conflicts', [])}
        namespace['_source_boundary_filter_v2'] = lambda paths: [path for path in paths if path.name not in excluded]
        Recorder.extract_logs = FunctionType(method.__code__, namespace, method.__name__, method.__defaults__, method.__closure__)
        Recorder.extract_logs.__source_boundary_policy__ = 'synthetic-patched-before-runtime-hook'
        csv = SimpleNamespace(RecordPerformance=Recorder, _parse_cn_dt_from_log_path=parse, should_keep_log=keep)
        value = SimpleNamespace(get_theme_lists=lambda config: list(config),
                                get_theme=lambda config, theme: (True, audit._rule_soc_type(config[theme]), []))
        controller = audit.install_runtime_audit(csv, value, request_time_range=WINDOW,
            inventory=inventory or self.inventory, rules=rules or self.rules,
            path_config=self.config, flat_log_root=self.flat, ini_path=self.ini)
        return controller, csv

    def initialize(self, csv, time_range=None):
        return csv.RecordPerformance(str(self.flat), list(self.rules),
            audit.request_epochs(WINDOW) if time_range is None else time_range, self.config, self.rules)

    def finish(self, controller, record):
        for theme in self.rules:
            record.extract_logs(theme, 'VLA')
        return controller.finalize()

    def test_actual_window_full_members_and_stable_hash_reuse(self):
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        self.assertEqual(receipt['collector_candidate_members'], self.members)
        self.assertEqual({row['theme'] for row in receipt['extractions']}, set(self.rules))
        self.assertTrue(all(row['actual_members'] == self.members for row in receipt['extractions']))
        self.assertGreater(receipt['hash_work']['stable_stat_cache_hits'], 0)
        self.assertEqual(audit.validate_runtime_execution(receipt, request_time_range=WINDOW,
            inventory=self.inventory, rules=self.rules), receipt)

    def test_wrong_constructor_window_and_all_time_mode_fail(self):
        for value in ([0, 1], None):
            with self.subTest(window=value):
                controller, csv = self.make()
                with self.assertRaises(audit.RuntimeAuditError):
                    csv.RecordPerformance(str(self.flat), list(self.rules), value, self.config, self.rules)
                with self.assertRaises(audit.RuntimeAuditError):
                    controller.finalize()

    def test_ini_mismatch_is_observed_instead_of_trusting_output_label(self):
        controller, csv = self.make()
        self.ini.write_text('[PARAMETER]\ntime=20261023150000/20261023160000\nlogPath=' + str(self.flat), encoding='utf-8')
        with self.assertRaisesRegex(audit.RuntimeAuditError, 'o.ini'):
            self.initialize(csv)
        with self.assertRaises(audit.RuntimeAuditError):
            controller.finalize()

    def test_record_window_different_from_constructor_is_rejected(self):
        self.behavior['record_time'] = [0, 1]
        controller, csv = self.make()
        with self.assertRaisesRegex(audit.RuntimeAuditError, 'RecordPerformance'):
            self.initialize(csv)

    def test_collector_missing_expected_member_is_not_no_sample(self):
        controller, csv = self.make()
        (self.flat / next(iter(self.members))).unlink()
        with self.assertRaises(FileNotFoundError):
            self.initialize(csv)
        with self.assertRaises(Exception):
            controller.finalize()

    def test_other_archive_variant_hash_cannot_replace_selected_eligible_content(self):
        name = next(iter(self.members))
        wrong = b'wrong!'
        self.inventory['basename_hashes'][name.casefold()].append([hashlib.sha256(wrong).hexdigest(), len(wrong)])
        (self.flat / name).write_bytes(wrong)
        controller, csv = self.make()
        with self.assertRaisesRegex(audit.RuntimeAuditError, 'collector'):
            self.initialize(csv)

    def test_swallowed_copy_failure_is_latched_until_final_export(self):
        controller, csv = self.make()
        record = self.initialize(csv)
        self.behavior['copy_failure'] = next(iter(self.members))
        try:
            record.extract_logs('first', 'VLA')
        except audit.RuntimeAuditError:
            pass  # Equivalent to the original log_process's broad catch.
        self.assertTrue(controller.errors)
        with self.assertRaisesRegex(audit.RuntimeAuditError, '吞下'):
            controller.finalize()

    def test_swallowed_upstream_exception_cannot_produce_partial_success(self):
        controller, csv = self.make()
        record = self.initialize(csv)
        self.behavior['raise'] = True
        try:
            record.extract_logs('first', 'VLA')
        except OSError:
            pass
        with self.assertRaisesRegex(audit.RuntimeAuditError, '吞下'):
            controller.finalize()

    def test_changed_selected_bytes_after_initialization_fail(self):
        controller, csv = self.make()
        record = self.initialize(csv)
        (self.flat / next(iter(self.members))).write_bytes(b'changed')
        with self.assertRaises(audit.RuntimeAuditError):
            record.extract_logs('first', 'VLA')

    def test_unpredicted_extracted_file_is_rejected(self):
        self.behavior['extra'] = True
        controller, csv = self.make()
        record = self.initialize(csv)
        with self.assertRaisesRegex(audit.RuntimeAuditError, '未审计'):
            record.extract_logs('first', 'VLA')

    def test_missing_theme_execution_is_rejected(self):
        controller, csv = self.make()
        record = self.initialize(csv)
        record.extract_logs('first', 'VLA')
        with self.assertRaisesRegex(audit.RuntimeAuditError, '所有应执行主题'):
            controller.finalize()

    def test_actual_record_time_mutation_during_extract_is_rejected(self):
        self.behavior['mutate_time_during_extract'] = True
        controller, csv = self.make()
        record = self.initialize(csv)
        with self.assertRaises(audit.RuntimeAuditError):
            record.extract_logs('first', 'VLA')

    def test_legacy_stays_one_explicit_stream(self):
        inventory = copy.deepcopy(self.inventory)
        inventory['source_by_basename'] = {name: 'legacy' for name in self.mapping}
        inventory['selected_members'] = {name: {**row, 'source': 'legacy'} for name, row in self.members.items()
                                         if '_B_' not in name}
        controller, csv = self.make(inventory=inventory)
        receipt = self.finish(controller, self.initialize(csv))
        self.assertEqual(set(receipt['collector_candidate_members']), set(inventory['selected_members']))

    def test_prediction_union_must_match_selected_member_map(self):
        inventory = copy.deepcopy(self.inventory)
        inventory['selected_members'].pop(next(iter(self.members)))
        with self.assertRaisesRegex(audit.RuntimeAuditError, '并集'):
            self.make(inventory=inventory)

    def add_tied_candidate(self, *, source='log_fsdB', stamp='2026_10_23_15_39_16'):
        name, payload = 'vla_copy_' + stamp + '.log', b'different-boundary-body'
        (self.flat / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.mapping[name] = source
        self.members[name] = {'sha256': digest, 'bytes': len(payload), 'source': source}
        self.inventory['basename_hashes'][name.casefold()] = [[digest, len(payload)]]
        return name

    def test_same_source_tie_uses_actual_glob_order_not_inventory_order(self):
        other = self.add_tied_candidate()
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        proof = next(iter(receipt['scope_proofs'].values()))
        event = proof['glob_observations'][0]
        tied = [name for name in event['glob_names'] if self.mapping[name] == 'log_fsdB']
        self.assertEqual(set(receipt['collector_candidate_members']), set(self.members))
        self.assertIn(tied[0], receipt['actual_selected_members'])
        self.assertNotIn(tied[1], receipt['actual_selected_members'])
        self.assertEqual(receipt['extractions'][1]['selection_mode'], 'cached_scope')
        self.assertIn(other, receipt['collector_candidate_members'])

    def test_reverse_glob_tie_selects_exact_first_observed_member(self):
        self.add_tied_candidate()
        self.behavior['glob_reverse'] = True
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        proof = next(iter(receipt['scope_proofs'].values()))
        event = proof['glob_observations'][0]
        tied = [name for name in event['glob_names'] if self.mapping[name] == 'log_fsdB']
        self.assertIn(tied[0], receipt['actual_selected_members'])
        self.assertNotIn(tied[1], receipt['actual_selected_members'])
        self.assertEqual(event['sorted_names'].index(tied[0]) + 1, event['sorted_names'].index(tied[1]))

    def test_each_source_retains_one_boundary_with_its_own_same_second_ties(self):
        self.add_tied_candidate()
        self.add_tied_candidate(source='log_fsdA', stamp='2026_10_23_15_36_34')
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        self.assertEqual(len(receipt['collector_candidate_members']), 5)
        self.assertEqual(len(receipt['actual_selected_members']), 3)
        event = next(iter(receipt['scope_proofs'].values()))['glob_observations'][0]
        for source, stamp in (('log_fsdA', '15_36_34'), ('log_fsdB', '15_39_16')):
            tied = [name for name in event['glob_names'] if self.mapping[name] == source and stamp in name]
            self.assertIn(tied[0], receipt['actual_selected_members'])
            self.assertNotIn(tied[1], receipt['actual_selected_members'])

    def test_missing_even_unselected_tied_glob_candidate_cannot_self_justify(self):
        other = self.add_tied_candidate()
        self.behavior['glob_drop'] = other
        controller, csv = self.make()
        record = self.initialize(csv)
        with self.assertRaisesRegex(audit.RuntimeAuditError, '遗漏候选'):
            record.extract_logs('first', 'VLA')
        with self.assertRaises(audit.RuntimeAuditError):
            controller.finalize()

    def test_preflight_cannot_omit_one_possible_tie_candidate(self):
        name = self.add_tied_candidate()
        self.members.pop(name)
        with self.assertRaisesRegex(audit.RuntimeAuditError, '并集'):
            self.make()

    def test_unselected_tied_candidate_still_requires_exact_original_body(self):
        name = self.add_tied_candidate()
        controller, csv = self.make()
        record = self.initialize(csv)
        self.finish(controller, record)
        (self.flat / name).write_bytes(b'wrong-unselected-body')
        with self.assertRaisesRegex(audit.RuntimeAuditError, 'collector'):
            controller.finalize()

    def test_extra_later_boundary_output_is_rejected(self):
        self.behavior['extra_later'] = 'vla_A_2026_10_23_15_50_00.log'
        controller, csv = self.make()
        with self.assertRaisesRegex(audit.RuntimeAuditError, '未审计'):
            self.initialize(csv).extract_logs('first', 'VLA')

    def test_overlapping_repeated_prefixes_are_exact_union_and_share_cache_proof(self):
        copied_boundary = self.add_tied_candidate()
        self.config['VLA'] = ['vla_', 'vla_copy_', 'vla_']
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        self.assertEqual(len(receipt['scope_proofs']), 1)
        proof = next(iter(receipt['scope_proofs'].values()))
        self.assertEqual([row['prefix'] for row in proof['glob_observations']], self.config['VLA'])
        # Native filesystem glob order is platform dependent. The broad prefix
        # keeps its first tied B boundary; the narrow prefix always keeps copy.
        # Predict this union from observed *input order*, never from output.
        broad = proof['glob_observations'][0]['glob_names']
        first_b = next(name for name in broad if self.mapping[name] == 'log_fsdB')
        fixed_a = {'vla_A_2026_10_23_15_20_00.log', 'vla_A_2026_10_23_15_36_34.log'}
        expected = fixed_a | {first_b, copied_boundary}
        self.assertEqual(set(proof['glob_observations'][0]['selected_names']), fixed_a | {first_b})
        self.assertEqual(proof['glob_observations'][1]['selected_names'], [copied_boundary])
        self.assertEqual(set(proof['glob_observations'][2]['selected_names']), fixed_a | {first_b})
        self.assertEqual(receipt['collector_candidate_members'], self.members)
        self.assertEqual(receipt['actual_selected_members'], {name: self.members[name] for name in expected})
        self.assertEqual(receipt['extractions'][1]['selection_mode'], 'cached_scope')
        self.assertEqual(receipt['extractions'][0]['scope_proof_id'], receipt['extractions'][1]['scope_proof_id'])
        self.assertEqual(receipt['extractions'][0]['actual_members'], receipt['extractions'][1]['actual_members'])

    def test_unobserved_preexisting_cache_cannot_be_accepted_from_member_subset(self):
        self.behavior['preexisting_cache'] = True
        controller, csv = self.make()
        with self.assertRaisesRegex(audit.RuntimeAuditError, '缓存提取缺少'):
            self.initialize(csv).extract_logs('first', 'VLA')

    def test_cached_scope_must_keep_prior_exact_selection_even_when_glob_order_changes(self):
        self.add_tied_candidate()
        controller, csv = self.make()
        record = self.initialize(csv)
        first = record.extract_logs('first', 'VLA')
        self.behavior['glob_reverse'] = True
        second = record.extract_logs('second', 'VLA')
        self.assertEqual(first, second)
        receipt = controller.finalize()
        self.assertEqual([row['selection_mode'] for row in receipt['extractions']], ['observed_glob', 'cached_scope'])

    def test_retained_proof_cannot_turn_arbitrary_candidate_subset_into_success(self):
        self.add_tied_candidate()
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        original_id = next(iter(receipt['scope_proofs']))
        changed = copy.deepcopy(receipt)
        proof = changed['scope_proofs'].pop(original_id)
        proof['glob_observations'][0]['selected_names'] = []
        new_id = audit.canonical_sha(proof)
        changed['scope_proofs'][new_id] = proof
        for row in changed['extractions']:
            row['scope_proof_id'] = new_id
        with self.assertRaisesRegex(audit.RuntimeAuditError, '原谓词选择'):
            audit.validate_runtime_execution(changed, request_time_range=WINDOW, inventory=self.inventory, rules=self.rules)

    def test_retained_cache_cannot_precede_observed_scope_proof(self):
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        receipt['extractions'].reverse()
        with self.assertRaisesRegex(audit.RuntimeAuditError, '先前完成'):
            audit.validate_runtime_execution(receipt, request_time_range=WINDOW, inventory=self.inventory, rules=self.rules)

    def test_retained_observed_scope_origin_must_match_actual_calling_theme(self):
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        old_id = next(iter(receipt['scope_proofs']))
        proof = receipt['scope_proofs'].pop(old_id)
        proof['origin_theme'] = 'second'
        new_id = audit.canonical_sha(proof)
        receipt['scope_proofs'][new_id] = proof
        for row in receipt['extractions']:
            row['scope_proof_id'] = new_id
        with self.assertRaisesRegex(audit.RuntimeAuditError, '发起主题'):
            audit.validate_runtime_execution(receipt, request_time_range=WINDOW, inventory=self.inventory, rules=self.rules)

    def test_filename_predicate_uses_last_stamp_and_inclusive_window(self):
        mapping = {
            'vla_2026_10_23_14_00_00__2026_10_23_15_00_00.log': 'log_fsdA',
            'vla_2026_10_23_15_35_00.log': 'log_fsdA',
            'vla_without_timestamp.log': 'log_fsdA',
            'vla_2026_10_23_15_36_00.log': 'log_fsdA',
            'vla_tie_2026_10_23_15_36_00.log': 'log_fsdA',
            'vla_2026_10_23_15_37_00.log': 'log_fsdA',
        }
        names = list(mapping)
        ordered, selected = audit._exact_selection(names, mapping, audit.request_epochs(WINDOW))
        self.assertEqual(ordered[0], 'vla_without_timestamp.log')
        self.assertEqual(set(selected), set(names[:4]))
        self.assertEqual(set(audit._candidate_envelope(['vla_'], mapping, set(), audit.request_epochs(WINDOW))), set(names[:5]))

    def test_retained_audit_cannot_change_window_or_drop_actual_files(self):
        controller, csv = self.make()
        receipt = self.finish(controller, self.initialize(csv))
        for mutate in (
            lambda data: data['initializations'][0].update(record_epoch_range=[0, 1]),
            lambda data: data['extractions'][0]['actual_members'].clear(),
            lambda data: data.update(input_inventory_sha256='f' * 64),
        ):
            changed = copy.deepcopy(receipt)
            mutate(changed)
            with self.assertRaises(audit.RuntimeAuditError):
                audit.validate_runtime_execution(changed, request_time_range=WINDOW,
                                                 inventory=self.inventory, rules=self.rules)

    def test_retained_POSIX_receipt_is_validated_without_host_resolution(self):
        receipt = synthetic_runtime_execution(inventory=self.inventory, request_time_range=WINDOW, rules=self.rules)
        root = '/mnt/d/private-already-cleaned/job/input/perf'
        receipt['flat_log_root'] = root
        for row in receipt['initializations']:
            row['ini_log_path'] = row['record_log_path'] = root
        receipt['final_ini']['log_path'] = root
        self.assertEqual(audit.validate_runtime_execution(receipt, request_time_range=WINDOW,
            inventory=self.inventory, rules=self.rules), receipt)

    def test_retained_windows_receipt_is_validated_without_host_resolution(self):
        receipt = synthetic_runtime_execution(inventory=self.inventory, request_time_range=WINDOW, rules=self.rules)
        root = 'D:/private-already-cleaned/job/input/perf'
        receipt['flat_log_root'] = root
        for row in receipt['initializations']:
            row['ini_log_path'] = row['record_log_path'] = root
        receipt['final_ini']['log_path'] = root
        self.assertEqual(audit.validate_runtime_execution(receipt, request_time_range=WINDOW,
            inventory=self.inventory, rules=self.rules), receipt)

    def on_selected_file_read(self, callback):
        """Inject an actual filesystem event between body read and final stat."""
        real_open = Path.open
        selected = self.flat / next(iter(self.members))
        fired = [False]

        class Reader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, size):
                result = self.stream.read(size)
                if result and not fired[0]:
                    fired[0] = True
                    callback(selected)
                return result

        def opened(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return Reader(stream) if Path(path) == selected and args and args[0] == 'rb' else stream
        return patch.object(Path, 'open', opened), fired

    def test_actual_hardlink_added_during_read_does_not_reject_identical_bytes(self):
        controller, csv = self.make()
        modifier, fired = self.on_selected_file_read(lambda source: os.link(source, self.flat / 'parallel-link.tmp'))
        with modifier:
            record = self.initialize(csv)
        self.assertTrue(fired[0])
        receipt = self.finish(controller, record)
        self.assertEqual(receipt['collector_candidate_members'], self.members)

    def test_actual_content_write_during_hash_is_rejected(self):
        controller, csv = self.make()
        modifier, fired = self.on_selected_file_read(lambda source: source.write_bytes(b'wrong-body-longer'))
        with modifier, self.assertRaisesRegex(audit.RuntimeAuditError, '核验过程中'):
            self.initialize(csv)
        self.assertTrue(fired[0])

    def test_mtime_change_with_increasing_hardlink_count_still_rejects(self):
        controller, csv = self.make()
        def change(source):
            before = source.stat()
            os.link(source, self.flat / 'parallel-link.tmp')
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000_000_000))
        modifier, fired = self.on_selected_file_read(change)
        with modifier, self.assertRaisesRegex(audit.RuntimeAuditError, '核验过程中'):
            self.initialize(csv)
        self.assertTrue(fired[0])

    def test_ctime_change_without_added_link_still_rejects(self):
        controller, csv = self.make()
        selected = self.flat / next(iter(self.members))
        after_read = [False]
        real_stat = Path.stat
        def changed_stat(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            if Path(path) == selected and after_read[0]:
                return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns + 1,
                    st_nlink=value.st_nlink, st_mode=value.st_mode)
            return value
        modifier, fired = self.on_selected_file_read(lambda unused: after_read.__setitem__(0, True))
        with modifier, patch.object(Path, 'stat', changed_stat), self.assertRaisesRegex(audit.RuntimeAuditError, '核验过程中'):
            self.initialize(csv)
        self.assertTrue(fired[0])

    def test_later_ctime_change_misses_cache_and_rehashes(self):
        controller, csv = self.make()
        record = self.initialize(csv)
        selected = self.flat / next(iter(self.members))
        before_count = controller.hash_files
        real_stat = Path.stat
        def changed_stat(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            if Path(path) == selected:
                return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns + 1,
                    st_nlink=value.st_nlink, st_mode=value.st_mode)
            return value
        with patch.object(Path, 'stat', changed_stat):
            checksum, size = controller._hash_file(selected)
        self.assertEqual(controller.hash_files, before_count + 1)
        self.assertEqual({'sha256': checksum, 'bytes': size, 'source': self.members[selected.name]['source']},
                         self.members[selected.name])


if __name__ == '__main__':
    unittest.main()
