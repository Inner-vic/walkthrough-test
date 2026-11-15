"""Synthetic publisher tests: fixed User source reads, Bot-owned output lifecycle.

Every run_lark call is captured by an in-memory transport. Socket and process
creation are blocked for the entire fixture; no real identities or cloud writes.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

import native_sheet_delivery as delivery
import template_reference_access as access_module
import test_native_sheet_delivery as legacy
from request_model import InputError


def ready_manifest(settings):
    """Synthetic evidence only; never used by the plan-only bridge helper."""
    source = {'token': 'templateSynthetic', 'sheet_id': 'sheet0'}
    relay = {'token': 'relaySynthetic', 'name': 'Synthetic private relay',
             'url': 'https://example.feishu.cn/sheets/relaySynthetic'}
    return {'schema': 'walkthrough-template-bridge-v1', 'state': 'ready',
        'binding': access_module.settings_binding(settings), 'source': source, 'relay': relay,
        'copy': {'ok': True, 'identity': 'user', 'data': {'copied': True,
            'file_type': 'sheet', 'source_type': 'sheet', 'source_file_token': source['token'],
            'file_token': relay['token'], 'name': relay['name'], 'url': relay['url']}},
        'bot': {'open_id': 'ou_syntheticBot', 'app_id': settings['expected_lark_app_id']},
        'verification': {**{key: True for key in access_module.VERIFIED_FLAGS},
            'source_snapshot_sha256': 'b' * 64, 'relay_snapshot_sha256': 'c' * 64}}


def rename_sheets(book, prefix):
    for index, sheet in enumerate(book['sheets']):
        old = sheet['info']['sheet_id']; new = prefix + str(index)
        sheet['info']['sheet_id'] = new
        sheet['layout']['sheet_id'] = new
        for row in sheet['cells']:
            for cell in row:
                if isinstance(cell.get('formula'), str):
                    cell['formula'] = cell['formula'].replace(old + '!', new + '!')


class DualTransport(legacy.FakeLark):
    def __init__(self, root):
        super().__init__(root)
        self.events = []; self.copy_allowed = True; self.user_identity = 'user'
        self.auth_calls = 0; self.before_auth = None; self.source_unavailable = False
        self.bot_open_id = 'ou_syntheticBot'; self.bot_info_shape = 'direct'
        self.books['relaySynthetic'] = copy.deepcopy(self.books['templateSynthetic'])
        self.books['relaySynthetic']['token'] = 'relaySynthetic'
        rename_sheets(self.books['relaySynthetic'], 'relaySheet')

    def __call__(self, settings, args, *, profile, timeout=150):
        identity = self.flag(args, '--as')
        self.events.append({'identity': identity, 'profile': profile, 'args': list(args)})
        if args[:3] == ['api', 'GET', '/open-apis/bot/v3/info']:
            assert identity == 'bot' and profile == 'report-bot'
            bot = {'open_id': self.bot_open_id, 'app_id': settings['expected_lark_app_id']}
            data = bot if self.bot_info_shape == 'direct' else {'code': 0, 'bot': bot}
            if self.bot_info_shape == 'empty': data = {}
            return {'ok': True, 'identity': 'bot', 'data': data}
        if identity == 'user':
            assert profile == 'query-user'
            if self.source_unavailable:
                raise AssertionError('resume must not read or authorize the live source')
            if args[:3] == ['drive', 'permission.members', 'auth']:
                assert self.flag(args, '--token') == 'templateSynthetic'
                assert self.flag(args, '--action') == 'copy' and self.flag(args, '--type') == 'sheet'
                self.auth_calls += 1
                if self.before_auth: self.before_auth(self.auth_calls)
                self.calls.append(list(args))
                return {'ok': True, 'identity': self.user_identity,
                        'data': {'auth_result': self.copy_allowed}}
            assert args[:2] in (['sheets', '+workbook-info'], ['sheets', '+sheet-info'], ['sheets', '+cells-get'])
            assert self.flag(args, '--spreadsheet-token') == 'templateSynthetic'
            # Reuse the legacy in-memory sheet implementation, preserving actual
            # User events separately; this does not invoke the real CLI.
            delegated = list(args); delegated[delegated.index('--as') + 1] = 'bot'
            before = len(self.calls)
            result = super().__call__(settings, delegated, profile='report-bot', timeout=timeout)
            self.calls[before] = list(args)
            result['identity'] = self.user_identity
            return result
        assert identity == 'bot' and profile == 'report-bot'
        if '--spreadsheet-token' in args:
            assert self.flag(args, '--spreadsheet-token') != 'templateSynthetic'
        if args[:2] == ['drive', '+copy']:
            assert self.flag(args, '--token') == 'relaySynthetic'
        result = super().__call__(settings, args, profile=profile, timeout=timeout)
        if args[:2] == ['drive', '+copy'] and result.get('data', {}).get('file_token') in self.books:
            token = result['data']['file_token']
            rename_sheets(self.books[token], token + 'Sheet')
        return result


class TemplateReferenceReaderTests(unittest.TestCase):
    run_delivery = legacy.DeliveryTests.run_delivery
    change_data = legacy.DeliveryTests.change_data

    def setUp(self):
        legacy.DeliveryTests.setUp(self)
        old = self.path.parent; runtime = self.root / 'data/runtime'
        runtime.parent.mkdir(); old.rename(runtime)
        self.path = runtime / self.path.name
        self.settings.update(runtime_state_dir=str(runtime), project_id='synthetic-project',
            expected_lark_app_id='cli_syntheticProject', expected_operator_open_id='ou_syntheticOperator')
        for item in self.result['native_sheet_data'].values():
            item['path'] = str(runtime / Path(item['path']).relative_to(old))
        self.fake = DualTransport(self.root)
        def start(item):
            value = item.start(); self.addCleanup(item.stop); return value
        self.network = start(patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')))
        self.process = start(patch.object(subprocess, 'Popen', side_effect=AssertionError('real CLI forbidden')))
        start(patch.object(delivery.policy, 'run_lark', self.fake))
        self.user_check = start(patch.object(delivery.policy, 'ensure_user_authorization',
            side_effect=lambda settings, expected: {'open_id': settings['expected_operator_open_id']}))
        self.snapshot_auth_status = start(patch.object(delivery.policy, 'auth_status',
            side_effect=lambda settings, **options: {'appId': settings['expected_lark_app_id'],
                'identities': {'user': {'openId': settings['expected_operator_open_id'],
                    'available': True, 'tokenStatus': 'valid',
                    'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}}))
        self.manifest_path = self.root / 'data/walkthrough-template-bridge/ready.json'
        self.manifest_path.parent.mkdir()
        self.manifest = ready_manifest(self.settings); self.save_manifest()
        self.settings['report_delivery'].update(native_template_reference_reader='fixed_user',
            native_template_spreadsheet_token='relaySynthetic',
            native_template_bridge_manifest=str(self.manifest_path))
        self.owner_calls = []
        def owner(settings, url, requester):
            self.assertEqual(requester, self.record['sender_id'])
            self.assertNotIn(delivery._sheet_token(url), ('templateSynthetic', 'relaySynthetic'))
            self.owner_calls.append((url, requester))
        delivery.policy.transfer_report_owner.side_effect = owner

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding='utf-8')

    def assert_no_real_io(self):
        self.network.assert_not_called(); self.process.assert_not_called()

    def test_user_reference_and_distinct_relay_and_final_ids_with_bot_to_requester(self):
        output = self.run_delivery()
        state = self.record['native_sheets']
        self.assertEqual(state['version'], delivery.FIXED_USER_VERSION)
        access = state['identity']['binding']['source_access']
        self.assertEqual(access['source_sheet_id'], 'sheet0')
        self.assertEqual(access['bot_identity_check'], 'same_app_bot_profile_and_bot_info')
        self.assertIs(access['fresh_bot_open_id_verified'], True)
        self.assertEqual(self.fake.counts['copy'], 2); self.assertEqual(self.fake.counts['write'], 2)
        self.assertEqual(self.fake.auth_calls, 4); self.assertEqual(len(self.owner_calls), 2)
        sets = [{s['info']['sheet_id'] for s in self.fake.books[token]['sheets']}
                for token in ['templateSynthetic', 'relaySynthetic', output['baseline']['token'], output['replacement']['token']]]
        self.assertEqual(len(set.union(*sets)), 28)
        for entry in state['roles'].values():
            for key in ('reference_snapshot', 'reference_copy_authorization',
                        'reference_copy_recheck', 'reference_before_copy'):
                self.assertEqual(delivery._digest(entry[key]['path']), entry[key]['sha256'])
        for event in self.fake.events:
            args = event['args']
            if event['identity'] == 'user':
                self.assertNotEqual(args[:2], ['drive', '+copy'])
                self.assertNotEqual(args[:2], ['sheets', '+cells-set'])
            if args[:2] in (['drive', '+copy'], ['sheets', '+cells-set']):
                self.assertEqual((event['identity'], event['profile']), ('bot', 'report-bot'))
        self.assert_no_real_io()

    def test_false_or_unknown_source_copy_permission_stops_before_source_or_copy(self):
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                self.record.pop('native_sheets', None); self.fake.copy_allowed = value
                with self.assertRaisesRegex(InputError, 'copy权限'): self.run_delivery()
                self.assertNotIn('copy', self.fake.counts); self.assertNotIn('info', self.fake.counts)
        self.assert_no_real_io()

    def test_wrong_user_response_identity_is_rejected(self):
        self.fake.user_identity = 'bot'
        with self.assertRaisesRegex(InputError, '固定user身份'): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_wrong_current_user_or_bot_cannot_claim_manifest(self):
        self.user_check.side_effect = lambda settings, expected: {'open_id': 'ou_otherUser'}
        with self.assertRaisesRegex(InputError, 'User身份'): self.run_delivery()
        self.assertFalse(self.fake.events)
        self.user_check.side_effect = lambda settings, expected: {'open_id': expected}
        self.fake.bot_open_id = 'ou_otherBot'
        with self.assertRaisesRegex(InputError, 'Bot身份'): self.run_delivery()
        self.fake.bot_open_id = 'ou_syntheticBot'; self.fake.bot_info_shape = 'empty'
        with self.assertRaises(InputError): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_relay_value_or_style_difference_stops_before_copy(self):
        self.fake.books['relaySynthetic']['sheets'][0]['cells'][0][0]['style']['font']['bold'] = True
        with self.assertRaisesRegex(InputError, '不兼容'): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_reference_sheet_id_must_belong_to_original_not_relay(self):
        for role in delivery.ROLES:
            self.change_data(role, lambda value: value['native_template_reference'].update(sheet_id='notInSource'))
        self.manifest['source']['sheet_id'] = 'notInSource'; self.save_manifest()
        with self.assertRaisesRegex(InputError, 'sheet_id'): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_source_mutation_before_copy_stops_without_new_report(self):
        def mutate(count):
            if count == 2:
                self.fake.books['templateSynthetic']['sheets'][0]['cells'][0][1]['value'] = 'new reference'
        self.fake.before_auth = mutate
        with self.assertRaisesRegex(InputError, '原reference在准备后已更新'): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_source_copy_revocation_before_actual_copy_stops(self):
        self.fake.before_auth = lambda count: setattr(self.fake, 'copy_allowed', count == 1)
        with self.assertRaisesRegex(InputError, 'copy权限'): self.run_delivery()
        self.assertNotIn('copy', self.fake.counts)

    def test_ready_resume_uses_saved_source_evidence_without_live_source_or_reanalysis(self):
        first = self.run_delivery(); copies = self.fake.counts['copy']; auths = self.fake.auth_calls
        self.fake.source_unavailable = True
        with patch.object(delivery.policy, 'ensure_user_authorization', side_effect=self.user_check.side_effect):
            second = self.run_delivery()
        self.assertEqual(first, second); self.assertEqual(self.fake.auth_calls, auths)
        self.assertEqual(self.fake.counts['copy'], copies); self.assertEqual(self.fake.counts['write'], 2)
        self.assert_no_real_io()

    def test_saved_copy_receipt_resume_does_not_reauthorize_or_recopy_source(self):
        self.fake.bot_info_shape = 'legacy'
        first = self.run_delivery(); entry = self.record['native_sheets']['roles']['baseline']
        entry.pop('token'); entry.pop('url'); entry['copy_pending'] = True
        self.fake.source_unavailable = True
        self.assertEqual(self.run_delivery()['baseline']['token'], first['baseline']['token'])
        self.assertEqual(self.fake.counts['copy'], 2); self.assertEqual(self.fake.auth_calls, 4)

    def test_unknown_copy_response_stays_unknown_and_never_repeats(self):
        self.fake.fail['copy'] = 'after'
        with self.assertRaises(TimeoutError): self.run_delivery()
        self.fake.source_unavailable = True
        with self.assertRaisesRegex(InputError, '禁止自动重复'): self.run_delivery()
        self.assertEqual(self.fake.counts['copy'], 1); self.assertEqual(self.fake.auth_calls, 2)

    def test_identity_manifest_or_mode_change_rejects_ready_state(self):
        self.run_delivery(); original = copy.deepcopy(self.settings)
        for key, value in (('expected_operator_open_id', 'ou_changed'),
                           ('lark_profile', 'different-user'), ('expected_lark_app_id', 'cli_changed')):
            with self.subTest(key=key):
                self.settings[key] = value
                with self.assertRaises(InputError): self.run_delivery()
                self.settings = copy.deepcopy(original)
        self.manifest['verification']['extra_public_evidence'] = 'changed'; self.save_manifest()
        with self.assertRaisesRegex(InputError, '断点与'): self.run_delivery()
        self.settings['report_delivery']['native_template_reference_reader'] = 'bot'
        with self.assertRaisesRegex(InputError, '断点与'): self.run_delivery()
        self.assertEqual(self.fake.counts['copy'], 2)

    def test_missing_saved_source_or_copy_proof_never_replaced_by_new_live_source(self):
        self.run_delivery(); entry = self.record['native_sheets']['roles']['baseline']
        for key in ('reference_snapshot', 'reference_copy_authorization', 'reference_copy_recheck',
                    'reference_before_copy', 'source_snapshot'):
            with self.subTest(key=key):
                saved = entry.pop(key); self.fake.source_unavailable = True
                with self.assertRaises(InputError): self.run_delivery()
                entry[key] = saved
        self.assertEqual(self.fake.counts['copy'], 2); self.assertEqual(self.fake.auth_calls, 4)

    def test_resumed_final_target_never_source_or_relay(self):
        self.run_delivery(); entry = self.record['native_sheets']['roles']['baseline']
        original = (entry['token'], entry['url'])
        for token in ('templateSynthetic', 'relaySynthetic'):
            with self.subTest(token=token):
                entry.update(token=token, url='https://example.feishu.cn/sheets/' + token)
                with self.assertRaisesRegex(InputError, '发布目标'): self.run_delivery()
        entry.update(zip(('token', 'url'), original))
        self.assertEqual(self.fake.counts['copy'], 2)

    def test_bot_default_binding_is_exact_v1_and_ignores_no_new_identity_fields(self):
        self.settings['report_delivery'] = {'format': 'native_sheets', 'include_images': False}
        self.user_check.side_effect = AssertionError('Bot default cannot inspect the fixed User')
        _, binding, _, _ = delivery._inputs(self.settings, self.path, self.record, self.result)
        self.assertEqual(binding, {'version': delivery.VERSION, 'profile': 'report-bot',
            'app_id': self.settings['expected_lark_app_id'], 'lark_home': str(Path(self.settings['lark_home']).resolve()),
            'request_sha256': self.record['request_sha256'], 'template_override': '', 'parent_token': 'my_space',
            'data_renderer_sha256': delivery._digest(legacy.__file__),
            **{key: self.record[key] for key in ('job_key', 'sender_id', 'source_chat_id', 'source_message_id')}})
        self.assertFalse(self.fake.events)


if __name__ == '__main__':
    unittest.main()
