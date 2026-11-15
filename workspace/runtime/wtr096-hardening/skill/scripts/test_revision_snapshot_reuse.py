"""Revision-bound task-local reuse never skips final full readback or governance."""
from collections import Counter
import unittest
from unittest.mock import patch

import native_sheet_delivery as delivery
from request_model import InputError
import test_native_sheet_delivery as fixtures


class RevisionSnapshotTests(unittest.TestCase):
    setUp = fixtures.DeliveryTests.setUp
    run_delivery = fixtures.DeliveryTests.run_delivery

    def transport(self, *, missing=False, mutation=None):
        self.settings['native_sheet_revision_guard'] = True
        revisions = Counter()
        reads = Counter()
        fake = self.fake
        def invoke(settings, args, **options):
            token = fake.flag(args, '--spreadsheet-token') if '--spreadsheet-token' in args else None
            if args[:2] == ['sheets', '+revision-get']:
                self.assertEqual(options['profile'], 'report-bot')
                self.assertEqual(fake.flag(args, '--as'), 'bot')
                reads[token] += 1
                if mutation is not None:
                    mutation(token, reads[token], revisions)
                return {'ok': True, 'identity': 'bot', 'data': {'revision': None if missing else revisions[token]}}
            try:
                return fake(settings, args, **options)
            finally:
                if args[:2] == ['sheets', '+cells-set']:
                    revisions[token] += 1
        return patch.object(delivery.policy, 'run_lark', side_effect=invoke), reads

    def test_stable_versions_save_48_calls_but_keep_all_final_cells_and_permissions(self):
        manager, revisions = self.transport()
        with manager:
            result = self.run_delivery()
        self.assertEqual(self.fake.counts['copy'], 2)
        self.assertEqual(self.fake.counts['write'], 2)
        self.assertEqual(self.fake.counts['layout'], 42)  # Legacy: 70.
        self.assertEqual(self.fake.counts['get'], 42)  # Full source, copy and final snapshots per role.
        self.assertEqual(sum(revisions.values()), 12)
        self.assertEqual(len(self.governance), 6)
        for value in result.values():
            self.assertEqual(value['validation']['verified_cells'], 196)
            self.assertTrue(value['validation']['all_values_formulas_blanks_styles_and_layout_verified'])
            self.assertEqual(value['permissions']['secure_label']['name'], 'L3')

    def test_unavailable_version_keeps_full_legacy_reads(self):
        manager, _ = self.transport(missing=True)
        with manager:
            self.run_delivery()
        self.assertEqual(self.fake.counts['layout'], 70)
        self.assertEqual(self.fake.counts['get'], 70)

    def test_template_change_before_copy_is_not_hidden_by_saved_snapshot(self):
        def mutate(token, count, revisions):
            if token == 'templateSynthetic' and count == 3:
                self.fake.books[token]['sheets'][0]['layout']['row_heights']['1'] = 99
                revisions[token] += 1
        manager, _ = self.transport(mutation=mutate)
        with manager, self.assertRaisesRegex(InputError, '模板.*更新'):
            self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_manual_change_before_write_is_not_overwritten(self):
        def mutate(token, count, revisions):
            if token.startswith('copy') and count == 3:
                self.fake.books[token]['sheets'][0]['cells'][0][0]['style']['number_format'] = '@'
                revisions[token] += 1
        manager, _ = self.transport(mutation=mutate)
        with manager, self.assertRaisesRegex(InputError, '格式'):
            self.run_delivery()
        self.assertNotIn('write', self.fake.counts)

    def test_completed_tables_are_fully_reverified_and_never_rewritten(self):
        manager, _ = self.transport()
        with manager:
            self.run_delivery()
            reads = self.fake.counts['get']
            writes = self.fake.counts['write']
            self.run_delivery()
        self.assertEqual(self.fake.counts['get'] - reads, 14)
        self.assertEqual(self.fake.counts['write'], writes)

    def test_revision_proof_rejects_bool_wrong_token_and_unstable_values(self):
        proof = {'token': 'one', 'revision_guard': {'schema': 'native-snapshot-revision-v1',
                 'token': 'one', 'before': 0, 'after': 0}}
        self.assertEqual(delivery._stable_revision(proof, 'one'), 0)
        self.assertIsNone(delivery._stable_revision(proof, 'other'))
        for before, after in [(False, False), (0, 1), (-1, -1), ('0', '0')]:
            proof['revision_guard'].update(before=before, after=after)
            self.assertIsNone(delivery._stable_revision(proof, 'one'))


if __name__ == '__main__':
    unittest.main()
