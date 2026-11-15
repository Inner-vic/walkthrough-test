"""Offline persistence-boundary tests; no CLI, credentials or business records."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_sheet_delivery
import walkthrough_worker as worker
from request_model import InputError
from test_native_main_report import fixture
from test_native_publish_pipeline import trusted_product


class WorkerRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'synthetic-task.json'
        self.settings = {'runtime_state_dir':str(self.root),'report_lark_profile':'synthetic-bot'}
        self.record = {'kind':'walkthrough_test_local','status':'queued',
            'vin':'DEM0VEH1CLE000001','source_chat_id':'oc_source','sender_id':'ou_requester',
            'source_message_id':'om_source','source_thread_id':'om_source',
            'report_url':'https://example.feishu.cn/docx/SyntheticMain'}
        self.content = '{"synthetic":"one reply"}'
        self.key = hashlib.sha256(b'synthetic').hexdigest()[:32]

    def save(self, record=None):
        worker.policy.atomic_json(self.path,self.record if record is None else record)

    def fresh(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def no_worker_work(self, stack):
        stack.enter_context(patch.object(worker,'load_settings',return_value=self.settings))
        for name in ('activate_lark_environment','ensure_user_authorization'):
            stack.enter_context(patch.object(worker.policy,name,side_effect=AssertionError('no authorization call')))
        stack.enter_context(patch.object(worker,'publish',side_effect=AssertionError('no publication')))
        stack.enter_context(patch.object(worker,'analyze_task',side_effect=AssertionError('no analysis')))

    def test_record_is_first_read_after_lock_and_completed_record_is_unchanged(self):
        self.save()
        completed = {**self.record,'status':'completed','report_reply_message_id':'om_existing'}
        original_open = os.open
        final_bytes = None
        def acquire(path, flags, mode=0o777):
            nonlocal final_bytes
            fd = original_open(path,flags,mode)
            if Path(path) == self.path.with_suffix('.worker.lock'):
                self.save(completed)  # Previous writer finishes before this caller owns the lock.
                final_bytes = self.path.read_bytes()
            return fd
        with contextlib.ExitStack() as stack:
            self.no_worker_work(stack)
            stack.enter_context(patch.object(worker.os,'open',side_effect=acquire))
            persisted = stack.enter_context(patch.object(worker.policy,'atomic_json',wraps=worker.policy.atomic_json))
            self.assertEqual(worker.run_worker(self.path,'unused'),0)
        self.assertEqual(self.path.read_bytes(),final_bytes)
        self.assertEqual(persisted.call_count,1)  # Only the simulated previous writer.
        self.assertFalse(self.path.with_suffix('.worker.lock').exists())

    def test_existing_worker_lock_does_not_read_or_mutate_record(self):
        self.save()
        self.path.with_suffix('.worker.lock').write_text('synthetic owner',encoding='utf-8')
        original = self.path.read_bytes()
        with contextlib.ExitStack() as stack:
            self.no_worker_work(stack)
            read = stack.enter_context(patch.object(Path,'read_text',side_effect=AssertionError('no read before lock')))
            with self.assertRaisesRegex(InputError,'已有走查 worker'):
                worker.run_worker(self.path,'unused')
            read.assert_not_called()
        self.assertEqual(self.path.read_bytes(),original)

    def test_foreign_or_invalid_record_is_not_overwritten(self):
        for value in ({'kind':'another-skill'},[],None):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value),encoding='utf-8')
                original = self.path.read_bytes()
                with contextlib.ExitStack() as stack:
                    self.no_worker_work(stack)
                    with self.assertRaises(InputError):
                        worker.run_worker(self.path,'unused')
                self.assertEqual(self.path.read_bytes(),original)
                self.assertFalse(self.path.with_suffix('.worker.lock').exists())

    def test_malformed_json_is_not_replaced_by_failed_record(self):
        self.path.write_text('{invalid',encoding='utf-8')
        with patch.object(worker,'load_settings',return_value=self.settings):
            with self.assertRaises(ValueError):
                worker.run_worker(self.path,'unused')
        self.assertEqual(self.path.read_text(encoding='utf-8'),'{invalid')
        self.assertFalse(self.path.with_suffix('.worker.lock').exists())

    def test_native_reply_requires_success_bot_and_valid_message_id(self):
        responses = [None,{}, {'ok':True,'identity':'bot','data':{}},
            {'ok':False,'identity':'bot','data':{'message_id':'om_success'}},
            {'ok':True,'identity':'user','data':{'message_id':'om_success'}},
            {'ok':True,'identity':'bot','data':{'message_id':'other'}},
            {'ok':True,'identity':'bot','data':{'message_id':7}},
            {'ok':True,'identity':'bot','data':[]}]
        for response in responses:
            with self.subTest(response=response):
                record = copy.deepcopy(self.record)
                def reply(*args,**kwargs):
                    pending = self.fresh()['report_reply_delivery']
                    self.assertIs(pending['pending'],True)
                    self.assertNotIn('receipt',pending)
                    return response
                with patch.object(worker.policy,'run_lark',side_effect=reply) as remote:
                    with self.assertRaises(InputError):
                        worker._native_source_reply(self.settings,self.path,record,self.content,self.key)
                self.assertEqual(remote.call_count,1)
                fresh = self.fresh()
                self.assertNotEqual(fresh['status'],'completed')
                self.assertIs(fresh['report_reply_delivery']['pending'],True)
                with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no resend')):
                    with self.assertRaisesRegex(InputError,'回执未知'):
                        worker._native_source_reply(self.settings,self.path,fresh,self.content,self.key)

    def test_native_send_timeout_remains_pending_and_never_repeats(self):
        with patch.object(worker.policy,'run_lark',side_effect=TimeoutError('synthetic timeout')) as remote:
            with self.assertRaises(TimeoutError):
                worker._native_source_reply(self.settings,self.path,self.record,self.content,self.key)
            with self.assertRaisesRegex(InputError,'回执未知'):
                worker._native_source_reply(self.settings,self.path,self.fresh(),self.content,self.key)
        self.assertEqual(remote.call_count,1)

    def test_successful_native_receipt_is_persisted_and_reused_without_send(self):
        response = {'ok':True,'identity':'bot','data':{'message_id':'om_delivered','unneeded':'not retained'}}
        with patch.object(worker.policy,'run_lark',return_value=response) as remote:
            self.assertEqual(worker._native_source_reply(self.settings,self.path,self.record,self.content,self.key),'om_delivered')
            self.assertEqual(worker._native_source_reply(self.settings,self.path,self.fresh(),self.content,self.key),'om_delivered')
        self.assertEqual(remote.call_count,1)
        proof = self.fresh()['report_reply_delivery']
        self.assertIs(proof['pending'],False)
        self.assertEqual(proof['receipt'],{'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}})

    def test_changed_reply_intent_or_inconsistent_receipt_never_resends(self):
        with patch.object(worker.policy,'run_lark',return_value={'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}):
            worker._native_source_reply(self.settings,self.path,self.record,self.content,self.key)
        for variant in ('content','profile','message-id','pending'):
            with self.subTest(variant=variant):
                record = self.fresh();settings = dict(self.settings);content = self.content
                if variant == 'content':content += 'changed'
                elif variant == 'profile':settings['report_lark_profile'] = 'other-bot'
                elif variant == 'message-id':record['report_reply_message_id'] = 'om_other'
                else:record['report_reply_delivery']['pending'] = True
                with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no resend')):
                    with self.assertRaises(InputError):
                        worker._native_source_reply(settings,self.path,record,content,self.key)

    def test_failed_intent_persistence_prevents_send(self):
        with patch.object(worker.policy,'atomic_json',side_effect=OSError('synthetic disk error')), \
                patch.object(worker.policy,'run_lark') as remote:
            with self.assertRaises(OSError):
                worker._native_source_reply(self.settings,self.path,self.record,self.content,self.key)
        remote.assert_not_called()

    def test_lost_receipt_persistence_keeps_unknown_send_nonrepeatable(self):
        actual_save = worker.policy.atomic_json
        def save(path, value):
            if value.get('report_reply_delivery',{}).get('receipt'):
                raise OSError('synthetic receipt disk error')
            actual_save(path,value)
        with patch.object(worker.policy,'atomic_json',side_effect=save), \
                patch.object(worker.policy,'run_lark',return_value={'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}) as remote:
            with self.assertRaises(OSError):
                worker._native_source_reply(self.settings,self.path,self.record,self.content,self.key)
        self.assertEqual(remote.call_count,1)
        self.assertIs(self.fresh()['report_reply_delivery']['pending'],True)
        with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no resend')):
            with self.assertRaisesRegex(InputError,'回执未知'):
                worker._native_source_reply(self.settings,self.path,self.fresh(),self.content,self.key)

    def publication(self, stack, *, owner=None, reply=None):
        result = fixture();details = result.pop('native_details')
        result['product_version'] = trusted_product(self.settings,self.record)
        expected = worker.build_report_document({**result,'native_details':details})
        stack.enter_context(patch.object(native_sheet_delivery,'ensure_native_sheets',return_value=details))
        for name in ('ensure_report_bot_authorization','restrict_collaborator_management',
                     'configure_report_secure_label'):
            stack.enter_context(patch.object(worker.policy,name))
        stack.enter_context(patch.object(worker.policy,'grant_source_chat_view',return_value={
            'chat_mode':'p2p','member_type':'openid','member_id':'ou_requester','perm':'view'}))
        transfer = stack.enter_context(patch.object(worker.policy,'transfer_report_owner',side_effect=owner))
        def call(settings,args,**kwargs):
            if args[:2] == ['docs','+fetch']:
                return {'ok':True,'identity':'bot','data':{'document':{'content':expected['xml']}}}
            if reply is not None:return reply(settings,args,**kwargs)
            return {'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}
        remote = stack.enter_context(patch.object(worker.policy,'run_lark',side_effect=call))
        return result,transfer,remote

    def test_unknown_main_owner_transfer_persists_pending_and_does_not_repeat(self):
        with contextlib.ExitStack() as stack:
            result,transfer,remote = self.publication(stack,owner=TimeoutError('synthetic timeout'))
            with self.assertRaises(TimeoutError):
                worker.publish(self.settings,self.path,self.record,result)
            saved = self.fresh()
            self.assertIs(saved['report_permissions']['owner_transfer_pending'],True)
            self.assertNotIn('owner_transfer',saved['report_permissions'])
            with self.assertRaisesRegex(InputError,'所有权交接回执未知'):
                worker.publish(self.settings,self.path,saved,result)
        self.assertEqual(transfer.call_count,1)
        self.assertFalse(any(c.args[1][:2] == ['im','+messages-reply'] for c in remote.call_args_list))

    def test_native_completed_requires_message_receipt_and_preserves_asset_links(self):
        with contextlib.ExitStack() as stack:
            result,transfer,_ = self.publication(stack,reply=lambda *a,**k:{'ok':True,'identity':'bot','data':{}})
            with self.assertRaisesRegex(InputError,'消息 ID'):
                worker.publish(self.settings,self.path,self.record,result)
        saved = self.fresh()
        self.assertNotEqual(saved['status'],'completed')
        self.assertEqual(saved['report_url'],self.record['report_url'])
        self.assertIs(saved['report_permissions']['owner_transfer'],True)
        self.assertNotIn('owner_transfer_pending',saved['report_permissions'])
        self.assertIs(saved['report_reply_delivery']['pending'],True)

    def test_receipt_survives_completion_save_failure_without_second_send(self):
        actual_save = worker.policy.atomic_json
        fail_once = True
        def save(path, record):
            nonlocal fail_once
            if record.get('status') == 'completed' and fail_once:
                fail_once = False
                raise OSError('synthetic completion save error')
            actual_save(path,record)
        with contextlib.ExitStack() as stack:
            result,transfer,remote = self.publication(stack)
            stack.enter_context(patch.object(worker.policy,'atomic_json',side_effect=save))
            with self.assertRaises(OSError):
                worker.publish(self.settings,self.path,self.record,result)
            saved = self.fresh()
            self.assertEqual(saved['status'],'delivering')
            self.assertEqual(saved['report_reply_message_id'],'om_delivered')
            worker.publish(self.settings,self.path,saved,result)
        self.assertEqual(sum(c.args[1][:2] == ['im','+messages-reply'] for c in remote.call_args_list),1)
        self.assertEqual(transfer.call_count,1)
        self.assertEqual(self.fresh()['status'],'completed')


if __name__ == '__main__':
    unittest.main()
