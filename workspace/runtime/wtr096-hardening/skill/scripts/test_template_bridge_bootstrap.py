"""All ready manifests/CLI responses here are synthetic; no cloud operations."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import template_reference_access as access
import prepare_native_template_bridge as bootstrap
from request_model import InputError

SOURCE_TOKEN = 'SourceTemplate123'
SOURCE_SHEET_ID = 'SourceSheet1'
RELAY_TOKEN = 'RelayTemplate789'
BOT_OPEN_ID = 'ou_syntheticReportBot'
REAL_ENSURE_REPORT_BOT_AUTHORIZATION = access.policy.ensure_report_bot_authorization


def synthetic_ready_manifest(settings, source_token=SOURCE_TOKEN, source_sheet_id=SOURCE_SHEET_ID,
                             relay_token=RELAY_TOKEN, bot_open_id=BOT_OPEN_ID):
    """Test factory only. Production bootstrap has no ready-manifest writer."""
    relay = {'token': relay_token, 'url': 'https://example.feishu.cn/sheets/' + relay_token,
             'name': 'Synthetic private relay'}
    return {'schema': access.SCHEMA, 'state': 'ready', 'binding': access.settings_binding(settings),
        'source': {'token': source_token, 'sheet_id': source_sheet_id}, 'relay': relay,
        'copy': {'ok': True, 'identity': 'user', 'data': {'copied': True, 'file_type': 'sheet',
            'source_type': 'sheet', 'source_file_token': source_token, 'file_token': relay_token,
            'url': relay['url'], 'name': relay['name']}},
        'bot': {'open_id': bot_open_id, 'app_id': settings['expected_lark_app_id']},
        'verification': {**{key: True for key in access.VERIFIED_FLAGS},
            'source_snapshot_sha256': 'a' * 64, 'relay_snapshot_sha256': 'b' * 64}}


class TemplateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ('bridge-fixture-' + uuid.uuid4().hex[:12])
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.home = self.root / 'identity'
        self.home.mkdir()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.record = self.root / 'data/walkthrough-template-bridge/relay-ready.json'
        self.record.parent.mkdir(parents=True)
        self.settings = {'workspace_root': str(self.root), 'runtime_state_dir': str(self.runtime),
            'lark_home': str(self.home), 'project_id': 'synthetic', 'expected_lark_app_id': 'cli_synthetic',
            'expected_operator_open_id': 'ou_syntheticOperator', 'lark_profile': 'synthetic-user',
            'report_lark_profile': 'synthetic-bot', 'report_delivery': {
                'native_template_reference_reader': 'fixed_user',
                'native_template_spreadsheet_token': RELAY_TOKEN,
                'native_template_bridge_manifest': str(self.record)}}
        self.references = {role: {'token': SOURCE_TOKEN, 'sheet_id': SOURCE_SHEET_ID} for role in access.ROLES}
        self.manifest = synthetic_ready_manifest(self.settings)
        self.save()
        self.user = self.enterContext(patch.object(access.policy, 'ensure_user_authorization',
            return_value={'open_id': self.settings['expected_operator_open_id'], 'refreshed': False}))
        self.bot = self.enterContext(patch.object(access.policy, 'ensure_report_bot_authorization',
            return_value={'profile': self.settings['report_lark_profile'], 'available': True}))
        self.cli = self.enterContext(patch.object(access.policy, 'run_lark', side_effect=self.response))
        self.enterContext(patch.object(access.policy, 'WORKSPACE_ROOT', self.root))

    def cleanup(self):
        assert self.root.resolve().parent == Path(__file__).resolve().parent
        shutil.rmtree(self.root)

    def save(self):
        self.record.write_text(json.dumps(self.manifest), encoding='utf-8')

    def response(self, settings, args, **kwargs):
        if args[:3] == ['api', 'GET', '/open-apis/bot/v3/info']:
            return {'ok': True, 'identity': 'bot', 'data': {
                'activate_status': 2, 'app_name': 'Synthetic report Bot',
                'avatar_url': 'https://example.invalid/avatar', 'ip_white_list': [],
                'open_id': BOT_OPEN_ID}}
        return {'ok': True, 'identity': 'user', 'data': {'auth_result': True}}

    def binding(self):
        return access.build_source_binding(self.settings, self.references, RELAY_TOKEN)

    def workbook(self, token=SOURCE_TOKEN):
        return ['sheets', '+workbook-info', '--spreadsheet-token', token]

    def test_default_bot_returns_none_without_reading_or_checking_identities(self):
        with patch.object(access, '_read', side_effect=AssertionError('no file read')):
            self.assertIsNone(access.build_source_binding({}, {}, ''))
        self.cli.assert_not_called()
        self.user.assert_not_called()
        self.bot.assert_not_called()

    def test_fixed_binding_is_stable_and_keeps_distinct_snapshot_digests(self):
        first = self.binding()
        self.user.return_value = {**self.user.return_value, 'refreshed': True, 'expires_at': 'variable'}
        self.assertEqual(self.binding(), first)
        self.assertNotEqual(self.manifest['verification']['source_snapshot_sha256'],
                            self.manifest['verification']['relay_snapshot_sha256'])
        self.assertNotIn('checked_at', first)
        self.assertNotIn('lark_home', first)

    def test_every_ready_success_flag_requires_literal_true(self):
        for key in access.VERIFIED_FLAGS:
            original = self.manifest['verification'][key]
            for value in (False, None, 'true', 1):
                self.manifest['verification'][key] = value
                self.save()
                with self.subTest(key=key, value=value), self.assertRaises(InputError):
                    self.binding()
            self.manifest['verification'][key] = original
        self.save()

    def test_copy_success_identity_source_new_token_name_and_url_all_checked(self):
        cases = [('copied', False), ('source_type', 'docx'), ('file_type', 'docx'),
                 ('source_file_token', 'OtherSource'), ('file_token', SOURCE_TOKEN),
                 ('name', 'different name'), ('url', 'https://example.feishu.cn/sheets/OtherRelay')]
        for key, value in cases:
            copied = copy.deepcopy(self.manifest)
            copied['copy']['data'][key] = value
            self.record.write_text(json.dumps(copied), encoding='utf-8')
            with self.subTest(key=key), self.assertRaises(InputError):
                self.binding()
        self.save()
        for identity in ('bot', None):
            self.manifest['copy']['identity'] = identity
            self.save()
            with self.assertRaises(InputError):
                self.binding()

    def test_any_permission_grant_in_user_copy_receipt_is_rejected(self):
        for value in (None, {}, {'status': 'not_granted'}, {'status': 'granted'}, 'unknown'):
            self.manifest['copy']['data']['permission_grant'] = value
            self.save()
            with self.subTest(value=value), self.assertRaises(InputError):
                self.binding()

    def test_unknown_or_pending_manifest_is_never_ready(self):
        for value in ('planned', 'copy_pending', 'unknown', None):
            self.manifest['state'] = value
            self.save()
            with self.subTest(state=value), self.assertRaises(InputError):
                self.binding()

    def test_foreign_and_identity_manifest_paths_are_rejected_before_open(self):
        self.settings['report_delivery']['native_template_bridge_manifest'] = str(self.home / 'secret.json')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not open foreign path')):
            with self.assertRaises(InputError):
                self.binding()
        self.cli.assert_not_called()

    def test_identity_home_inside_dedicated_directory_is_still_excluded(self):
        home = self.record.parent / 'identity'
        home.mkdir()
        self.settings['lark_home'] = str(home)
        self.settings['report_delivery']['native_template_bridge_manifest'] = str(home / 'ready.json')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not open identity home')):
            with self.assertRaises(InputError):
                self.binding()

    def test_manifest_hardlink_is_rejected(self):
        link = self.record.with_name('linked.json')
        try:
            os.link(self.record, link)
        except OSError:
            self.skipTest('hardlinks unavailable on this filesystem')
        self.settings['report_delivery']['native_template_bridge_manifest'] = str(link)
        with self.assertRaises(InputError):
            self.binding()

    def test_source_references_and_relay_cannot_drift(self):
        for refs, relay in [({**self.references, 'replacement': {'token': 'OtherSource', 'sheet_id': SOURCE_SHEET_ID}}, RELAY_TOKEN),
                             (self.references, SOURCE_TOKEN), (self.references, 'OtherRelay')]:
            with self.subTest(relay=relay), self.assertRaises(InputError):
                access.build_source_binding(self.settings, refs, relay)

    def test_target_normalized_bot_info_is_checked_fresh_with_explicit_bot_profile(self):
        # Shape confirmed from the target's retained actual CLI response.
        first = self.binding()
        self.bot.assert_called_once_with(self.settings)
        self.cli.assert_called_once_with(self.settings,
            ['api', 'GET', '/open-apis/bot/v3/info', '--as', 'bot'],
            profile=self.settings['report_lark_profile'], timeout=60)
        self.assertEqual(first['bot_identity_check'], 'same_app_bot_profile_and_bot_info')
        self.assertIs(first['fresh_bot_open_id_verified'], True)
        self.assertEqual(first['bot_open_id'], BOT_OPEN_ID)
        self.assertEqual(self.binding(), first)
        self.assertEqual(self.bot.call_count, 2)
        self.assertEqual(self.cli.call_count, 2)

    def test_bot_info_rejects_empty_wrong_identity_id_business_error_and_unproved_shapes(self):
        self.cli.side_effect = None
        invalid = [
            {'ok': True, 'identity': 'bot', 'data': {}},
            {'ok': True, 'identity': 'user', 'data': {'open_id': BOT_OPEN_ID}},
            {'ok': True, 'identity': 'bot', 'data': {'open_id': 'ou_other'}},
            {'ok': False, 'identity': 'bot', 'data': {'open_id': BOT_OPEN_ID}},
            {'ok': True, 'identity': 'bot', 'data': {'open_id': BOT_OPEN_ID, 'app_id': 'cli_other'}},
            {'ok': True, 'identity': 'bot', 'data': {'bot': {'open_id': BOT_OPEN_ID}}},
            {'ok': True, 'identity': 'bot', 'data': {'code': 1, 'bot': {'open_id': BOT_OPEN_ID}}},
            {'ok': True, 'identity': 'bot', 'data': {'code': False, 'bot': {'open_id': BOT_OPEN_ID}}},
            {'ok': True, 'identity': 'bot', 'data': {'code': 0, 'data': {'bot': {'open_id': BOT_OPEN_ID}}}},
            {'ok': True, 'identity': 'bot', 'data': {'code': 0, 'bot': {'open_id': 'ou_other'}}},
        ]
        for payload in invalid:
            self.cli.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(InputError):
                self.binding()

    def test_exact_legacy_code_zero_bot_object_is_supported_without_path_guessing(self):
        self.cli.side_effect = None
        self.cli.return_value = {'ok': True, 'identity': 'bot',
            'data': {'code': 0, 'bot': {'open_id': BOT_OPEN_ID,
                'app_id': self.settings['expected_lark_app_id']}}}
        self.assertIs(self.binding()['fresh_bot_open_id_verified'], True)

    def test_real_bot_policy_requires_same_app_and_dedicated_bot_only_profile(self):
        good = {'appId': self.settings['expected_lark_app_id'], 'identities': {
            'bot': {'available': True}, 'user': {'available': False}}}
        invalid = []
        for key, value in [('appId', 'cli_other'), ('identities', {}),
                           ('identities', {'bot': {'available': 1}}),
                           ('identities', {'bot': {'available': True}, 'user': {'available': True}})]:
            invalid.append({**good, key: value})
        with patch.object(access.policy, 'ensure_report_bot_authorization',
                          REAL_ENSURE_REPORT_BOT_AUTHORIZATION), \
             patch.object(access.policy, '_profile_auth_status', return_value=good) as status:
            self.binding()
            status.assert_called_once_with(self.settings, self.settings['report_lark_profile'], verify=False)
            for payload in invalid:
                status.return_value = payload
                with self.subTest(payload=payload), self.assertRaises(InputError):
                    self.binding()
        self.assertEqual(self.cli.call_count, 1)  # Only the valid auth-status case reached GET.

    def test_bot_policy_failure_or_incomplete_success_cannot_be_accepted(self):
        for value in (None, {}, {'profile': 'other', 'available': True},
                      {'profile': self.settings['report_lark_profile'], 'available': 1}):
            self.bot.return_value = value
            with self.subTest(value=value), self.assertRaises(InputError):
                self.binding()
        self.bot.side_effect = InputError('Synthetic auth-status failure')
        with self.assertRaises(InputError):
            self.binding()
        self.cli.assert_not_called()

    def test_user_reads_source_only_and_preserves_identity(self):
        binding = self.binding()
        self.cli.return_value = {'ok': True, 'identity': 'user', 'data': {'sheets': []}}
        self.cli.side_effect = None
        result = access.user_reference_call(self.settings, binding, self.workbook(), timeout=42)
        self.assertEqual(result, {'sheets': []})
        self.assertEqual(self.cli.call_args.args[1][-2:], ['--as', 'user'])
        self.assertEqual(self.cli.call_args.kwargs, {'timeout': 42, 'profile': self.settings['lark_profile']})
        self.cli.return_value['identity'] = 'bot'
        with self.assertRaises(InputError):
            access.user_reference_call(self.settings, binding, self.workbook())

    def test_writes_duplicates_actor_overrides_and_other_tokens_are_rejected_before_auth(self):
        binding = self.binding()
        self.user.reset_mock()
        self.cli.reset_mock()
        invalid = [['sheets', '+cells-set', '--spreadsheet-token', SOURCE_TOKEN],
            self.workbook(RELAY_TOKEN), self.workbook() + ['--as', 'bot'],
            self.workbook() + ['--profile', 'foreign'], self.workbook() + ['--home', 'foreign'],
            self.workbook() + ['--format', 'text'],
            self.workbook() + ['--spreadsheet-token', 'OtherSource'],
            ['drive', 'permission.members', 'auth', '--action', 'manage', '--token', SOURCE_TOKEN, '--type', 'sheet']]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(InputError):
                access.user_reference_call(self.settings, binding, args)
        self.user.assert_not_called()
        self.cli.assert_not_called()

    def test_mode_and_override_mutation_rejected_before_auth(self):
        binding = self.binding()
        for key, value in [('native_template_reference_reader', 'bot'),
                           ('native_template_spreadsheet_token', 'OtherRelay')]:
            original = self.settings['report_delivery'][key]
            self.settings['report_delivery'][key] = value
            self.user.reset_mock()
            self.cli.reset_mock()
            with self.subTest(key=key), self.assertRaises(InputError):
                access.user_reference_call(self.settings, binding, self.workbook())
            self.user.assert_not_called()
            self.cli.assert_not_called()
            self.settings['report_delivery'][key] = original

    def test_manifest_replacement_rejected_before_auth(self):
        binding = self.binding()
        self.manifest['deployment_note'] = 'changed record bytes'
        self.save()
        self.user.reset_mock()
        self.cli.reset_mock()
        with self.assertRaises(InputError):
            access.user_reference_call(self.settings, binding, self.workbook())
        self.user.assert_not_called()
        self.cli.assert_not_called()

    def test_cells_output_cannot_overwrite_record_or_enter_foreign_directory(self):
        binding = self.binding()
        for output in ('../outside.json', str(self.root / 'absolute.json'),
                       './data/walkthrough-template-bridge/relay-ready.json'):
            args = ['sheets', '+cells-get', '--spreadsheet-token', SOURCE_TOKEN,
                    '--sheet-id', SOURCE_SHEET_ID, '--range', 'B1:O2', '--include', 'value,formula,style',
                    '--output-path', output]
            with self.subTest(output=output), self.assertRaises(InputError):
                access.user_reference_call(self.settings, binding, args)

    def test_copy_permission_requires_true_and_evidence_is_outside_stable_binding(self):
        binding = self.binding()
        result = access.assert_source_copy_allowed(self.settings, binding)
        self.assertIs(result['auth_result'], True)
        self.assertEqual(result['identity'], 'user')
        self.assertIn('checked_at', result)
        self.assertNotIn('checked_at', binding)
        self.cli.side_effect = None
        for value in (False, None, 'true', 1):
            self.cli.return_value = {'ok': True, 'identity': 'user', 'data': {'auth_result': value}}
            with self.subTest(value=value), self.assertRaises(InputError):
                access.assert_source_copy_allowed(self.settings, binding)

    def test_default_plan_is_local_idempotent_and_never_ready_or_cloud(self):
        path = self.record.with_name('plan.json')
        first = bootstrap.prepare(self.settings, path, SOURCE_TOKEN, SOURCE_SHEET_ID, 'Relay name')
        before = path.read_bytes()
        second = bootstrap.prepare(self.settings, path, SOURCE_TOKEN, SOURCE_SHEET_ID, 'Relay name')
        self.assertEqual(first['status'], 'planned')
        self.assertIs(second['idempotent'], True)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(first['cloud_executed'])
        self.assertFalse(first['ready_manifest_created'])
        self.assertEqual(first['plan']['state'], 'planned')
        self.assertTrue(first['plan']['unsupported_checks'])
        self.cli.assert_not_called()
        self.user.assert_not_called()
        self.bot.assert_not_called()

    def test_all_execution_flags_fail_before_any_record_or_cloud_action(self):
        path = self.record.with_name('not-created.json')
        for flag in ('execute', 'execute_copy', 'grant_report_bot'):
            with self.subTest(flag=flag), self.assertRaises(InputError):
                bootstrap.prepare(self.settings, path, SOURCE_TOKEN, SOURCE_SHEET_ID, 'Relay', **{flag: True})
        self.assertFalse(path.exists())
        self.cli.assert_not_called()
        self.user.assert_not_called()

    def test_cli_execute_flags_do_not_read_even_settings(self):
        for flag in ('--execute', '--execute-copy', '--grant-report-bot'):
            args = ['--settings', str(self.root / 'must-not-read.json'), '--record', str(self.record),
                    '--source-token', SOURCE_TOKEN, '--source-sheet-id', SOURCE_SHEET_ID,
                    '--relay-name', 'Relay', flag]
            with patch.object(Path, 'read_text', side_effect=AssertionError('no settings read')), redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(args), 2)
        self.cli.assert_not_called()

    def test_unknown_or_pending_records_are_preserved_without_retry(self):
        path = self.record.with_name('pending.json')
        for value in ({'schema': 'other', 'state': 'copy_pending'}, {'schema': 'other', 'state': 'unknown'},
                      {'schema': 'other', 'state': 'ready'}, ['foreign object']):
            path.write_text(json.dumps(value), encoding='utf-8')
            original = path.read_bytes()
            with self.subTest(value=value), self.assertRaises(InputError):
                bootstrap.prepare(self.settings, path, SOURCE_TOKEN, SOURCE_SHEET_ID, 'Relay')
            self.assertEqual(path.read_bytes(), original)
        self.cli.assert_not_called()


if __name__ == '__main__':
    unittest.main()
