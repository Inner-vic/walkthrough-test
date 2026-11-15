"""Offline source-access publication tests; all Feishu calls are mocked."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import report_policy as policy
import walkthrough_worker as worker
from feishu_report import METRICS, SCENARIOS
from request_model import InputError


class SourceChatAccessTests(unittest.TestCase):
    SETTINGS = {'lark_profile': 'offline-user', 'report_lark_profile': 'offline-report-bot',
                'expected_operator_open_id': 'ou_operator'}
    URL = 'https://example.feishu.cn/docx/OfflineReport'
    CHAT = 'oc_sourcechat'
    REQUESTER = 'ou_requester'

    @staticmethod
    def response(mode):
        return {'ok': True, 'identity': 'bot', 'data': {'chat_mode': mode, 'chat_status': 'normal'}}

    def test_p2p_grants_only_requester_not_fixed_operator(self):
        with patch.object(policy, 'run_lark', side_effect=[self.response('p2p'), {'ok': True}]) as run:
            audit = policy.grant_source_chat_view(self.SETTINGS, self.URL, self.CHAT, self.REQUESTER)
        self.assertEqual(audit, {'chat_mode': 'p2p', 'member_type': 'openid',
                                 'member_id': self.REQUESTER, 'perm': 'view'})
        self.assertEqual(run.call_args_list[0].args[1],
                         ['im', 'chats', 'get', '--chat-id', self.CHAT, '--as', 'bot'])
        self.assert_grant(run.call_args_list[1], 'openid', self.REQUESTER)
        self.assertNotIn(self.SETTINGS['expected_operator_open_id'], run.call_args_list[1].args[1])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['profile'], self.SETTINGS['report_lark_profile'])

    def assert_grant(self, call, member_type, member_id):
        self.assertEqual(call.args[1], ['drive', '+member-add', '--as', 'bot', '--token', self.URL,
                                       '--member-id', member_id, '--member-type', member_type,
                                       '--perm', 'view', '--yes'])

    def test_group_and_topic_keep_openchat_view(self):
        for mode in ('group', 'topic'):
            with self.subTest(mode=mode), patch.object(policy, 'run_lark',
                    side_effect=[self.response(mode), {'ok': True}]) as run:
                audit = policy.grant_source_chat_view(self.SETTINGS, self.URL, 'chat:' + self.CHAT, self.REQUESTER)
                self.assertEqual(audit, {'chat_mode': mode, 'member_type': 'openchat',
                                         'member_id': self.CHAT, 'perm': 'view'})
                self.assert_grant(run.call_args_list[1], 'openchat', self.CHAT)
                self.assertTrue(all(c.kwargs['profile'] == 'offline-report-bot' for c in run.call_args_list))

    def test_unknown_or_untrusted_chat_response_never_grants(self):
        responses = [self.response('unknown'), self.response(None),
                     {'ok': True, 'identity': 'user', 'data': {'chat_mode': 'p2p'}},
                     {'ok': False, 'identity': 'bot', 'data': {'chat_mode': 'group'}},
                     {'ok': True, 'identity': 'bot', 'data': None}, None]
        for response in responses:
            with self.subTest(response=response), patch.object(policy, 'run_lark', return_value=response) as run:
                with self.assertRaises(InputError):
                    policy.grant_source_chat_view(self.SETTINGS, self.URL, self.CHAT, self.REQUESTER)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[1][:3], ['im', 'chats', 'get'])

    def test_chat_query_errors_never_grant_or_fallback(self):
        for failure in (InputError('offline query failed'), TimeoutError('offline timeout')):
            with self.subTest(failure=type(failure).__name__), patch.object(policy, 'run_lark', side_effect=failure) as run:
                with self.assertRaises(type(failure)):
                    policy.grant_source_chat_view(self.SETTINGS, self.URL, self.CHAT, self.REQUESTER)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs['profile'], 'offline-report-bot')

    def test_missing_or_invalid_requester_never_uses_operator_or_chat(self):
        for requester in (None, '', 'user_id', self.CHAT, 'ou_bad value'):
            with self.subTest(requester=requester), patch.object(policy, 'run_lark') as run:
                with self.assertRaises(InputError):
                    policy.grant_source_chat_view(self.SETTINGS, self.URL, self.CHAT, requester)
                run.assert_not_called()

    def test_report_profile_is_required_without_default_identity(self):
        with patch.object(policy, 'run_lark') as run, self.assertRaises(InputError):
            policy.grant_source_chat_view({'lark_profile': 'offline-user'}, self.URL, self.CHAT, self.REQUESTER)
        run.assert_not_called()

    def publish_fixture(self):
        record = {'report_url': self.URL, 'source_chat_id': self.CHAT, 'sender_id': self.REQUESTER,
                  'source_message_id': 'om_source', 'source_thread_id': 'om_source',
                  'vin': 'DEM0VEH1CLE000001', 'status': 'failed', 'error': 'prior safe failure',
                  'failed_at': '2026-10-23T00:00:00Z',
                  'report_permissions': {'collaborator_management': True}}
        audit = {'chat_mode': 'p2p', 'member_type': 'openid', 'member_id': self.REQUESTER, 'perm': 'view'}
        return record, audit

    def publish_patches(self, stack, audit):
        stack.enter_context(patch.object(policy, 'ensure_report_bot_authorization'))
        stack.enter_context(patch.object(worker, 'create_report', side_effect=AssertionError('must reuse report')))
        grant = stack.enter_context(patch.object(policy, 'grant_source_chat_view', return_value=audit))
        stack.enter_context(patch.object(policy, 'transfer_report_owner'))
        stack.enter_context(patch.object(policy, 'configure_report_secure_label'))
        reply = stack.enter_context(patch.object(policy, 'run_lark', return_value={'ok': True}))
        return grant, reply

    def test_publish_passes_real_requester_persists_audit_and_clears_only_completed_error(self):
        record, audit = self.publish_fixture()
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            path = Path(temporary) / 'offline-task.json'
            grant, reply = self.publish_patches(stack, audit)
            worker.publish(self.SETTINGS, path, record, {'status': 'partial'})
            grant.assert_called_once_with(self.SETTINGS, self.URL, self.CHAT, self.REQUESTER)
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved['report_permissions']['source_chat_access'], audit)
            self.assertTrue(saved['report_permissions']['source_chat_view'])
            self.assertEqual(saved['report_url'], self.URL)
            self.assertEqual(saved['status'], 'completed')
            self.assertNotIn('error', saved)
            self.assertNotIn('failed_at', saved)
            self.assertEqual(reply.call_args.kwargs['profile'], 'offline-report-bot')
            self.assertNotIn('--reply-in-thread',reply.call_args.args[1])
            target=reply.call_args.args[1]
            self.assertEqual(target[target.index('--message-id')+1],record['source_message_id'])

    def test_failed_source_access_is_not_persisted_as_success_or_replied(self):
        record, audit = self.publish_fixture()
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            grant, reply = self.publish_patches(stack, audit)
            grant.side_effect = InputError('source lookup failed')
            with self.assertRaises(InputError):
                worker.publish(self.SETTINGS, Path(temporary) / 'offline-task.json', record, {'status': 'partial'})
            self.assertNotIn('source_chat_view', record['report_permissions'])
            self.assertNotIn('source_chat_access', record['report_permissions'])
            self.assertEqual(record['error'], 'prior safe failure')
            self.assertIn('failed_at', record)
            reply.assert_not_called()

    def test_reply_failure_does_not_clear_prior_failure_fields(self):
        record, audit = self.publish_fixture()
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            _, reply = self.publish_patches(stack, audit)
            reply.side_effect = InputError('reply failed')
            with self.assertRaises(InputError):
                worker.publish(self.SETTINGS, Path(temporary) / 'offline-task.json', record, {'status': 'partial'})
            self.assertNotEqual(record['status'], 'completed')
            self.assertEqual(record['error'], 'prior safe failure')
            self.assertIn('failed_at', record)

    def test_reply_uses_original_message_in_main_chat_and_persists_id(self):
        record, audit = self.publish_fixture()
        record['source_thread_id'] = 'om_existing_thread_root'
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            _, reply = self.publish_patches(stack, audit)
            reply.return_value = {'ok':True,'data':{'message_id':'om_reportreply'}}
            worker.publish(self.SETTINGS,Path(temporary)/'offline-task.json',record,{'status':'partial'})
            args = reply.call_args.args[1]
            self.assertNotIn('--reply-in-thread',args)
            self.assertEqual(args[args.index('--message-id')+1],'om_source')
            self.assertEqual(record['report_reply_mode'],'source_message_in_chat')
            self.assertEqual(record['report_reply_message_id'],'om_reportreply')
            self.assertEqual(record['source_thread_id'],'om_existing_thread_root')

    def test_report_reply_is_one_concise_delivery_without_internal_status(self):
        for status in ('ok','partial'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
                record,audit=self.publish_fixture()
                _,reply=self.publish_patches(stack,audit)
                result={'status':status,'comparisons':{scenario:{'metrics':{
                    metric:{'baseline':1,'replacement':1,'baseline_status':'value','replacement_status':'value'}
                    for metric,_ in METRICS}} for scenario in SCENARIOS}}
                worker.publish(self.SETTINGS,Path(temporary)/'offline-task.json',record,result)
                reply.assert_called_once()
                args=reply.call_args.args[1]
                content=json.loads(args[args.index('--content')+1])['zh_cn']
                self.assertEqual(content,{'title':'走查测试报告已生成','content':[
                    [{'tag':'text','text':'VIN：'+record['vin']}],
                    [{'tag':'a','text':'查看走查测试报告','href':self.URL}]]})

    def test_delivery_retry_uses_same_source_message_idempotency_key(self):
        record,audit=self.publish_fixture()
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            _,reply=self.publish_patches(stack,audit)
            reply.side_effect=[InputError('temporary delivery failure'),{'ok':True}]
            path=Path(temporary)/'offline-task.json'
            with self.assertRaises(InputError):
                worker.publish(self.SETTINGS,path,record,{'status':'partial'})
            worker.publish(self.SETTINGS,path,record,{'status':'partial'})
            first,second=[call.args[1] for call in reply.call_args_list]
            self.assertEqual(first[first.index('--idempotency-key')+1],second[second.index('--idempotency-key')+1])
            self.assertEqual(first[first.index('--message-id')+1],'om_source')
            self.assertEqual(second[second.index('--message-id')+1],'om_source')
            self.assertEqual(record['status'],'completed')

    def test_failed_phase_persists_elapsed_time_and_failure_without_exception_detail(self):
        record = {}
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)/'offline-task.json'
            with patch.object(worker,'perf_counter',side_effect=[10.0,12.5]):
                with self.assertRaises(InputError):
                    with worker.phase(target,record,'offline_phase'):
                        raise InputError('private diagnostic')
            saved=json.loads(target.read_text('utf-8'))
            self.assertEqual(saved['phase_timings'][0]['elapsed_seconds'],2.5)
            self.assertEqual(saved['phase_timings'][0]['outcome'],'failed')
            self.assertNotIn('private diagnostic',target.read_text('utf-8'))

    def test_successful_reply_without_optional_mapping_metadata_still_completes(self):
        for data in (None,[],['optional'],'optional'):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
                record,audit=self.publish_fixture()
                _,reply=self.publish_patches(stack,audit)
                reply.return_value={'ok':True,'data':data}
                worker.publish(self.SETTINGS,Path(temporary)/'offline-task.json',record,{'status':'partial'})
                self.assertEqual(record['status'],'completed')
                self.assertNotIn('report_reply_message_id',record)
                reply.assert_called_once()


if __name__ == '__main__':
    unittest.main()
