"""Synthetic configuration and read-only identity preflight; no live CLI calls."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configure_walkthrough as configure
import preflight
from request_model import InputError


PRODUCT = {
    'query_chat_id':'oc_syntheticquery', 'asgard_bot_id':'ou_syntheticAsgard',
    'timeout_seconds':120, 'poll_interval_seconds':2,
}


def pinned_identity():
    return {
        'appId':'cli_syntheticProject',
        'identities':{
            'user':{
                'available':True, 'openId':'ou_syntheticOperator', 'tokenStatus':'valid',
                'expiresAt':(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            },
            'bot':{'available':True},
        },
    }


class ProductConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = {
            'lark_cli':str(self.root/'lark-cli.exe'), 'lark_home':str(self.root/'identity-home'),
            'project_id':'synthetic', 'lark_profile':'synthetic-user', 'report_lark_profile':'synthetic-report',
            'expected_lark_app_id':'cli_syntheticProject', 'expected_operator_open_id':'ou_syntheticOperator',
            'source_chat_id':'oc_syntheticSource',
            'runtime_state_dir':str(self.root/'data/walkthrough-test-full/runtime'),
            'product_version':{'query_chat_id':'oc_oldInheritedGroup'},
        }
        self.identity_path = self.root/'source-identity.json'
        self.cloud_path = self.root/'source-cloud.json'
        self.identity_path.write_text(json.dumps(self.identity), encoding='utf-8')
        self.cloud_path.write_text(json.dumps({'username':'synthetic-private-config'}), encoding='utf-8')
        self.settings = {**self.identity, 'product_version':copy.deepcopy(PRODUCT)}

    def args(self, **changes):
        values = dict(
            workspace_root=self.root/'workspace', identity_settings=self.identity_path,
            cloud_settings=self.cloud_path, output=self.root/'new/settings.json',
            environment='test_then_prod', execution='wsl', distribution='Ubuntu',
            analyzer_python='/latest/venv/bin/python', analyzer_root='/latest/auto_anasys',
            rules_json=self.root/'rules.json', rules_token=None,
            query_chat_id=PRODUCT['query_chat_id'], asgard_bot_id=PRODUCT['asgard_bot_id'],
            asgard_app_id=None, product_version_timeout_seconds=120,
            product_version_poll_interval_seconds=2,
        )
        values.update(changes)
        return argparse.Namespace(**values)

    def test_independent_runtime_and_fixed_query_target_preserve_source_configuration(self):
        args = self.args()
        original_identity = self.identity_path.read_bytes()
        original_cloud = self.cloud_path.read_bytes()
        legacy = args.output.parent/'walkthrough-cloud.settings.json'
        legacy.parent.mkdir()
        legacy.write_text('old 0.6 cloud settings', encoding='utf-8')
        with patch.object(preflight.policy, 'run_lark', side_effect=AssertionError('must not call CLI')):
            result = configure.configure(args)
        saved = json.loads(args.output.read_text(encoding='utf-8'))
        self.assertEqual(saved['runtime_state_dir'], str((args.workspace_root/'data/walkthrough-test-version/runtime').resolve()))
        self.assertEqual(saved['product_version']['query_chat_id'], 'oc_syntheticquery')
        self.assertEqual(saved['product_version']['asgard_bot_id'], 'ou_syntheticAsgard')
        self.assertNotIn('source_chat_id', saved)
        self.assertNotEqual(saved['product_version']['query_chat_id'], self.identity['source_chat_id'])
        self.assertEqual(Path(saved['cloud_settings_path']).name, 'walkthrough-version-cloud.settings.json')
        self.assertEqual(legacy.read_text(encoding='utf-8'), 'old 0.6 cloud settings')
        self.assertEqual(self.identity_path.read_bytes(), original_identity)
        self.assertEqual(self.cloud_path.read_bytes(), original_cloud)
        self.assertEqual(result['status'], 'configured')
        self.assertNotIn('synthetic-private-config', json.dumps(result))
        self.assertEqual(saved['local_analysis']['analyzer_root'], '/latest/auto_anasys')
        self.assertEqual(saved['local_analysis']['rules_json'], str(args.rules_json.resolve()))

    def test_optional_app_and_explicit_poll_limits_are_preserved(self):
        args = self.args(asgard_app_id='cli_syntheticAsgard', product_version_timeout_seconds=60,
                         product_version_poll_interval_seconds=3)
        configure.configure(args)
        saved = json.loads(args.output.read_text(encoding='utf-8'))['product_version']
        self.assertEqual(saved['asgard_app_id'], 'cli_syntheticAsgard')
        self.assertEqual(saved['timeout_seconds'], 60)
        self.assertEqual(saved['poll_interval_seconds'], 3)

    def test_existing_version_settings_are_never_overwritten(self):
        for name in ('settings.json', 'walkthrough-version-cloud.settings.json'):
            with self.subTest(name=name):
                args = self.args(output=self.root/name.replace('.json','')/'settings.json')
                target = args.output.parent/name
                target.parent.mkdir()
                target.write_text('preserve', encoding='utf-8')
                with self.assertRaises(ValueError):
                    configure.configure(args)
                self.assertEqual(target.read_text(encoding='utf-8'), 'preserve')
                self.assertEqual(len(list(target.parent.iterdir())), 1)

    def test_output_cannot_overwrite_its_own_new_cloud_configuration(self):
        args = self.args(output=self.root/'new/walkthrough-version-cloud.settings.json')
        with self.assertRaises(ValueError):
            configure.configure(args)
        self.assertFalse(args.output.parent.exists())

    def test_invalid_query_config_fails_before_writing_any_output(self):
        cases = (
            {'query_chat_id':''}, {'query_chat_id':'chat:oc_syntheticquery'},
            {'query_chat_id':'ou_syntheticuser'}, {'asgard_bot_id':'cli_wrongtype'},
            {'asgard_app_id':'oc_wrongtype'}, {'product_version_timeout_seconds':0},
            {'product_version_poll_interval_seconds':121},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                args = self.args(output=self.root/str(index)/'settings.json', **changes)
                with self.assertRaises((InputError, ValueError)):
                    configure.configure(args)
                self.assertFalse(args.output.parent.exists())

    def test_cli_exposes_required_target_and_default_bounds(self):
        parser = configure.build_parser()
        argv = ['--workspace-root',str(self.root), '--identity-settings',str(self.identity_path),
                '--cloud-settings',str(self.cloud_path), '--output',str(self.root/'new.json'),
                '--analyzer-python','/latest/python', '--analyzer-root','/latest/analyzer',
                '--rules-token','syntheticRules', '--query-chat-id','oc_syntheticquery',
                '--asgard-bot-id','ou_syntheticAsgard']
        args = parser.parse_args(argv)
        self.assertEqual(args.product_version_timeout_seconds, 120)
        self.assertEqual(args.product_version_poll_interval_seconds, 2)
        self.assertIsNone(args.asgard_app_id)
        for flag in ('--query-chat-id', '--asgard-bot-id'):
            action = next(action for action in parser._actions if flag in action.option_strings)
            self.assertTrue(action.required)

    def test_readonly_preflight_checks_pinned_user_without_verify_or_messages(self):
        with patch.object(preflight.policy,'auth_status',return_value=pinned_identity()) as status, \
             patch.object(preflight.policy,'ensure_user_authorization',side_effect=AssertionError('refresh prohibited')), \
             patch.object(preflight.policy,'run_lark',side_effect=AssertionError('messages prohibited')):
            result = preflight.check_product_configuration(self.settings)
        status.assert_called_once_with(self.settings, verify=False)
        self.assertEqual(result['identity'], 'user')
        self.assertTrue(result['fixed_operator_matched'])
        self.assertFalse(result['network_verified'])
        self.assertFalse(result['query_sent'])
        self.assertFalse(result['authorization_requested'])
        self.assertFalse(result['asgard_app_id_configured'])
        for private in ('oc_syntheticquery','ou_syntheticAsgard','ou_syntheticOperator','cli_syntheticProject'):
            self.assertNotIn(private, json.dumps(result))

    def test_preflight_rejects_wrong_or_unavailable_user_without_authorization(self):
        cases = [('wrong-app', {'appId':'cli_other'}),
                 ('wrong-user', {'openId':'ou_other'}),
                 ('unavailable', {'available':False}),
                 ('expired-status', {'tokenStatus':'expired'}),
                 ('missing-expiry', {'expiresAt':None}),
                 ('expired-time', {'expiresAt':'2000-01-01T00:00:00+00:00'})]
        for name, changes in cases:
            with self.subTest(name=name):
                payload = pinned_identity()
                if name == 'wrong-app': payload.update(changes)
                else: payload['identities']['user'].update(changes)
                with patch.object(preflight.policy,'auth_status',return_value=payload) as status, \
                     patch.object(preflight.policy,'ensure_user_authorization',side_effect=AssertionError('no refresh')), \
                     patch.object(preflight.policy,'run_lark',side_effect=AssertionError('no message')):
                    with self.assertRaises((InputError,ValueError)):
                        preflight.check_product_configuration(self.settings)
                status.assert_called_once_with(self.settings, verify=False)

    def test_preflight_rejects_missing_fixed_target_before_identity_read(self):
        settings = {**self.settings, 'product_version':{**PRODUCT,'query_chat_id':''}}
        with patch.object(preflight.policy,'auth_status',side_effect=AssertionError('must not read CLI')):
            with self.assertRaises((InputError,ValueError)):
                preflight.check_product_configuration(settings)

    def test_preflight_never_falls_back_to_bot_or_default_user(self):
        payload = pinned_identity()
        payload['identities'].pop('user')
        with patch.object(preflight.policy,'auth_status',return_value=payload), \
             patch.object(preflight.policy,'run_lark',side_effect=AssertionError('no fallback')):
            with self.assertRaises(ValueError):
                preflight.check_product_configuration(self.settings)

    def test_full_preflight_preserves_native_checks_and_sends_nothing(self):
        cli = self.root/'lark-cli.exe'
        cli.write_text('synthetic', encoding='utf-8')
        settings = {**self.settings, 'lark_cli':str(cli), 'cloud_settings_path':str(self.cloud_path),
                    'local_analysis':{'environment':'test_then_prod','distribution':'Ubuntu',
                                      'python':'/latest/python','analyzer_root':'/latest/analyzer'}}
        with patch.object(preflight,'load_settings',return_value=settings), \
             patch.object(preflight,'check_host_dependencies',return_value={'python':'3.11'}), \
             patch.object(preflight.cloud,'environment_config'), \
             patch.object(preflight.cloud,'load_settings'), \
             patch.object(preflight.cloud,'request_json',side_effect=AssertionError('no cloud query')), \
             patch.object(preflight,'check_native_environment',return_value={'source_files_present':True}) as native, \
             patch.object(preflight.policy,'auth_status',return_value=pinned_identity()) as status, \
             patch.object(preflight.policy,'run_lark',side_effect=AssertionError('no message')):
            result = preflight.check(self.root/'unused.json')
        self.assertEqual(result['status'],'local_preflight_passed')
        self.assertEqual(result['backend'],'native_full')
        self.assertEqual(result['cloud_environment_policy'],'test_then_prod')
        self.assertTrue(result['product_version']['fixed_operator_matched'])
        self.assertFalse(result['product_version']['query_sent'])
        native.assert_called_once_with(settings['local_analysis'])
        status.assert_called_once_with(settings, verify=False)

    def test_example_has_only_placeholders_and_new_runtime(self):
        example = json.loads((Path(__file__).resolve().parents[5]/'examples/settings.windows.example.json').read_text(encoding='utf-8'))
        for key in ('query_chat_id','asgard_bot_id','asgard_app_id'):
            self.assertIn('REPLACE',example['product_version'][key])
        self.assertIn('walkthrough-test-windows081',example['runtime_state_dir'])
        self.assertNotIn('walkthrough-test-full',example['runtime_state_dir'])
        self.assertEqual(example['product_version']['timeout_seconds'],120)
        self.assertEqual(example['product_version']['poll_interval_seconds'],2)
        self.assertEqual(example['local_analysis']['backend'], 'native_full')
        self.assertEqual(example['execution_release'], 'walkthrough-target16-hardening')


if __name__ == '__main__':
    unittest.main()
