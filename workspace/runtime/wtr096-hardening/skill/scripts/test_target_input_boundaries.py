"""Offline VIN and local-range boundaries; no settings, CLI or worker IO."""
from copy import deepcopy
import hashlib
import unittest

import walkthrough_route as routing
from request_model import InputError, parse_comparison_request


VIN = 'DEM0VEH1CLEX00001'
OTHER_VIN = 'DEM0VEH1CLEX00002'
ANCHOR = '2026-10-29T12:00:00+08:00'
FULL = ('2026-10-27 08:00 - 2026-10-27 09:00；'
        '2026-10-28 10:00 - 2026-10-28 11:00')
SHORT = '2026-10-27 08:00-09:00；2026-10-28 10:00-11:00'


class VinBoundaries(unittest.TestCase):
    def decision(self, vehicles):
        return routing.route('走查测试 '+vehicles+'；'+FULL, received_at=ANCHOR)

    def test_labeled_vin_does_not_hide_other_bare_vin(self):
        for vehicles in (f'VIN {VIN}，另一车辆 {OTHER_VIN}',
                         f'另一车辆 {OTHER_VIN}，VIN：{VIN}',
                         f'VIN {VIN}，VIN {OTHER_VIN}',
                         f'{VIN}，{OTHER_VIN}'):
            with self.subTest(vehicles=vehicles):
                decision = self.decision(vehicles)
                self.assertEqual(decision['status'], 'needs_input')
                self.assertIn('多个 VIN', decision['message'])

    def test_repeated_same_vin_is_deduplicated_case_insensitively(self):
        decision = self.decision(f'VIN码：{VIN.lower()}，车辆{VIN}，VIN号={VIN}')
        self.assertEqual(decision['status'], 'ready', decision)
        self.assertEqual(decision['normalized']['vin'], VIN)

    def test_labeled_vin_requires_exact_length_and_alphabet(self):
        invalid = ['ABCDE', VIN[:-1], VIN+'0', VIN.replace('X','I'),
                   VIN.replace('X','O'), VIN.replace('X','Q'), VIN[:-1]+'_',
                   VIN[:-1]+'-', '1234']
        for value in invalid:
            with self.subTest(value=value):
                decision = self.decision('VIN '+value)
                self.assertEqual(decision['status'], 'needs_input', decision)
                self.assertIn('17 位', decision['message'])
                self.assertEqual(decision['missing'], ['VIN'])

    def test_invalid_explicit_value_not_replaced_by_valid_bare_vin(self):
        self.assertEqual(self.decision('VIN ABCDE，'+VIN)['status'], 'needs_input')

    def test_illegal_bare_candidate_not_ignored_beside_labeled_vin(self):
        for illegal in (OTHER_VIN.replace('X','I'), OTHER_VIN.replace('X','O'),
                        OTHER_VIN.replace('X','Q')):
            with self.subTest(illegal=illegal):
                self.assertEqual(self.decision('VIN '+VIN+'，'+illegal)['status'], 'needs_input')

    def test_parser_itself_rejects_both_vins(self):
        with self.assertRaises(InputError):
            parse_comparison_request('VIN '+VIN+'，'+OTHER_VIN+' '+FULL)

    def test_missing_vin_remains_missing(self):
        decision = self.decision('')
        self.assertEqual(decision['status'], 'needs_input')
        self.assertIn('VIN', decision['missing'])


