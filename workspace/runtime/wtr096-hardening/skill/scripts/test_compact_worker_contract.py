"""Offline contracts for opt-in presentation and source-reply versioning."""
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_sheet_delivery
from request_model import InputError
import walkthrough_worker as worker
from test_native_main_report import fixture
from test_native_publish_pipeline import trusted_product
import test_product_pipeline as pipeline_fixture


class CompactWorkerContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'synthetic.json'
        self.settings = {'runtime_state_dir':str(self.root),'report_lark_profile':'synthetic-bot'}
        self.record = {'kind':'walkthrough_test_local','status':'queued','vin':'DEM0VEH1CLE000001',
            'source_chat_id':'oc_source','source_message_id':'om_source','sender_id':'ou_requester',
            'presentation_version':worker.PRESENTATION_VERSION}
        self.content = json.dumps({'text':'https://example.feishu.cn/docx/Synthetic\nVIN：synthetic'})
        self.key = hashlib.sha256(b'synthetic-reply').hexdigest()[:32]
        self.receipt = {'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('socket.socket',side_effect=AssertionError('no network')))
        stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('no real process')))

    def saved(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def reply(self, record=None, *, msg_type='text', response=None):
        record = self.record if record is None else record
        with patch.object(worker.policy,'run_lark',return_value=response or self.receipt) as remote:
            value = worker._native_source_reply(self.settings,self.path,record,self.content,self.key,msg_type=msg_type)
        return value,remote

    def test_text_success_persists_v2_type_and_reuses_receipt_without_send(self):
        value,remote = self.reply()
        self.assertEqual(value,'om_delivered')
        args = remote.call_args.args[1]
        self.assertEqual(args[args.index('--msg-type')+1],'text')
        self.assertEqual(args[args.index('--content')+1],self.content)
        saved = self.saved()
        delivery = saved['report_reply_delivery']
        self.assertEqual(delivery['version'],'native-source-reply-v2')
        self.assertEqual(delivery['intent'],{'source_message_id':'om_source','profile':'synthetic-bot',
            'idempotency_key':self.key,'content_sha256':hashlib.sha256(self.content.encode()).hexdigest(),
            'msg_type':'text'})
        self.assertIs(delivery['pending'],False)
        self.assertEqual(delivery['receipt'],self.receipt)
        with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no second send')):
            self.assertEqual(worker._native_source_reply(self.settings,self.path,saved,self.content,self.key,
                msg_type='text'),'om_delivered')

    def test_text_unknown_or_invalid_receipt_stays_pending_without_resend(self):
        failures = [TimeoutError('synthetic timeout'),
            {'ok':True,'identity':'bot','data':{}},
            {'ok':True,'identity':'user','data':{'message_id':'om_delivered'}}]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                record = copy.deepcopy(self.record)
                with patch.object(worker.policy,'run_lark',side_effect=failure if isinstance(failure,Exception) else None,
                                  return_value=None if isinstance(failure,Exception) else failure) as remote:
                    with self.assertRaises((TimeoutError,InputError)):
                        worker._native_source_reply(self.settings,self.path,record,self.content,self.key,msg_type='text')
                self.assertEqual(remote.call_count,1)
                saved = self.saved()
                self.assertIs(saved['report_reply_delivery']['pending'],True)
                self.assertNotIn('report_reply_message_id',saved)
                with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no second send')):
                    with self.assertRaisesRegex(InputError,'回执未知'):
                        worker._native_source_reply(self.settings,self.path,saved,self.content,self.key,msg_type='text')

    def test_type_and_checkpoint_version_drift_never_resends(self):
        self.reply()
        for change in ('post','version','intent-type','record-version'):
            with self.subTest(change=change):
                record = self.saved()
                msg_type = 'text'
                if change == 'post':msg_type = 'post'
                elif change == 'version':record['report_reply_delivery']['version'] = 'native-source-reply-v1'
                elif change == 'intent-type':record['report_reply_delivery']['intent']['msg_type'] = 'post'
                else:record.pop('presentation_version')
                with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no second send')):
                    with self.assertRaises(InputError):
                        worker._native_source_reply(self.settings,self.path,record,self.content,self.key,msg_type=msg_type)

    def test_old_post_checkpoint_shape_and_recovery_are_unchanged(self):
        record = copy.deepcopy(self.record)
        record.pop('presentation_version')
        value,remote = self.reply(record,msg_type='post')
        self.assertEqual(value,'om_delivered')
        args = remote.call_args.args[1]
        self.assertEqual(args[args.index('--msg-type')+1],'post')
        delivery = self.saved()['report_reply_delivery']
        self.assertEqual(delivery['version'],'native-source-reply-v1')
        self.assertNotIn('msg_type',delivery['intent'])
        with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no second send')):
            self.assertEqual(worker._native_source_reply(self.settings,self.path,self.saved(),self.content,self.key),
                'om_delivered')

    def test_mixed_record_and_result_presentation_rejected_before_any_cloud_or_persist(self):
        for record_version,result_version in ((None,worker.PRESENTATION_VERSION),
                (worker.PRESENTATION_VERSION,None),('unknown',worker.PRESENTATION_VERSION),
                (worker.PRESENTATION_VERSION,'unknown')):
            with self.subTest(record_version=record_version,result_version=result_version):
                record = copy.deepcopy(self.record)
                result = {'delivery_format':worker.DELIVERY_FORMAT}
                if record_version is None:record.pop('presentation_version')
                else:record['presentation_version'] = record_version
                if result_version is not None:result['presentation_version'] = result_version
                before = (copy.deepcopy(record),copy.deepcopy(result))
                with patch.object(worker.policy,'run_lark',side_effect=AssertionError('no cloud')), \
                        patch.object(worker.policy,'atomic_json',side_effect=AssertionError('no persist')), \
                        patch.object(worker.policy,'ensure_report_bot_authorization',side_effect=AssertionError('no auth')):
                    with self.assertRaisesRegex(InputError,'呈现版本不一致'):
                        worker.publish(self.settings,self.path,record,result)
                self.assertEqual((record,result),before)

    def publish_boundaries(self, stack, *, compact=True, notification=None):
        result = fixture()
        details = result.pop('native_details')
        result['product_version'] = trusted_product(self.settings,self.record)
        self.record['report_url'] = 'https://example.feishu.cn/docx/SyntheticMain'
        if compact:result['presentation_version'] = worker.PRESENTATION_VERSION
        else:self.record.pop('presentation_version',None)
        for name in ('ensure_report_bot_authorization','restrict_collaborator_management',
                     'grant_source_chat_view','transfer_report_owner','configure_report_secure_label'):
            stack.enter_context(patch.object(worker.policy,name,return_value={'perm':'view'}))
        stack.enter_context(patch.object(native_sheet_delivery,'ensure_native_sheets',return_value=details))
        stack.enter_context(patch.object(worker,'build_report_document',return_value={'xml':'synthetic'}))
        stack.enter_context(patch('feishu_report.verify_main_report_document',return_value={'data_cells_verified':48}))
        text = {'text':'https://example.feishu.cn/docx/SyntheticMain\nVIN：synthetic\nBase：fixed\nPCM更新：fixed'}
        stack.enter_context(patch.object(worker,'build_report_notification',return_value=
            (text if compact else {'zh_cn':{'title':'old','content':[]}}) if notification is None else notification))
        def call(settings,args,**kwargs):
            if args[:2] == ['docs','+fetch']:
                return {'ok':True,'identity':'bot','data':{'document':{'content':'synthetic'}}}
            return self.receipt
        remote = stack.enter_context(patch.object(worker.policy,'run_lark',side_effect=call))
        return result,remote

    def test_publish_matching_v2_selects_text_and_old_v1_remains_post(self):
        for compact in (True,False):
            with self.subTest(compact=compact):
                self.record.pop('report_reply_delivery',None)
                self.record.pop('report_reply_message_id',None)
                self.record['presentation_version'] = worker.PRESENTATION_VERSION
                with contextlib.ExitStack() as stack:
                    result,remote = self.publish_boundaries(stack,compact=compact)
                    worker.publish(self.settings,self.path,self.record,result)
                sends = [c.args[1] for c in remote.call_args_list if c.args[1][:2] == ['im','+messages-reply']]
                self.assertEqual(len(sends),1)
                args = sends[0]
                self.assertEqual(args[args.index('--msg-type')+1],'text' if compact else 'post')
                self.assertEqual(self.record['status'],'completed')
                self.assertEqual(self.record['report_reply_message_id'],'om_delivered')

    def test_matching_v2_rejects_nontext_notification_without_sending(self):
        with contextlib.ExitStack() as stack:
            result,remote = self.publish_boundaries(stack,notification={'zh_cn':{'content':[]}})
            with self.assertRaisesRegex(InputError,'纯文本'):
                worker.publish(self.settings,self.path,self.record,result)
        self.assertFalse(any(c.args[1][:2] == ['im','+messages-reply'] for c in remote.call_args_list))

    def test_completed_v2_record_is_not_republished_or_modified(self):
        self.record.update(status='completed',report_reply_message_id='om_delivered')
        self.path.write_text(json.dumps(self.record),encoding='utf-8')
        original = self.path.read_bytes()
        with patch.object(worker,'load_settings',return_value=self.settings), \
                patch.object(worker,'publish',side_effect=AssertionError('no repeat publish')), \
                patch.object(worker.policy,'activate_lark_environment',side_effect=AssertionError('no auth')):
            self.assertEqual(worker.run_worker(self.path,'unused'),0)
        self.assertEqual(self.path.read_bytes(),original)

    def test_new_record_is_v2_but_exact_old_source_replay_is_not_migrated(self):
        identity = {'source_message_id':'om_start','source_chat_id':'oc_source','sender_id':'ou_requester',
            'source_thread_id':'om_start'}
        metadata = {'vin':self.record['vin'],'start':'2026-01-01T10:00:00','end':'2026-01-01T10:30:00'}
        request = {'vin':self.record['vin'],'baseline_metadata':metadata,
            'replacement_metadata':{**metadata,'start':'2026-01-01T11:00:00','end':'2026-01-01T11:30:00'}}
        settings = {'runtime_state_dir':str(self.root),'workspace_root':str(self.root)}
        args = SimpleNamespace(text='synthetic body',ccconnect_context='synthetic context',
            settings=self.root/'unused-settings.json',received_at='2026-01-01T12:00:00+08:00')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(worker,'load_settings',return_value=settings))
            stack.enter_context(patch.object(worker,'request_identity',return_value=identity))
            stack.enter_context(patch.object(worker,'source_normalization_anchor',return_value=args.received_at))
            stack.enter_context(patch.object(worker,'route',return_value={'status':'ready','normalized':request}))
            for name in ('activate_lark_environment','ensure_report_bot_authorization','ensure_secure_label_authorization'):
                stack.enter_context(patch.object(worker.policy,name))
            stack.enter_context(patch.object(worker.policy,'ensure_user_authorization',return_value={'open_id':'ou_operator'}))
            stack.enter_context(patch.object(worker.policy,'resolve_report_secure_label',return_value={'synthetic':True}))
            process = stack.enter_context(patch.object(worker.subprocess,'Popen'))
            result = worker.start_job(args)
            record_path = self.root/(result['job_key']+'.json')
            record = json.loads(record_path.read_text(encoding='utf-8'))
            self.assertEqual(record['presentation_version'],worker.PRESENTATION_VERSION)
            record.pop('presentation_version')
            record_path.write_text(json.dumps(record),encoding='utf-8')
            original = record_path.read_bytes()
            replay = worker.start_job(args)
        self.assertTrue(replay['idempotent'])
        self.assertEqual(record_path.read_bytes(),original)
        self.assertEqual(process.call_count,1)

    def test_final_result_inherits_only_explicit_new_record_marker(self):
        helper = pipeline_fixture.ProductPipelineTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        for compact in (True,False):
            with self.subTest(compact=compact):
                if compact:helper.record['presentation_version'] = worker.PRESENTATION_VERSION
                else:helper.record.pop('presentation_version',None)
                value = helper.local(helper.root/('v2' if compact else 'v1'))
                self.assertEqual(value.get('presentation_version'),worker.PRESENTATION_VERSION if compact else None)
                self.assertEqual('presentation_version' in value,compact)


if __name__ == '__main__':
    unittest.main()
