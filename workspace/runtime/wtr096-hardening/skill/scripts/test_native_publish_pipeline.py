"""Three-asset publication gates and delivery-only resume without live calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import walkthrough_worker as worker
from request_model import InputError
from test_native_main_report import fixture


def trusted_product(settings, record):
    """A fully task-bound synthetic reply; never looks up a live identity."""
    settings.update(lark_profile='synthetic-user',lark_home=str(Path(settings['runtime_state_dir'])/'auth'),
        expected_lark_app_id='cli_project',expected_dispatch_open_id='ou_operator',
        expected_operator_open_id='ou_operator',product_version={
            'query_chat_id':'oc_query','asgard_bot_id':'ou_asgard','asgard_app_id':'cli_asgard'})
    record.update(job_key='synthetic-publication',report_operator_open_id='ou_operator',
        request={'vin':record['vin']})
    binding=worker.product._binding(settings,worker.product_context(settings,record),worker.product.validate_config(settings))
    query={'command':'查询车辆版本 '+record['vin']+' CCUFSD','message_id':'om_query',
           'thread_id':'omt_query','sent_at':'2026-10-24T01:00:00+00:00'}
    return {'status':'ok','reason':'synthetic confirmed response','code':'AB12','label':'AB12',
        'binding':binding,'vin':record['vin'],'semantics':worker.product.SEMANTICS,
        'historical_version_verified':False,'query':query,'query_message_id':'om_query',
        'queried_at':query['sent_at'],'raw_related_product':'AB12, 9.2.0-1234',
        'artifact_info':'AB12, 9.2.0-1234','response_message_id':'om_response',
        'response_at':'2026-10-24T01:00:01+00:00','quality':{'status':'reference_only'},
        'response':{'message_id':'om_response','create_time':'2026-10-24T01:00:01+00:00',
            'asgard_bot_id':'ou_asgard','asgard_app_id':'cli_asgard','thread_id':'omt_query',
            'reply_to':'om_query','correlation':{'kind':'thread','query_message_id':'om_query',
                'thread_id':'omt_query','query_message_ids':['om_query']}}}


class NativePublishPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.path=self.root/'job.json'
        self.record={'vin':'DEM0VEH1CLE000001','source_chat_id':'oc_source','sender_id':'ou_requester',
                     'source_message_id':'om_source','source_thread_id':'om_source','status':'analyzing'}
        self.result=fixture()
        self.result['status']='partial'
        self.settings={'report_lark_profile':'demo-bot','runtime_state_dir':str(self.root)}
        self.result['product_version']=trusted_product(self.settings,self.record)
        self.details=self.result.pop('native_details')
        self.expected=worker.build_report_document({**self.result,'native_details':self.details})
        self.url='https://example.feishu.cn/docx/SyntheticMain'
        self.calls=[]

    def remote(self,settings,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['docs','+fetch']:
            return {'ok':True,'identity':'bot','data':{'document':{
                'document_id':'SyntheticMain','revision_id':1,'content':self.expected['xml']}}}
        return {'ok':True,'identity':'bot','data':{'message_id':'om_syntheticDelivered'}}

    def boundaries(self):
        import contextlib
        stack=contextlib.ExitStack()
        for name in ('ensure_report_bot_authorization','restrict_collaborator_management',
                     'grant_source_chat_view','transfer_report_owner','configure_report_secure_label'):
            if name == 'grant_source_chat_view':
                stack.enter_context(patch.object(worker.policy,name,return_value={
                    'chat_mode':'group','member_type':'openchat',
                    'member_id':self.record['source_chat_id'],'perm':'view'}))
            else:
                stack.enter_context(patch.object(worker.policy,name))
        stack.enter_context(patch.object(worker.policy,'run_lark',side_effect=self.remote))
        return stack

    def test_detail_failure_never_creates_or_sends_main(self):
        import native_sheet_delivery
        with self.boundaries(),patch.object(native_sheet_delivery,'ensure_native_sheets',side_effect=InputError('unverified')), \
                patch.object(worker,'create_report',side_effect=AssertionError('must not create')):
            with self.assertRaises(InputError):worker.publish(self.settings,self.path,self.record,self.result)
        self.assertFalse(self.calls)

    def test_known_main_is_reused_after_failed_readback_and_send_waits_for_verification(self):
        import native_sheet_delivery
        with self.boundaries(),patch.object(native_sheet_delivery,'ensure_native_sheets',return_value=self.details), \
                patch.object(worker,'create_report',return_value=self.url) as create:
            with patch.object(worker.policy,'run_lark',side_effect=InputError('temporary readback error')):
                with self.assertRaises(InputError):worker.publish(self.settings,self.path,self.record,self.result)
            self.assertEqual(create.call_count,1)
            self.assertEqual(self.record['report_url'],self.url)
            self.assertNotIn('report_create_pending',self.record)
            self.assertFalse(self.calls)
            worker.publish(self.settings,self.path,self.record,self.result)
            self.assertEqual(create.call_count,1)
        self.assertEqual(self.record['status'],'completed')
        self.assertEqual(self.record['report_content_validation']['data_cells_verified'],48)
        self.assertEqual(sum(args[:2]==['im','+messages-reply'] for args in self.calls),1)

    def test_changed_main_blocks_notification_and_preserves_existing_url(self):
        import native_sheet_delivery
        self.record['report_url']=self.url
        wrong=copy.deepcopy(self.expected)
        self.expected['xml']=self.expected['xml'].replace('>10</p>','>11</p>',1)
        with self.boundaries(),patch.object(native_sheet_delivery,'ensure_native_sheets',return_value=self.details), \
                patch.object(worker,'create_report',side_effect=AssertionError('must not recreate')):
            with self.assertRaises(InputError):worker.publish(self.settings,self.path,self.record,self.result)
        self.assertEqual(self.record['report_url'],self.url)
        self.assertFalse(any(args[:2]==['im','+messages-reply'] for args in self.calls))

    def test_legacy_final_snapshot_uses_original_attachment_path_only(self):
        import native_sheet_delivery
        import report_attachments
        self.record['report_url']=self.url
        old={'delivery_format':'docx_with_full_xlsx_attachments','full_workbooks':{},
             'status':'partial'}
        with self.boundaries(),patch.object(native_sheet_delivery,'ensure_native_sheets',side_effect=AssertionError('legacy migrated')), \
                patch.object(report_attachments,'ensure_attachments') as attachments:
            worker.publish(self.settings,self.path,self.record,old)
        self.assertEqual(attachments.call_count,1)
        self.assertFalse(any(args[:2]==['docs','+fetch'] for args in self.calls))

    def test_old_incomplete_job_cannot_trigger_new_cloud_or_version_work(self):
        record={**self.record,'kind':'walkthrough_test_local','report_operator_open_id':'ou_operator'}
        self.path.write_text(json.dumps(record),encoding='utf-8')
        settings={**self.settings,'runtime_state_dir':str(self.root)}
        with patch.object(worker,'load_settings',return_value=settings), \
                patch.object(worker.policy,'activate_lark_environment'), \
                patch.object(worker.policy,'ensure_user_authorization'), \
                patch.object(worker,'check_native_environment',side_effect=AssertionError('reanalyze old job')), \
                patch.object(worker,'analyze_task',side_effect=AssertionError('cloud or version query')):
            with self.assertRaisesRegex(InputError,'旧任务没有本版最终快照'):
                worker.run_worker(self.path,'unused')


if __name__=='__main__':unittest.main()
