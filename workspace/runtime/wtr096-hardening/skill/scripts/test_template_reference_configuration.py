"""Synthetic configuration boundaries; never invokes a real CLI or auth store."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import configure_walkthrough as configure
import preflight
from request_model import InputError
import test_product_config as product_tests


class TemplateReferenceConfigurationTests(unittest.TestCase):
    setUp = product_tests.ProductConfigTests.setUp
    args = product_tests.ProductConfigTests.args

    def test_default_configuration_preserves_original_bot_shape(self):
        args = self.args()
        configure.configure(args)
        delivery = json.loads(args.output.read_text(encoding='utf-8'))['report_delivery']
        self.assertEqual(delivery, {'format': 'native_sheets', 'include_images': False})

    def test_fixed_user_is_explicit_and_does_not_change_report_bot_or_requester_policy(self):
        manifest = self.root/'workspace/data/walkthrough-template-bridge/ready.json'
        args = self.args(native_template_reference_reader='fixed_user',
                         native_template_token='syntheticRelay',
                         native_template_bridge_manifest=manifest)
        with patch.object(preflight.policy, 'run_lark', side_effect=AssertionError('no cloud operations')):
            configure.configure(args)
        settings = json.loads(args.output.read_text(encoding='utf-8'))
        self.assertEqual(settings['report_lark_profile'], self.identity['report_lark_profile'])
        self.assertEqual(settings['lark_profile'], self.identity['lark_profile'])
        self.assertEqual(settings['report_delivery']['native_template_reference_reader'], 'fixed_user')
        self.assertEqual(settings['report_delivery']['native_template_bridge_manifest'], str(manifest))
        self.assertFalse(manifest.exists())  # configuration never invents a ready receipt

    def test_invalid_reference_options_fail_before_writing_configuration(self):
        cases = (
            {'native_template_reference_reader': 'fixed_user'},
            {'native_template_reference_reader': 'fixed_user', 'native_template_token': 'syntheticRelay'},
            {'native_template_reference_reader': 'fixed_user', 'native_template_token': 'syntheticRelay',
             'native_template_bridge_manifest': Path('relative/ready.json')},
            {'native_template_reference_reader': 'bot',
             'native_template_bridge_manifest': self.root/'workspace/data/walkthrough-template-bridge/ready.json'},
            {'native_template_reference_reader': 'unexpected'},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                args = self.args(output=self.root/f'bridge-invalid-{index}'/'settings.json', **case)
                with self.assertRaises((InputError, ValueError)):
                    configure.configure(args)
                self.assertFalse(args.output.parent.exists())

    def test_local_preflight_does_not_claim_cloud_template_acceptance(self):
        settings = {'report_delivery': {
            'format': 'native_sheets', 'include_images': False,
            'native_template_reference_reader': 'fixed_user',
            'native_template_spreadsheet_token': 'syntheticRelay',
            'native_template_bridge_manifest': str(self.root/'workspace/data/walkthrough-template-bridge/ready.json')}}
        with patch.object(preflight.policy, 'run_lark', side_effect=AssertionError('no cloud operations')):
            result = preflight.check_delivery_configuration(settings)
        self.assertEqual(result['template_reference_reader'], 'fixed_user')
        self.assertIs(result['template_bridge_online_verified'], False)
        self.assertIs(result['online_permissions_verified'], False)


if __name__ == '__main__':
    unittest.main()
