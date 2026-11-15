"""Offline evidence for parsed-empty versus unparsed report fields."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import direct_ad as ad
import native_log_source as native
import state_performance as pcm


VIN = 'DEM0VEH1CLE000001'
META = {'vin': VIN, 'start': '2026-10-21T14:00:00', 'end': '2026-10-21T14:10:00'}
TARGETS = ('header_delay', 'vla_parking_header_delay', 'VlaModel_FPS', 'VlaParkingDLM_FPS')


def rules(expression='var = "" $10 "," find_digit($7);max_threshold=1000'):
    return {'fixture': {name: {'func': 'custom', 'calculate_type': '', 'ps_type': 'ps_time',
        'soc_type': 'synthetic_container', 'log_tag': 'SYNTHETIC', 'input': expression} for name in TARGETS}}


class ParserFieldPresenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def ad_input(self, value='5.0', state='state:0', timestamp='14:01:00.000', tag='SYNTHETIC'):
        root = self.root / 'ad' / 'input'
        root.mkdir(parents=True)
        path = root / 'synthetic_2026_10_21_14_10_00.zip'
        line = f'10-21 {timestamp} 42 x x x {state} x x {value} {tag}\n'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(f'FSDA_M100-{VIN}-2026_10_21_14_10_00.log', 'synthetic identity')
            archive.writestr('synthetic_container_2026_10_21_14_10_00.log', line * 20)
        manifest = {'request': META, 'environment': 'testtwo', 'source_policy': native.SOURCE_POLICY,
            'archive_query': native.archive_query_metadata(META), 'counts': {'log_fsdA': 1, 'log_fsdB': 0},
            'bytes': path.stat().st_size, 'files': [{'relative_path': path.name, 'sha256': ad.digest(path)}]}
        (root / 'download-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return root, manifest

    def assert_ad_presence(self, result, status, observations):
        self.assertEqual(set(result['field_presence']), {name.casefold() for name in TARGETS})
        for evidence in result['field_presence'].values():
            self.assertEqual(evidence['status'], status)
            self.assertEqual(evidence['observations'], observations)

    def test_ad_numeric_fields_remain_parsed_when_threshold_removes_all_values(self):
        root, _ = self.ad_input()
        result = ad.analyze(root, META, rules('var = "" $10 "," find_digit($7);max_threshold=1'))
        self.assert_ad_presence(result, 'parsed', 20)
        self.assertEqual(result['records'][0]['statistics']['P99'], '-')
        self.assertIn('阈值过滤后无数据', result['records'][0]['statistics']['diagnostic'])

    def test_ad_explicit_empty_value_is_parsed_without_a_numeric_value(self):
        root, _ = self.ad_input(value='-')
        result = ad.analyze(root, META, rules())
        self.assert_ad_presence(result, 'parsed', 20)
        self.assertEqual(result['field_presence']['header_delay']['numeric_observations'], 0)
        self.assertEqual(result['field_presence']['header_delay']['empty_observations'], 20)
        self.assertEqual(result['records'][0]['statistics']['P99'], '-')

    def test_ad_tag_without_required_value_column_is_unparsed(self):
        root, _ = self.ad_input()
        result = ad.analyze(root, META, rules('var = "" $999 "," find_digit($7)'))
        self.assertTrue(all(sample['rows'] == 20 for sample in result['samples'].values()))
        self.assert_ad_presence(result, 'unparsed', 0)

    def test_ad_find_digit_failure_does_not_count_as_field_presence(self):
        root, _ = self.ad_input(value='unknown')
        result = ad.analyze(root, META, rules('var = "" find_digit($10) "," find_digit($7)'))
        self.assert_ad_presence(result, 'unparsed', 0)

    def test_ad_missing_state_extraction_does_not_count_as_presence(self):
        root, _ = self.ad_input(state='unknown')
        self.assert_ad_presence(ad.analyze(root, META, rules()), 'unparsed', 0)

    def test_ad_only_outside_window_values_are_not_in_window_evidence(self):
        root, _ = self.ad_input(timestamp='14:11:00.000')
        self.assert_ad_presence(ad.analyze(root, META, rules()), 'unparsed', 0)

    def test_ad_missing_tag_is_not_a_parsed_field(self):
        root, _ = self.ad_input(tag='UNRELATED')
        self.assert_ad_presence(ad.analyze(root, META, rules()), 'unparsed', 0)

    def test_native_cache_preserves_new_evidence_but_does_not_invent_old_evidence(self):
        # Cache identity uses the complete native boundary fixture; the actual
        # CSV observation logic has its own full-runtime evidence regressions.
        from copy import deepcopy
        from test_native_cache_identity import NativeCacheIdentityTests
        from runtime_audit import canonical_sha
        from m100_attachment_worker import TARGET_METRICS
        from request_model import InputError
        fixture = NativeCacheIdentityTests('test_verified_cache_preserves_nulls_and_never_reanalyzes')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runtime = fixture.report['runtime_execution']
        evidence = {}
        for name in TARGET_METRICS:
            count = 20 if name == 'header_delay' else 0
            row = {'status':'parsed' if count else 'unparsed', 'observations':count,
                'numeric_observations':count, 'empty_observations':0,
                'scope':'requested_window', 'evidence':'audited_native_target_csv_value_and_state',
                'request_time_range':runtime['request_time_range'],
                'rules_sha256':runtime['rules_sha256'],
                'runtime_execution_sha256':canonical_sha(runtime),
                'csv_files':([{'relative_path':'logs_traned/synthetic/1+header_delay.csv',
                    'bytes':20, 'sha256':canonical_sha('synthetic completed-process CSV')}]
                    if count else []), 'scan_complete':True}
            binding = next((entry for entry in fixture.report['statistics']
                            if str(entry.get('metric','')).casefold() == name), None)
            if binding is not None:
                row['rule_sha256'] = canonical_sha({'theme':binding['theme'],
                    'metric':binding['metric'], 'rule':binding['rule']})
            row['evidence_sha256'] = canonical_sha(row)
            evidence[name] = row
        fixture.report['field_presence'] = evidence
        fixture.report['coverage'] = {'field_presence':deepcopy(evidence)}
        fixture.cache['field_presence'] = deepcopy(evidence)
        fixture.rewrite_detail_with_valid_hashes()
        settings = {'cloud_settings_path': 'unused', 'local_analysis': {
            'backend':'native_full', 'execution':'wsl', 'distribution':'Synthetic',
            'python':'/venv/python', 'analyzer_root':'/latest', 'environment':'test',
            'rules_json':str(fixture.rules), 'keep_raw_logs':True}}
        result = native.analyze_window(fixture.metadata, settings, fixture.root, fixture.rules)
        self.assertEqual(result['field_presence']['header_delay']['observations'], 20)
        self.assertEqual(fixture.read()['field_presence'], result['field_presence'])
        del fixture.cache['field_presence']
        fixture.save()
        with self.assertRaises(InputError):
            fixture.read()
        del fixture.report['field_presence']
        del fixture.report['coverage']['field_presence']
        fixture.rewrite_detail_with_valid_hashes()
        self.assertNotIn('field_presence', fixture.read())

    def pcm_result(self, *records):
        root = self.root / 'pcm'
        root.mkdir()
        (root / 'AVM_Service-synthetic').write_text(''.join(records), encoding='utf-8')
        return pcm.extract(root, META['start'], META['end'])

    @staticmethod
    def pcm_line(milliseconds, text):
        seconds, millis = divmod(milliseconds, 1000)
        return f'+0800 2026-10-21-14_01_{seconds:02d}_{millis:03d} 42 9 I infer_expert_pcm {text}\n'

    def test_pcm_preset_metric_keys_do_not_prove_parsing(self):
        result = self.pcm_result(self.pcm_line(0, 'unrelated record'))
        for evidence in result['field_presence'].values():
            self.assertEqual(evidence['status'], 'unparsed')
            self.assertEqual(evidence['observations'], 0)

    def test_pcm_cpu_fields_are_independent_even_without_complete_cpu_samples(self):
        result = self.pcm_result(self.pcm_line(0, 'infer_expert_pc_cpu=38.0%'))
        self.assertEqual(result['field_presence']['pcm_cpu_percent']['status'], 'parsed')
        self.assertEqual(result['field_presence']['system_cpu_percent']['status'], 'unparsed')
        self.assertEqual(result['coverage']['complete_cpu_samples'], 0)
        self.assertEqual(result['metrics']['slow_driver']['pcm_cpu_percent']['count'], 0)

    def test_pcm_both_cpu_fields_can_be_observed_on_separate_rows(self):
        result = self.pcm_result(self.pcm_line(0, 'infer_expert_pc_cpu=38.0%'),
            self.pcm_line(1, 'sys_cpu=50.0%'))
        self.assertEqual(result['field_presence']['pcm_cpu_percent']['observations'], 1)
        self.assertEqual(result['field_presence']['system_cpu_percent']['observations'], 1)
        self.assertEqual(result['coverage']['complete_cpu_samples'], 0)

    def test_pcm_fps_field_is_parsed_before_failed_frame_attribution(self):
        result = self.pcm_result(self.pcm_line(0, 'FPSlast10frames: 8.0 Hz'))
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['status'], 'parsed')
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['numeric_samples'], 0)
        self.assertEqual(result['coverage']['fps_samples_unattributed'], 1)

    def test_pcm_invalid_fps_token_does_not_prove_parsing(self):
        result = self.pcm_result(self.pcm_line(0, 'FPSlast10frames: unknown Hz'))
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['status'], 'unparsed')

    def test_pcm_complete_latency_chain_is_parsed_before_ctx_validation(self):
        result = self.pcm_result(self.pcm_line(0, 'UroadPreNode::run() frame=1 perception_state=0 ctx=1'),
            self.pcm_line(100, 'UroadPreNode::run() timing(ms) frame=1 ctx=1'),
            self.pcm_line(200, 'UroadPostNode::run() timing(ms) frame=2 ctx=2'))
        self.assertEqual(result['field_presence']['pcm_p99_infer_delay_ms']['status'], 'parsed')
        self.assertEqual(result['field_presence']['pcm_p99_infer_delay_ms']['numeric_samples'], 0)
        self.assertEqual(result['frame_diagnostics']['ctx_mismatch'], 1)

    def test_pcm_isolated_latency_tag_does_not_prove_derived_field(self):
        result = self.pcm_result(self.pcm_line(0, 'UroadPreNode::run() frame=1 perception_state=0 ctx=1'))
        self.assertEqual(result['field_presence']['pcm_p99_infer_delay_ms']['status'], 'unparsed')

    def test_pcm_existing_numeric_values_are_unchanged(self):
        result = self.pcm_result(self.pcm_line(0, 'UroadPreNode::run() frame=1 perception_state=0 ctx=1'),
            self.pcm_line(100, 'UroadPreNode::run() timing(ms) frame=1 ctx=1'),
            self.pcm_line(200, 'UroadPostNode::run() timing(ms) frame=2 ctx=1'),
            self.pcm_line(300, 'perception_state=0 infer_expert_pc_cpu=38.0% sys_cpu=50.0%'))
        row = result['metrics']['slow_driver']
        self.assertEqual(row['pcm_p99_infer_delay_ms'], {'p99': 200.0, 'count': 1})
        self.assertEqual(row['pcm_cpu_percent'], {'mean': 2.0, 'count': 1})
        self.assertEqual(row['system_cpu_percent'], {'mean': 50.0, 'count': 1})


if __name__ == '__main__':
    unittest.main()
