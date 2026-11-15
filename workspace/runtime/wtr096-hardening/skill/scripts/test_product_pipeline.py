"""Synthetic concurrent local+version integration with real workbook export."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from native_sheet_data import load_native_sheet_data
from feishu_report import build_report_notification
import cloud_pipeline
import environment_selection as selection
import native_log_source as native
import walkthrough_worker as worker
import window_pipeline
from request_model import InputError
from test_full_integration import complete_native_fixture, complete_pcm_fixture, synthetic_rules, META, OTHER, SCENARIOS
from test_task_environment_selection import (task_fixture, pipeline_fixture,
    per_window_task_fixture, per_window_pipeline_fixture)


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


class ProductPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rules = self.root/'rules.json'
        native.write_json(self.rules,synthetic_rules())
        self.request = {'vin':META['vin'],'baseline_metadata':META,'replacement_metadata':OTHER}
        self.record = {'job_key':'synthetic-version-job','kind':'walkthrough_test_local','status':'queued',
                       'request':self.request,'vin':META['vin'],'request_sha256':'synthetic-request-binding',
                       'report_operator_open_id':'ou_operator','source_chat_id':'oc_source',
                       'source_message_id':'om_source','source_thread_id':'om_source','sender_id':'ou_requester'}
        self.settings = {
            'workspace_root':str(self.root),'runtime_state_dir':str(self.root),
            'lark_home':str(self.root/'auth'),'lark_profile':'synthetic-user',
            'expected_lark_app_id':'cli_project','expected_dispatch_open_id':'ou_operator',
            'expected_operator_open_id':'ou_operator',
            'product_version':{'query_chat_id':'oc_query','asgard_bot_id':'ou_asgard',
                               'asgard_app_id':'cli_asgard','timeout_seconds':120,'poll_interval_seconds':2},
            'cloud_settings_path':'unused','local_analysis':{'environment':'test','backend':'native_full'},
        }

    def version(self,status='ok'):
        query = {'command':'查询车辆版本 '+META['vin']+' CCUFSD','message_id':'om_query',
                 'thread_id':'omt_query','sent_at':'2026-10-24T01:00:00+00:00'}
        binding = {'vin':META['vin'],'task_key':self.record['job_key'],'operator_open_id':'ou_operator',
                   'query_chat_id':'oc_query','asgard_bot_id':'ou_asgard','asgard_app_id':'cli_asgard',
                   'lark_profile':'synthetic-user','lark_home':os.path.normcase(str((self.root/'auth').resolve())),
                   'expected_lark_app_id':'cli_project'}
        value = {'status':status,'reason':'synthetic confirmed response' if status=='ok' else '版本查询超时',
                 'code':None,'label':None,'binding':binding,'vin':META['vin'],
                 'raw_related_product':None,'artifact_info':None,'query':query,'response':None,
                 'query_message_id':'om_query','response_message_id':None,'queried_at':query['sent_at'],
                 'response_at':None,'semantics':'query_time_platform_value','historical_version_verified':False,
                 'source_reported_at':None,'source_versions':{},'source_environment':None,
                 'quality':{'status':'reference_only','issues':[{'kind':'historical_version_unverified',
                     'message':'查询时平台值，未验证属于历史日志时段。'}]}}
        if status=='ok':
            value.update(code='AB12',label='AB12',raw_related_product='AB12, 9.2.0-1234',
                         artifact_info='AB12, 9.2.0-1234',response_message_id='om_response',
                         response_at='2026-10-24T01:00:01+00:00',response={
                             'message_id':'om_response','create_time':'2026-10-24T01:00:01+00:00',
                             'asgard_bot_id':'ou_asgard','asgard_app_id':'cli_asgard',
                             'thread_id':'omt_query','reply_to':'om_query',
                             'correlation':{'kind':'thread','query_message_id':'om_query',
                                            'thread_id':'omt_query','query_message_ids':['om_query']}})
        worker.product.validate_result(self.settings,worker.product_context(self.settings,self.record),value)
        return value

    def pipeline(self,directory,*,missing=False):
        windows = {}
        task = per_window_task_fixture(policy=self.settings['local_analysis']['environment'],
                                       requests={'baseline':META,'replacement':OTHER})
        for role,metadata in [('baseline',META),('replacement',OTHER)]:
            root = Path(directory)/role
            n = 10 if role=='baseline' else 9
            metrics = {s:{'label':s,'p99_ms':float(n),'frame_rate_hz':float(n+1),
                          'status':'ok','frame_rate_status':'ok'} for s,_ in SCENARIOS}
            if missing:
                metrics['parking'].update(p99_ms=None,frame_rate_hz=None)
            ad = complete_native_fixture(root,metadata,self.rules,metrics=metrics)
            pcm = complete_pcm_fixture(metadata,self.settings,root,None)
            if role=='replacement':
                for row in pcm['metrics'].values():
                    for metric in row.values():
                        metric['mean'] = 5.0
                        metric['p99'] = 19.0
            windows[role] = {
                'version':window_pipeline.RESULT_VERSION,'request':metadata,'status':'ok',
                'rules_sha256':native.digest(self.rules),'environment':'testtwo','ad':ad,'pcm':pcm,
                'sources':window_pipeline.source_plan(metadata,'test'),
                'environment_selection':selection.window_decision(task,role),
            }
        value = per_window_pipeline_fixture(task)
        value.update(version=cloud_pipeline.VERSION,windows=windows,source_manifest_sha256='a'*64)
        return value

    def local(self,directory,*,missing=False):
        pipeline = self.pipeline(directory,missing=missing)
        with patch.object(worker,'_prepare_local_analysis',return_value=(self.rules,pipeline)), \
             patch.object(worker.product,'get_product_version',side_effect=AssertionError('local-only must not query')):
            return worker.analyze_local_task(self.settings,self.record,directory)

    def test_local_and_version_branches_overlap_and_label_reaches_both_sheet_payloads(self):
        directory = self.root/'parallel'
        pipeline = self.pipeline(directory)
        expected = self.version()
        local_started = threading.Event()
        version_started = threading.Event()
        join = threading.Barrier(2,timeout=3)
        identities = {}
        def local(settings,record,root,*,on_stage=None):
            identities['local'] = threading.get_ident()
            local_started.set()
            self.assertTrue(version_started.wait(3),'version branch did not overlap local')
            join.wait()
            return self.rules,pipeline
        def version(settings,context,root,*,on_stage=None):
            identities['version'] = threading.get_ident()
            version_started.set()
            self.assertTrue(local_started.wait(3),'local branch did not overlap version')
            self.assertEqual(root,directory/'product-version')
            self.assertEqual(context,{'vin':META['vin'],'task_key':self.record['job_key'],'operator_open_id':'ou_operator'})
            self.assertNotIn('source_chat_id',context)
            self.assertNotEqual(settings['product_version']['query_chat_id'],self.record['source_chat_id'])
            join.wait()
            return expected
        with patch.object(worker,'_prepare_local_analysis',side_effect=local), \
             patch.object(worker.product,'get_product_version',side_effect=version), \
             patch.object(worker.policy,'run_lark',side_effect=AssertionError('network/message boundary mocked')):
            result = worker.analyze_task(self.settings,self.record,directory)
        self.assertNotEqual(identities['local'],identities['version'])
        self.assertIsNone(result['vehicle_label'])
        self.assertEqual(result['product_version']['code'],'AB12')
        self.assertIn('AB12',result['title'])
        self.assertEqual(result['product_version'],expected)
        self.assertEqual(set(result['native_sheet_data']),{'baseline','replacement'})
        document = worker.build_report_document({**result,'native_details':{
            'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
            'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}})
        self.assertIn('AB12',document['xml'])
        # The target body remains table-only; the same source reply carries
        # both native links and the explicit query-time product semantics.
        notice=build_report_notification({**result,'native_details':{
            'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
            'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}},
            vin=META['vin'],url='https://example.feishu.cn/docx/SyntheticMain')
        self.assertIn('未验证为两个历史时段的安装版本',str(notice))
        self.assertIn('AB12',str(notice))
        self.assertEqual(document['xml'].count('<p><b>数据来源'),0)
        for role,attachment in result['native_sheet_data'].items():
            self.assertEqual(attachment['role'],role)
            payload = load_native_sheet_data(attachment['path'],attachment['sha256'])
            context = payload['delivery_context']
            self.assertEqual(context['source_hashes']['product_version'],hashlib.sha256(canonical(expected).encode()).hexdigest())
            self.assertEqual(context['product_version']['code'],'AB12')
            self.assertFalse(context['product_version']['historical_version_verified'])
            self.assertEqual(context['product_version']['semantics'],'query_time_platform_value')
            self.assertEqual(payload['metadata']['role'],role)
        self.assertFalse(list(directory.rglob('*.xlsx')))

    def test_version_timeout_is_partial_and_all_six_metric_results_remain_exact(self):
        local = self.local(self.root/'local',missing=True)
        directory = self.root/'timeout'
        pipeline = self.pipeline(directory,missing=True)
        with patch.object(worker,'_prepare_local_analysis',return_value=(self.rules,pipeline)), \
             patch.object(worker.product,'get_product_version',return_value=self.version('timeout')):
            result = worker.analyze_task(self.settings,self.record,directory)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(canonical(result['comparisons']),canonical(local['comparisons']))
        self.assertEqual(result['vehicle_label'],local['vehicle_label'])
        self.assertIsNone(result['product_version']['code'])
        self.assertTrue(any(row.get('kind')=='product_version_unavailable' for row in result['issues']))
        for scenario in result['comparisons'].values():
            self.assertEqual(len(scenario['metrics']),6)
        self.assertEqual(len(result['native_sheet_data']),2)
        document=worker.build_report_document({**result,'native_details':{
            'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
            'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}})
        notice=build_report_notification({**result,'native_details':{
            'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
            'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}},
            vin=META['vin'],url='https://example.feishu.cn/docx/SyntheticMain')
        self.assertIn('未获取',str(notice))
        self.assertNotIn('相关制品信息',document['xml'])

    def test_invalid_version_binding_stops_finalization(self):
        invalid = self.version()
        invalid['binding']['query_chat_id']='oc_unrelated'
        with patch.object(worker,'_prepare_local_analysis',return_value=(self.rules,{})), \
             patch.object(worker.product,'get_product_version',return_value=invalid), \
             patch.object(worker,'_finalize_local_analysis',side_effect=AssertionError('must not export')):
            with self.assertRaises(InputError):
                worker.analyze_task(self.settings,self.record,self.root/'invalid')

    def cached(self,version):
        path = self.root/'job.json'
        value = {'status':'partial','vin':META['vin'],'request_sha256':self.record['request_sha256'],
                 'source_policy':native.SOURCE_POLICY,'product_version':version,
                 'delivery_format':worker.DELIVERY_FORMAT}
        value['pipeline'] = pipeline_fixture(task_fixture(policy='test',
            requests={'baseline':META,'replacement':OTHER}))
        for window in value['pipeline']['windows'].values():
            window['rules_sha256'] = native.digest(self.rules)
        result_path = path.with_suffix('.result.json')
        native.write_json(result_path,value)
        record = {**self.record,'status':'failed','result_sha256':native.digest(result_path),
                  'report_url':'https://example.com/report'}
        native.write_json(path,record)
        return path,value

    def test_cached_delivery_validates_version_and_does_not_query_or_reanalyze(self):
        path,value = self.cached(self.version())
        with patch.object(worker,'load_settings',return_value=self.settings), \
             patch.object(worker.policy,'activate_lark_environment'), \
             patch.object(worker.policy,'ensure_user_authorization'), \
             patch.object(worker,'analyze_task',side_effect=AssertionError('no reanalysis')), \
             patch.object(worker.product,'get_product_version',side_effect=AssertionError('no requery')), \
             patch.object(worker,'publish') as publish:
            self.assertEqual(worker.run_worker(path,'unused'),0)
        self.assertEqual(publish.call_args.args[-1],value)

    def test_cached_wrong_query_group_or_history_claim_never_publishes(self):
        for variant in ('missing','wrong-group','wrong-user','historical-claim'):
            with self.subTest(variant=variant):
                version = self.version()
                if variant=='missing':version=None
                elif variant=='wrong-group':version['binding']['query_chat_id']='oc_other'
                elif variant=='wrong-user':version['binding']['operator_open_id']='ou_other'
                else:version['historical_version_verified']=True
                path,_ = self.cached(version)
                with patch.object(worker,'load_settings',return_value=self.settings), \
                     patch.object(worker.policy,'activate_lark_environment'), \
                     patch.object(worker.policy,'ensure_user_authorization'), \
                     patch.object(worker,'analyze_task',side_effect=AssertionError('no reanalysis')), \
                     patch.object(worker.product,'get_product_version',side_effect=AssertionError('no requery')), \
                     patch.object(worker,'publish',side_effect=AssertionError('invalid result must not publish')):
                    with self.assertRaises(InputError):
                        worker.run_worker(path,'unused')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'],'failed')
                self.assertFalse(path.with_suffix('.worker.lock').exists())


if __name__=='__main__':
    unittest.main()
