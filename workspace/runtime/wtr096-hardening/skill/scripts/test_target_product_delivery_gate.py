"""Offline release gate: trusted product code precedes every cloud write."""
import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_sheet_delivery
import report_attachments
import walkthrough_worker as worker
from request_model import InputError
from test_native_main_report import fixture
from test_native_publish_pipeline import trusted_product
from test_task_environment_selection import task_fixture, pipeline_fixture


class ProductDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'job.json'
        self.settings={'runtime_state_dir':str(self.root),'report_lark_profile':'synthetic-bot',
                       'local_analysis':{'environment':'test_then_prod'}}
        self.record={'vin':'DEM0VEH1CLE000001','kind':'walkthrough_test_local',
            'source_chat_id':'oc_source','source_message_id':'om_source','sender_id':'ou_requester',
            'status':'failed','request_sha256':'synthetic-request'}
        self.result=fixture();self.result.update(status='partial',vin=self.record['vin'],
            request_sha256=self.record['request_sha256'],source_policy=worker.SOURCE_POLICY)
        self.result['product_version']=trusted_product(self.settings,self.record)
        self.requests={role:{'vin':self.record['vin'],**self.result[role]['time_range']}
                       for role in ('baseline','replacement')}
        self.record['request'].update({role+'_metadata':metadata for role,metadata in self.requests.items()})
        self.rules=self.root/'rules.json'
        self.rules.write_text(json.dumps({'rules':'synthetic delivery gate'}),encoding='utf-8')
        self.details=self.result.pop('native_details')
        self.url='https://example.feishu.cn/docx/SyntheticMain'
        stack=ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch('socket.socket',side_effect=AssertionError('no network')))
        stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('no subprocess')))

    def unavailable(self,status):
        value=copy.deepcopy(self.result['product_version']);value['status']=status
        for key in ('code','label','raw_related_product','artifact_info','response_message_id'):
            value[key]=None
        value['response']=None
        return value

    def no_cloud(self):
        stack=ExitStack()
        for name in ('ensure_report_bot_authorization','run_lark','restrict_collaborator_management',
                     'grant_source_chat_view','transfer_report_owner','configure_report_secure_label'):
            stack.enter_context(patch.object(worker.policy,name,side_effect=AssertionError('cloud operation before code gate')))
        stack.enter_context(patch.object(native_sheet_delivery,'ensure_native_sheets',side_effect=AssertionError('detail creation before gate')))
        stack.enter_context(patch.object(worker,'create_report',side_effect=AssertionError('main creation before gate')))
        stack.enter_context(patch.object(worker.product,'get_product_version',side_effect=AssertionError('must not requery')))
        stack.enter_context(patch.object(worker,'analyze_task',side_effect=AssertionError('must not recalculate')))
        return stack

    def test_missing_failed_timeout_unknown_never_touch_cloud_or_checkpoint(self):
        for status in ('missing','failed','timeout','unknown',None):
            with self.subTest(status=status):
                result=copy.deepcopy(self.result)
                result['product_version']=self.unavailable(status) if status else None
                before=(copy.deepcopy(self.record),copy.deepcopy(result))
                with self.no_cloud(),self.assertRaisesRegex(InputError,'可信四位制品码'):
                    worker.publish(self.settings,self.path,self.record,result)
                self.assertEqual((self.record,result),before)
                self.assertFalse(self.path.exists())

    def test_code_or_binding_or_reply_tampering_stops_before_cloud(self):
        for variant in ('placeholder','wrong-code','wrong-group','wrong-user','wrong-app','wrong-reply'):
            with self.subTest(variant=variant):
                result=copy.deepcopy(self.result);version=result['product_version']
                if variant=='placeholder':version['code']=version['label']='XXXX'
                elif variant=='wrong-code':version['code']=version['label']='AB13'
                elif variant=='wrong-group':version['binding']['query_chat_id']='oc_other'
                elif variant=='wrong-user':version['binding']['operator_open_id']='ou_other'
                elif variant=='wrong-app':version['response']['asgard_app_id']='cli_other'
                else:version['response']['reply_to']='om_other'
                with self.no_cloud(),self.assertRaises(InputError):
                    worker.publish(self.settings,self.path,self.record,result)
                self.assertFalse(self.path.exists())

    def test_trusted_code_publishes_three_assets_without_query_or_recalculation(self):
        expected=worker.build_report_document({**self.result,'native_details':self.details})
        def call(settings,args,**kwargs):
            if args[:2]==['docs','+fetch']:
                return {'ok':True,'identity':'bot','data':{'document':{
                    'content':expected['xml'],'document_id':'SyntheticMain','revision_id':1}}}
            return {'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}
        with ExitStack() as stack:
            for name in ('ensure_report_bot_authorization','restrict_collaborator_management',
                         'transfer_report_owner','configure_report_secure_label'):
                stack.enter_context(patch.object(worker.policy,name))
            stack.enter_context(patch.object(worker.policy,'grant_source_chat_view',return_value={'perm':'view'}))
            sent=stack.enter_context(patch.object(worker.policy,'run_lark',side_effect=call))
            details=stack.enter_context(patch.object(native_sheet_delivery,'ensure_native_sheets',return_value=self.details))
            main=stack.enter_context(patch.object(worker,'create_report',return_value=self.url))
            stack.enter_context(patch.object(worker.product,'get_product_version',side_effect=AssertionError('no requery')))
            stack.enter_context(patch.object(worker,'analyze_task',side_effect=AssertionError('no recalculate')))
            worker.publish(self.settings,self.path,self.record,self.result)
        details.assert_called_once();main.assert_called_once()
        self.assertEqual(self.record['status'],'completed')
        self.assertEqual(self.record['report_content_validation']['data_cells_verified'],48)
        self.assertEqual(sum(c.args[1][:2]==['im','+messages-reply'] for c in sent.call_args_list),1)

    def cached(self,result):
        if result.get('delivery_format') == worker.DELIVERY_FORMAT:
            result['pipeline']=pipeline_fixture(task_fixture(requests=self.requests))
            rules_sha256=hashlib.sha256(self.rules.read_bytes()).hexdigest()
            for window in result['pipeline']['windows'].values():
                window['rules_sha256']=rules_sha256
        target=self.path.with_suffix('.result.json')
        target.write_text(json.dumps(result),encoding='utf-8')
        self.record['result_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        self.path.write_text(json.dumps(self.record),encoding='utf-8')
        return target,target.read_bytes()

    def resume_boundaries(self):
        stack=ExitStack()
        stack.enter_context(patch.object(worker,'load_settings',return_value=self.settings))
        stack.enter_context(patch.object(worker.policy,'activate_lark_environment'))
        stack.enter_context(patch.object(worker.policy,'ensure_user_authorization'))
        return stack

    def test_cached_timeout_resume_preserves_result_and_native_checkpoint(self):
        self.result['product_version']=self.unavailable('timeout')
        checkpoint={'version':'synthetic','roles':{'baseline':{'copy_pending':True}}}
        self.record['native_sheets']=copy.deepcopy(checkpoint)
        target,original=self.cached(self.result)
        with self.resume_boundaries(),self.no_cloud(),self.assertRaisesRegex(InputError,'可信四位制品码'):
            worker.run_worker(self.path,'unused')
        saved=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['native_sheets'],checkpoint)
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(target.read_bytes(),original)
        self.assertFalse(self.path.with_suffix('.worker.lock').exists())

    def test_legacy_final_resume_retains_old_delivery_without_product_query(self):
        legacy={key:value for key,value in self.result.items() if key!='product_version'}
        legacy.update(delivery_format='docx_with_full_xlsx_attachments',full_workbooks={})
        self.record.update(report_url=self.url,report_permissions={
            'collaborator_management':True,'source_chat_view':True,'owner_transfer':True})
        target,original=self.cached(legacy)
        with self.resume_boundaries(),ExitStack() as stack:
            stack.enter_context(patch.object(worker.policy,'ensure_report_bot_authorization'))
            stack.enter_context(patch.object(worker.policy,'configure_report_secure_label'))
            stack.enter_context(patch.object(worker.policy,'run_lark',return_value={
                'ok':True,'identity':'bot','data':{'message_id':'om_delivered'}}))
            stack.enter_context(patch.object(worker,'create_report',side_effect=AssertionError('must reuse legacy report')))
            stack.enter_context(patch.object(native_sheet_delivery,'ensure_native_sheets',side_effect=AssertionError('no legacy migration')))
            stack.enter_context(patch.object(worker.product,'get_product_version',side_effect=AssertionError('no legacy requery')))
            stack.enter_context(patch.object(worker,'analyze_task',side_effect=AssertionError('no legacy recalculation')))
            attachments=stack.enter_context(patch.object(report_attachments,'ensure_attachments'))
            self.assertEqual(worker.run_worker(self.path,'unused'),0)
        attachments.assert_called_once()
        self.assertEqual(target.read_bytes(),original)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['status'],'completed')


if __name__=='__main__':unittest.main()
