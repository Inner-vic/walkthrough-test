import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import product_version as product
from request_model import InputError


VIN = 'L1234567890123456'


class ProductVersionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir())/('product-test-'+uuid.uuid4().hex[:10])
        self.root.mkdir()
        self.settings = {'lark_profile':'project-user', 'report_lark_profile':'project-bot',
                         'lark_home':str(self.root/'identity'), 'expected_lark_app_id':'cli_report',
                         'expected_dispatch_open_id':'ou_operator',
                         'product_version':{'query_chat_id':'oc_fixed', 'asgard_bot_id':'ou_asgard',
                                            'asgard_app_id':'cli_asgard', 'timeout_seconds':1,
                                            'poll_interval_seconds':0.1}}
        self.context = {'vin':VIN, 'task_key':'task_one', 'operator_open_id':'ou_operator'}
        self.calls = []
        self.auth = patch.object(product.policy, 'ensure_user_authorization', return_value={'open_id':'ou_operator'})
        self.auth.start(); self.addCleanup(self.auth.stop)
        self.addCleanup(lambda:shutil.rmtree(self.root, ignore_errors=True))

    def root_message(self, message_id='om_query', thread_id='omt_query'):
        root = {'message_id':message_id, 'chat_id':'oc_fixed', 'create_time':1792801020000,
                'sender':{'id':'ou_operator', 'id_type':'open_id', 'sender_type':'user'},
                'content':'@阿斯加德 查询车辆版本 '+VIN+' CCUFSD'}
        if thread_id:
            root['thread_id'] = thread_id
        return root

    def reply(self, code='9AEE', message_id='om_reply', thread='omt_query'):
        return {'message_id':message_id, 'chat_id':'oc_fixed', 'thread_id':thread,
                'sender':{'id':'cli_asgard', 'id_type':'app_id', 'open_bot_id':'ou_asgard', 'sender_type':'app'},
                'create_time':'2026-10-23 16:17',
                'content':'<card>\n**相关制品信息:** '+code+', 9.2.0-4697\n'
                          '**CCUFSD_SOC:** CCUFSD_SOC_ota-9.2.0-1378.0001\n'
                          '**上报时间:** 2026-09-09 10:11:38\n**云端环境:** testtwo\n</card>'}

    def envelope(self, messages, **kwargs):
        return {'ok':True, 'identity':'user', 'data':{'messages':messages, **kwargs}}

    def transport(self, settings, args, **kwargs):
        self.calls.append((args, kwargs))
        if '+messages-send' in args:
            return {'ok':True, 'identity':'user', 'data':{'message_id':'om_query', 'chat_id':'oc_fixed', 'create_time':'123'}}
        if '+messages-mget' in args:
            return self.envelope([self.root_message()])
        if '+threads-messages-list' in args:
            return self.envelope([self.root_message(), self.reply()], has_more=False)
        self.fail('Unexpected transport operation')

    def run_query(self, transport=None, directory=None):
        with patch.object(product.policy, 'run_lark', side_effect=transport or self.transport):
            return product.get_product_version(self.settings, self.context, directory or self.root/'state')

    def test_only_version_send_fixed_group_and_explicit_user_profile(self):
        events=[]
        with patch.object(product.policy, 'run_lark', side_effect=self.transport):
            result=product.get_product_version(self.settings,self.context,self.root/'state',on_stage=events.append)
        self.assertEqual(result['status'],'ok'); self.assertEqual(result['code'],'9AEE')
        self.assertEqual(result['artifact_info'],'9AEE, 9.2.0-4697')
        self.assertEqual(result['source_reported_at'],'2026-09-09 10:11:38')
        self.assertEqual(result['response_at'],'2026-10-23 16:17')
        self.assertEqual(result['query']['create_time'],1792801020000)
        self.assertFalse(result['historical_version_verified'])
        self.assertEqual(result['quality']['issues'][1]['kind'],'product_soc_version_mismatch')
        self.assertEqual([e['stage'] for e in events],['sending','waiting_reply','ready'])
        send=self.calls[0][0]
        self.assertIn('+messages-send',send); self.assertNotIn('+messages-reply',send)
        self.assertEqual(send[send.index('--chat-id')+1],'oc_fixed')
        self.assertNotIn('fsdlog',str(self.calls))
        for args,kwargs in self.calls:
            self.assertEqual(args[args.index('--as')+1],'user')
            self.assertEqual(kwargs['profile'],'project-user')
        self.assertEqual(product.validate_result(self.settings,self.context,result),result)

    def test_complete_checkpoint_reuses_result_without_send_or_read(self):
        first=self.run_query()
        with patch.object(product.policy,'run_lark',side_effect=AssertionError('no network')):
            second=product.get_product_version(self.settings,self.context,self.root/'state')
        self.assertEqual(first,second)

    def test_unknown_send_ack_never_blindly_resends_even_on_retry(self):
        with patch.object(product.policy,'run_lark',side_effect=RuntimeError('token=SECRET')) as transport:
            first=product.get_product_version(self.settings,self.context,self.root/'state')
            second=product.get_product_version(self.settings,self.context,self.root/'state')
        self.assertEqual(first['status'],'unknown'); self.assertEqual(second['status'],'unknown')
        self.assertEqual(transport.call_count,1)
        self.assertNotIn('SECRET',json.dumps(first))

    def test_send_pending_survives_interrupted_process_without_second_send(self):
        with patch.object(product.policy,'run_lark',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                product.get_product_version(self.settings,self.context,self.root/'state')
        with patch.object(product.policy,'run_lark',side_effect=AssertionError('must not send')):
            result=product.get_product_version(self.settings,self.context,self.root/'state')
        self.assertEqual(result['status'],'unknown')

    def test_binding_changes_are_hard_errors_before_network(self):
        self.run_query()
        variations=[('product_version','query_chat_id','oc_other'),('product_version','asgard_bot_id','ou_other'),
                    ('product_version','asgard_app_id','cli_other'),(None,'lark_profile','other-user'),
                    (None,'lark_home',str(self.root/'other')), (None,'expected_lark_app_id','cli_other')]
        original=copy.deepcopy(self.settings)
        for section,key,value in variations:
            self.settings=copy.deepcopy(original)
            (self.settings[section] if section else self.settings)[key]=value
            with self.subTest(key=key),patch.object(product.policy,'run_lark',side_effect=AssertionError('no send')):
                with self.assertRaises(InputError):
                    product.get_product_version(self.settings,self.context,self.root/'state')
        self.settings=original
        for key,value in [('vin','L2234567890123456'),('task_key','other_task'),('operator_open_id','ou_other')]:
            context={**self.context,key:value}
            with self.subTest(key=key),self.assertRaises(InputError):
                product.get_product_version(self.settings,context,self.root/'state')

    def test_exact_root_and_reply_sender_app_chat_thread_are_verified(self):
        variants=[('root_user',lambda root,reply:root['sender'].update(id='ou_other')),
                  ('root_chat',lambda root,reply:root.update(chat_id='oc_other')),
                  ('root_vin',lambda root,reply:root.update(content='查询车辆版本 L2234567890123456 CCUFSD')),
                  ('reply_app',lambda root,reply:reply['sender'].update(id='cli_other')),
                  ('reply_chat',lambda root,reply:reply.update(chat_id='oc_other')),
                  ('reply_thread',lambda root,reply:reply.update(thread_id='omt_other'))]
        for name,mutate in variants:
            root,reply=self.root_message(),self.reply(); mutate(root,reply)
            def transport(settings,args,**kwargs):
                if '+messages-mget' in args:return self.envelope([root])
                if '+threads-messages-list' in args:return self.envelope([reply],has_more=False)
                return self.transport(settings,args,**kwargs)
            with self.subTest(name=name):
                self.assertEqual(self.run_query(transport,self.root/name)['status'],'failed')

    def test_untrusted_bot_ignored_and_conflicting_or_non_four_hex_rejected(self):
        for name,replies in [('conflict',[self.reply(),self.reply('ABCD','om_second')]),
                             ('nonhex',[self.reply('ZZZZ')]),('long',[self.reply('9AEEA')]),
                             ('lower',[self.reply('9aee')])]:
            def transport(settings,args,**kwargs):
                if '+threads-messages-list' in args:return self.envelope(replies,has_more=False)
                return self.transport(settings,args,**kwargs)
            with self.subTest(name=name):
                self.assertEqual(self.run_query(transport,self.root/name)['status'],'failed')
        binding=product._binding(self.settings,self.context,product.validate_config(self.settings))
        untrusted=self.reply();untrusted['sender']['open_bot_id']='ou_other'
        self.assertIsNone(product._extract_replies([untrusted],{'binding':binding,'query':{'message_id':'om_query'}},'omt_query'))

    def test_read_failure_resume_reads_original_message_without_resend(self):
        def broken(settings,args,**kwargs):
            if '+messages-mget' in args:raise RuntimeError('sensitive')
            return self.transport(settings,args,**kwargs)
        first=self.run_query(broken)
        self.assertEqual(first['status'],'failed')
        self.calls=[]
        result=self.run_query()
        self.assertEqual(result['status'],'ok')
        self.assertFalse(any('+messages-send' in args for args,_ in self.calls))

    def test_truncated_thread_replies_fail_instead_of_accepting_first_code(self):
        def transport(settings,args,**kwargs):
            if '+threads-messages-list' in args:return self.envelope([self.reply()],has_more=True)
            return self.transport(settings,args,**kwargs)
        self.assertEqual(self.run_query(transport)['status'],'failed')

    def direct_transport(self, settings, args, **kwargs):
        self.calls.append((args,kwargs))
        reply=self.reply();reply.pop('thread_id');reply['reply_to']='om_query'
        if '+messages-send' in args:
            return {'ok':True,'identity':'user','data':{'message_id':'om_query','chat_id':'oc_fixed'}}
        if '+messages-mget' in args:
            requested=args[args.index('--message-ids')+1]
            return self.envelope([self.root_message(thread_id=None)] if requested=='om_query' else [reply])
        if '+chat-messages-list' in args:
            unrelated=self.reply(message_id='om_other');unrelated.pop('thread_id')
            unrelated['reply_to']='om_somebodyelse'
            return self.envelope([unrelated,reply],has_more=False)
        self.fail('unexpected direct-reply operation')

    def test_direct_reply_to_discovery_and_readback_preserve_exact_association(self):
        result=self.run_query(self.direct_transport)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['response']['reply_to'],'om_query')
        self.assertEqual(result['response']['correlation'],{'kind':'reply_to','query_message_id':'om_query'})
        listing=next(args for args,_ in self.calls if '+chat-messages-list' in args)
        self.assertIn('--page-all',listing);self.assertIn('--start',listing);self.assertIn('--end',listing)
        self.assertEqual(self.calls[-1][0][self.calls[-1][0].index('--message-ids')+1],'om_reply')
        self.assertEqual(product.validate_result(self.settings,self.context,result),result)

    def test_no_thread_embedded_preview_is_not_accepted_without_reply_to(self):
        def transport(settings,args,**kwargs):
            if '+messages-mget' in args:
                root=self.root_message(thread_id=None)
                reply=self.reply();reply.pop('thread_id')
                root['thread_replies']=[reply]
                return self.envelope([root])
            if '+chat-messages-list' in args:
                return self.envelope([self.reply()],has_more=False)
            return self.transport(settings,args,**kwargs)
        with patch.object(product.time,'monotonic',side_effect=[0,0,0,2,2]):
            self.assertEqual(self.run_query(transport)['status'],'timeout')

    def test_direct_reply_readback_must_match_query_chat_sender_and_app(self):
        mutations=[lambda r:r.update(reply_to='om_other'),lambda r:r.update(chat_id='oc_other'),
                   lambda r:r['sender'].update(open_bot_id='ou_other'),lambda r:r['sender'].update(id='cli_other')]
        for index,mutate in enumerate(mutations):
            def transport(settings,args,**kwargs):
                payload=self.direct_transport(settings,args,**kwargs)
                if '+messages-mget' in args and args[args.index('--message-ids')+1]=='om_reply':
                    mutate(payload['data']['messages'][0])
                return payload
            with self.subTest(index=index):
                self.assertEqual(self.run_query(transport,self.root/str(index))['status'],'failed')

    def test_direct_reply_discovery_cannot_accept_incomplete_page(self):
        def transport(settings,args,**kwargs):
            payload=self.direct_transport(settings,args,**kwargs)
            if '+chat-messages-list' in args:
                payload['meta']={'pagination':{'complete':False}}
            return payload
        self.assertEqual(self.run_query(transport)['status'],'failed')

    def test_direct_reply_correlation_is_checked_when_result_resumed(self):
        result=self.run_query(self.direct_transport)
        for mutate in (lambda r:r['response'].pop('correlation'),lambda r:r['response'].update(reply_to='om_other')):
            changed=copy.deepcopy(result);mutate(changed)
            with self.assertRaises(InputError):product.validate_result(self.settings,self.context,changed)

    def test_original_timeout_checkpoint_resumes_direct_reply_without_send(self):
        def broken(settings,args,**kwargs):
            if '+messages-mget' in args:raise RuntimeError('read temporarily unavailable')
            return self.direct_transport(settings,args,**kwargs)
        self.assertEqual(self.run_query(broken)['status'],'failed')
        checkpoint=self.root/'state/product-version.json'
        binding=product._binding(self.settings,self.context,product.validate_config(self.settings))
        record=product._load(checkpoint,binding)
        record['phase']='timeout'
        record['query']['sent_at']=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
        product._save(checkpoint,record)
        self.calls=[]
        result=self.run_query(self.direct_transport)
        self.assertEqual(result['status'],'ok')
        self.assertFalse(any('+messages-send' in args for args,_ in self.calls))

    def test_discovery_older_than_24h_is_unavailable_and_never_resends(self):
        def broken(settings,args,**kwargs):
            if '+messages-mget' in args:raise RuntimeError('read unavailable')
            return self.direct_transport(settings,args,**kwargs)
        self.run_query(broken)
        checkpoint=self.root/'state/product-version.json'
        binding=product._binding(self.settings,self.context,product.validate_config(self.settings))
        record=product._load(checkpoint,binding)
        record['query']['sent_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
        product._save(checkpoint,record)
        self.calls=[]
        result=self.run_query(self.direct_transport)
        self.assertEqual(result['status'],'missing');self.assertIn('24 小时',result['reason'])
        self.assertFalse(any('+messages-send' in args for args,_ in self.calls))

    def test_cli_bot_identity_is_rejected_even_with_valid_message_content(self):
        def transport(settings,args,**kwargs):
            payload=self.transport(settings,args,**kwargs)
            if '+messages-mget' in args:payload['identity']='bot'
            return payload
        self.assertEqual(self.run_query(transport)['status'],'failed')

    def test_timeout_does_not_use_unrelated_latest_card(self):
        def transport(settings,args,**kwargs):
            if '+threads-messages-list' in args:return self.envelope([],has_more=False)
            return self.transport(settings,args,**kwargs)
        with patch.object(product.time,'monotonic',side_effect=[0,0,0,2,2]):
            result=self.run_query(transport)
        self.assertEqual(result['status'],'timeout');self.assertIsNone(result['code'])
        self.assertFalse(any('+chat-messages-list' in args for args,_ in self.calls))

    def test_result_validation_rejects_mutated_code_binding_and_history_claim(self):
        result=self.run_query()
        mutations=[lambda r:r.update(code='ABCD'),lambda r:r.update(historical_version_verified=True),
                   lambda r:r['binding'].update(query_chat_id='oc_other'),
                   lambda r:r.update(response_message_id='om_other'),lambda r:r.update(status='missing')]
        for mutate in mutations:
            changed=copy.deepcopy(result);mutate(changed)
            with self.assertRaises(InputError):product.validate_result(self.settings,self.context,changed)

    def test_same_task_concurrent_call_cannot_send_twice(self):
        directory=self.root/'concurrent';directory.mkdir()
        errors=[]
        with product._job_lock(directory):
            def call():
                try:product.get_product_version(self.settings,self.context,directory)
                except InputError as exc:errors.append(str(exc))
            with patch.object(product.policy,'run_lark',side_effect=AssertionError('must not send')):
                thread=threading.Thread(target=call);thread.start();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(len(errors),1)

    def test_config_pure_validation_rejects_bad_ids_and_limits(self):
        bad=[('query_chat_id','chat:oc_bad'),('asgard_bot_id','cli_wrong'),('asgard_app_id','ou_wrong'),
             ('timeout_seconds',True),('timeout_seconds',float('nan')),('timeout_seconds',601),
             ('poll_interval_seconds',0),('poll_interval_seconds',31)]
        for key,value in bad:
            settings=copy.deepcopy(self.settings);settings['product_version'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(InputError):product.validate_config(settings)

    def test_authorization_identity_mismatch_is_hard_error(self):
        with patch.object(product.policy,'ensure_user_authorization',side_effect=InputError('授权用户已变化')):
            with self.assertRaisesRegex(InputError,'身份校验不一致'):
                self.run_query()
        self.assertEqual(self.calls,[])

    def test_shared_thread_explicit_other_query_reply_is_not_claimed(self):
        binding=product._binding(self.settings,self.context,product.validate_config(self.settings))
        record={'binding':binding,'query':{'message_id':'om_query'}}
        reply=self.reply();reply['reply_to']='om_otherquery'
        self.assertIsNone(product._extract_replies([self.root_message(),reply],record,'omt_query'))
        result=self.run_query()
        result['response']['reply_to']='om_otherquery'
        with self.assertRaises(InputError):product.validate_result(self.settings,self.context,result)

    def test_shared_thread_multiple_queries_require_exact_reply_to(self):
        binding=product._binding(self.settings,self.context,product.validate_config(self.settings))
        record={'binding':binding,'query':{'message_id':'om_query'}}
        for vin in (VIN,'L2234567890123456'):
            other=self.root_message(message_id='om_secondquery')
            other['content']='查询车辆版本 '+vin+' CCUFSD'
            messages=[self.root_message(),other,self.reply()]
            with self.subTest(vin=vin),self.assertRaises(ValueError):
                product._extract_replies(messages,record,'omt_query')
            messages[-1]['reply_to']='om_query'
            evidence=product._extract_replies(messages,record,'omt_query')
            self.assertEqual(evidence['code'],'9AEE')


if __name__=='__main__':unittest.main()
