"""Target governance and fixed-reference publishing stay compatible offline."""
import copy
import unittest
from unittest.mock import patch

import native_sheet_delivery as delivery
import test_template_reference_reader as reader


class TargetTemplatePublisherCompatibilityTests(unittest.TestCase):
    setUp = reader.TemplateReferenceReaderTests.setUp
    save_manifest = reader.TemplateReferenceReaderTests.save_manifest
    run_delivery = reader.TemplateReferenceReaderTests.run_delivery
    assert_no_real_io = reader.TemplateReferenceReaderTests.assert_no_real_io

    def test_fixed_user_source_keeps_requester_governance_and_persisted_audit(self):
        grants = []
        expected = {'chat_mode': 'p2p', 'member_type': 'openid',
                    'member_id': self.record['sender_id'], 'perm': 'view'}

        def grant(settings, url, source_chat, requester):
            self.assertEqual(source_chat, self.record['source_chat_id'])
            self.assertEqual(requester, self.record['sender_id'])
            self.assertNotIn(delivery._sheet_token(url), ('templateSynthetic', 'relaySynthetic'))
            grants.append(url)
            return copy.deepcopy(expected)

        with patch.object(delivery.policy, 'grant_source_chat_view', side_effect=grant):
            output = self.run_delivery()
        self.assertEqual(self.record['native_sheets']['version'], delivery.FIXED_USER_VERSION)
        self.assertEqual(len(grants), 2)
        self.assertEqual(len(self.owner_calls), 2)
        for role in delivery.ROLES:
            self.assertEqual(output[role]['permissions']['source_chat_access'], expected)
            saved = self.record['native_sheets']['roles'][role]
            self.assertEqual(saved['permissions']['source_chat_access'], expected)
            self.assertTrue(saved['permissions']['source_chat_view'])
            self.assertTrue(saved['permissions']['owner_transfer'])
        self.assert_no_real_io()


if __name__ == '__main__':
    unittest.main()
