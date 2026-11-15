"""Offline checks of field evidence taken from the actual full-native CSVs."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import m100_attachment_worker as worker
import native_log_source as native
from request_model import InputError
from runtime_audit import canonical_sha


class TargetNativePresenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.window = '20261021140000/20261021141000'
        zone = timezone(timedelta(hours=8))
        self.epochs = [datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=zone).timestamp()
                       for value in self.window.split('/')]
        self.rules = {'synthetic': {name: {'func':'custom', 'soc_type':'synthetic_container',
            'ps_type':'ps_time', 'calculate_type':'', 'input':'synthetic exact expression'}
            for name in worker.TARGET_METRICS}}
        self.runtime = {'status':'verified', 'request_time_range':self.window,
            'expected_epoch_range':self.epochs, 'flat_log_root':str(self.root / 'perf'),
            'rules_sha256':canonical_sha(self.rules), 'extractions':[{'theme':'synthetic'}]}
        self.folder = self.root / 'perf' / 'logs_traned' / 'synthetic'
        self.folder.mkdir(parents=True)

    def csv(self, value='5.0', state='0', *, stamp=None, metric='header_delay', raw=None):
        path = self.folder / ('1+' + metric + '.csv')
        if raw is None:
            raw = f'10-21 14:01:00.000 42 {stamp or self.epochs[0]+60} {value},{state}\n'.encode()
        path.write_bytes(raw)
        return path

    def collect(self):
        return worker.collect_coverage(self.root, self.window,
            runtime_execution=self.runtime, rules=self.rules)['field_presence']

    def test_numeric_and_explicit_empty_values_are_distinct_proven_observations(self):
        self.csv()
        self.csv(value='-', metric='vla_parking_header_delay')
        data = self.collect()
        self.assertEqual(data['header_delay']['numeric_observations'], 1)
        self.assertEqual(data['vla_parking_header_delay']['empty_observations'], 1)
        self.assertEqual(data['header_delay']['status'], 'parsed')
        self.assertEqual(data['vla_parking_header_delay']['status'], 'parsed')
        self.assertEqual(data['vlamodel_fps']['status'], 'unparsed')
        self.assertEqual(data['header_delay']['csv_files'][0]['sha256'],
                         native.digest(self.folder / '1+header_delay.csv'))

    def test_tag_hit_missing_value_or_unknown_token_does_not_prove_field(self):
        for value in ('', 'unknown', 'cost:5.0', 'infinity'):
            with self.subTest(value=value):
                self.csv(value=value)
                self.assertEqual(self.collect()['header_delay']['observations'], 0)

    def test_missing_or_invalid_state_does_not_prove_field(self):
        for state in ('', 'unknown', 'NaN', 'state:0'):
            with self.subTest(state=state):
                self.csv(state=state)
                self.assertEqual(self.collect()['header_delay']['status'], 'unparsed')

    def test_outside_window_and_nonfinite_time_do_not_prove_field(self):
        for stamp in (self.epochs[1] + 1, float('nan'), float('inf')):
            with self.subTest(stamp=stamp):
                self.csv(stamp=stamp)
                self.assertEqual(self.collect()['header_delay']['observations'], 0)

    def test_invalid_encoding_and_incomplete_columns_do_not_prove_field(self):
        for raw in (b'10-21 14:01:00.000\n',
                    f'10-21 14:01:00.000 42 {self.epochs[0]+1} 5.0,0 '.encode() + b'\xff\n'):
            with self.subTest(raw=raw):
                self.csv(raw=raw)
                self.assertEqual(self.collect()['header_delay']['status'], 'unparsed')

    def test_other_theme_csv_does_not_prove_exact_rule_field(self):
        other = self.folder.parent / 'other'
        other.mkdir()
        path = self.csv()
        path.rename(other / path.name)
        self.assertEqual(self.collect()['header_delay']['status'], 'unparsed')

    def test_unknown_runtime_theme_does_not_prove_field(self):
        self.csv()
        self.runtime['extractions'] = []
        evidence = self.collect()['header_delay']
        self.assertEqual(evidence['status'], 'unparsed')
        self.assertFalse(evidence['scan_complete'])

    def test_window_rules_and_runtime_root_are_bound(self):
        self.csv()
        original = copy.deepcopy(self.runtime)
        for field, value in (('request_time_range','20261021150000/20261021151000'),
                             ('rules_sha256','0'*64), ('flat_log_root',str(self.root / 'other'))):
            with self.subTest(field=field):
                self.runtime = {**original, field:value}
                with self.assertRaises(ValueError):
                    self.collect()

    def test_duplicate_target_csv_is_rejected(self):
        self.csv()
        (self.folder / '2+header_delay.csv').write_text('', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.collect()

    def test_top_level_evidence_matches_full_report_and_rejects_altered_count(self):
        self.csv()
        presence = self.collect()
        report = {'field_presence':presence, 'coverage':{'field_presence':copy.deepcopy(presence)},
                  'runtime_execution':self.runtime,
                  'statistics':[{'theme':theme,'metric':name,'rule':rule}
                      for theme,rows in self.rules.items() for name,rule in rows.items()]}
        self.assertEqual(native._validated_field_presence(report), presence)
        report['field_presence']['header_delay']['observations'] = 50
        with self.assertRaises(InputError):
            native._validated_field_presence(report)

    def test_evidence_binding_rejects_altered_runtime_after_collection(self):
        self.csv()
        presence = self.collect()
        report = {'field_presence':presence, 'coverage':{'field_presence':presence},
                  'runtime_execution':{**self.runtime, 'status':'other'}}
        with self.assertRaises(InputError):
            native._validated_field_presence(report)

    def test_legacy_report_without_evidence_does_not_invent_presence(self):
        self.assertIsNone(native._validated_field_presence({'coverage':{'targets':{}}}))


if __name__ == '__main__':
    unittest.main()