class CompactLocalTimeBoundaries(unittest.TestCase):
    def decision(self, times, *, interpretation=None):
        return routing.route('走查测试 '+VIN+' '+times, received_at=ANCHOR,
                             interpretation=interpretation)

    def test_short_local_ranges_keep_endpoints_and_original_audit(self):
        decision = self.decision(SHORT)
        self.assertEqual(decision['status'], 'ready', decision)
        request = decision['normalized']
        self.assertEqual(request['baseline_metadata']['start'], '2026-10-27T08:00:00')
        self.assertEqual(request['baseline_metadata']['end'], '2026-10-27T09:00:00')
        self.assertEqual(request['replacement_metadata']['start'], '2026-10-28T10:00:00')
        self.assertEqual(request['replacement_metadata']['end'], '2026-10-28T11:00:00')
        original = '走查测试 '+VIN+' '+SHORT
        self.assertEqual(request['normalization']['original_text'], original)
        self.assertEqual(request['normalization']['source_sha256'], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(sum(c['kind']=='range_completion' for c in request['normalization']['changes']), 2)

    def test_short_ranges_with_seconds(self):
        decision = self.decision('2026-10-27 08:00:01-09:00:02；2026-10-28 10:00:03-11:00:04')
        self.assertEqual(decision['status'], 'ready', decision)
        self.assertEqual(decision['normalized']['baseline_metadata']['end'], '2026-10-27T09:00:02')

    def test_compact_ranges_match_explicit_full_ranges(self):
        short = self.decision(SHORT)['normalized']
        full = self.decision(FULL)['normalized']
        for key in ('baseline_metadata','replacement_metadata','comparison_key','input_roles'):
            self.assertEqual(short[key], full[key])

    def test_reversed_local_end_does_not_infer_tomorrow(self):
        self.assertEqual(self.decision('2026-10-27 23:00-01:00；2026-10-28 10:00-11:00')['status'], 'needs_input')

    def test_illegal_clock_and_calendar_remain_rejected(self):
        for times in ('2026-10-27 08:00-09:99；2026-10-28 10:00-11:00',
                      '2026-02-30 08:00-09:00；2026-10-28 10:00-11:00'):
            with self.subTest(times=times):
                self.assertEqual(self.decision(times)['status'], 'needs_input')

    def test_roles_remain_source_order_or_explicit(self):
        request = self.decision('PCM更新：2026-10-28 10:00-11:00；Base：2026-10-27 08:00-09:00')['normalized']
        self.assertEqual(request['input_roles'], ['replacement','baseline'])
        self.assertEqual(request['baseline_metadata']['start'], '2026-10-27T08:00:00')

    def test_true_iso_offsets_keep_instant_for_both_date_separators(self):
        for separator in ('T',' '):
            for offset, expected in (('-05:00','21:00:00'), ('+08:00','08:00:00'), ('Z','16:00:00')):
                with self.subTest(separator=separator, offset=offset):
                    times = (f'2026-10-27{separator}08:00{offset} - 2026-10-27{separator}09:00{offset}；'
                             f'2026-10-28{separator}08:00{offset} - 2026-10-28{separator}09:00{offset}')
                    decision = self.decision(times)
                    self.assertEqual(decision['status'], 'ready', decision)
                    self.assertEqual(decision['normalized']['baseline_metadata']['start'], '2026-10-27T'+expected)
                    self.assertFalse(any(c['kind']=='range_completion' for c in decision['normalized']['normalization']['changes']))

    def test_isolated_t_iso_instants_do_not_become_two_ranges(self):
        self.assertEqual(self.decision('2026-10-27T08:00-09:00；2026-10-28T10:00-11:00')['status'], 'needs_input')

    def test_mixed_offset_and_naive_overlap_rejected(self):
        times = ('2026-10-27 00:00Z - 2026-10-27 01:00Z；'
                 '2026-10-27 08:30 - 2026-10-27 09:30')
        self.assertEqual(self.decision(times)['status'], 'needs_input')

    def test_invalid_offset_and_fractional_second_not_repaired(self):
        for times in ('2026-10-27T08:00-08:99 - 2026-10-27T09:00-08:99；2026-10-28 10:00-11:00',
                      '2026-10-27T08:00:00.5Z - 2026-10-27T09:00Z；2026-10-28 10:00-11:00'):
            with self.subTest(times=times):
                self.assertEqual(self.decision(times)['status'], 'needs_input')

    def test_undated_clock_ranges_do_not_assume_today(self):
        self.assertEqual(self.decision('08:00-09:00；10:00-11:00')['status'], 'needs_input')

    def test_shared_normalizer_and_validator_globals_not_mutated(self):
        shared = routing._shared_input_module('semantic_input')
        before = shared.normalize_input
        self.decision(SHORT)
        self.assertIs(shared.normalize_input, before)
        result = routing._natural_input_module().normalize_input(SHORT)
        self.assertNotIn('2026-10-27 09:00', result['text'])


class InterpretationBoundaries(unittest.TestCase):
    def setUp(self):
        self.first = '2026-10-27 08:00-09:00'
        self.second = '2026-10-28 10:00-11:00'
        self.text = f'走查测试 {VIN}，Base：{self.first}；PCM更新：{self.second}'
        self.proposal = {'source_sha256':hashlib.sha256(self.text.encode()).hexdigest(),
            'received_at':ANCHOR,'windows':[
                {'quote':self.first,'start':'2026-10-27T08:00:00+08:00','end':'2026-10-27T09:00:00+08:00'},
                {'quote':self.second,'start':'2026-10-28T10:00:00+08:00','end':'2026-10-28T11:00:00+08:00'}]}

    def decision(self, proposal):
        return routing.route(self.text, received_at=ANCHOR, interpretation=proposal)

    def test_original_quote_and_hash_are_validated_with_local_range_normalization(self):
        decision = self.decision(self.proposal)
        self.assertEqual(decision['status'], 'ready', decision)
        self.assertEqual(decision['normalized']['normalization']['original_text'], self.text)
        self.assertEqual(decision['normalized']['normalization']['interpretation'], self.proposal)

    def test_hash_clock_and_candidate_drift_remain_rejected(self):
        candidates = []
        proposal = deepcopy(self.proposal); proposal['source_sha256'] = '0'*64; candidates.append(proposal)
        proposal = deepcopy(self.proposal); proposal['received_at'] = '2026-10-30T12:00:00+08:00'; candidates.append(proposal)
        proposal = deepcopy(self.proposal); proposal['windows'][0]['start'] = '2026-10-27T08:01:00+08:00'; candidates.append(proposal)
        proposal = deepcopy(self.proposal); proposal['vin'] = OTHER_VIN; candidates.append(proposal)
        for proposal in candidates:
            with self.subTest(proposal=proposal):
                self.assertEqual(self.decision(proposal)['status'], 'needs_input')

    def test_role_and_identity_quote_rewrite_remain_rejected(self):
        for quote in ('Base：'+self.first, VIN+'，Base：'+self.first):
            proposal = deepcopy(self.proposal); proposal['windows'][0]['quote'] = quote
            self.assertEqual(self.decision(proposal)['status'], 'needs_input')

    def test_quote_order_and_overlap_remain_rejected(self):
        proposal = deepcopy(self.proposal); proposal['windows'].reverse()
        self.assertEqual(self.decision(proposal)['status'], 'needs_input')
        proposal = deepcopy(self.proposal); proposal['windows'][1] = deepcopy(proposal['windows'][0])
        self.assertEqual(self.decision(proposal)['status'], 'needs_input')

    def test_shared_validator_global_still_unchanged_after_interpretation(self):
        shared = routing._shared_input_module('semantic_input'); before = shared.normalize_input
        self.decision(self.proposal)
        self.assertIs(shared.normalize_input, before)


if __name__ == '__main__':
    unittest.main()
