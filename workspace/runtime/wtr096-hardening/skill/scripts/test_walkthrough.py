"""Synthetic behavioral tests; no network, real VINs, or Feishu messages."""
import argparse
import ast
from datetime import datetime, timezone, timedelta
import json
import io
import zipfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import native_log_source as native
import walkthrough_worker as worker
from walkthrough_route import route, canonical_time
from request_model import InputError
from performance_comparison import compare_performance, METRIC_SPECS
from feishu_report import build_report_document
from m100_attachment_worker import collect_coverage
import preflight
import environment_selection
import window_pipeline
import cloud_pipeline
from test_full_integration import complete_native_fixture, synthetic_rules

VIN = 'DEM0VEH1CLE000001'
TEXT = f'走查测试 {VIN} base 2026-10-19 15:10:00 - 2026-10-19 15:44:00 PCM更新 2026-10-20 15:10:00 - 2026-10-20 15:44:00'
META = {'vin':VIN,'start':'2026-10-19T15:10:00','end':'2026-10-19T15:44:00'}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_trigger_preserves_legacy(self):
        self.assertEqual(route(TEXT.replace('走查测试','时延分析'))['status'],'no_match')
        decision = route(TEXT)
        self.assertEqual(decision['status'],'ready',decision)
        self.assertEqual(decision['normalized']['baseline_metadata'],META)

    def test_short_same_day_ranges_and_explicit_roles(self):
        decision = route(TEXT.replace('15:10:00 - 2026-10-19 15:44:00','15:10 到 15:44'))
        self.assertEqual(decision['status'],'ready',decision)
        reversed_roles = route(TEXT.replace('base','PCM更新',1).rsplit('PCM更新',1)[0]+'base 2026-10-20 15:10:00 - 2026-10-20 15:44:00')
        self.assertEqual(reversed_roles['status'],'ready',reversed_roles)
        self.assertIn('2026-10-20',reversed_roles['normalized']['baseline_metadata']['start'])

    def test_no_results_url_or_single_window(self):
        self.assertEqual(route('走查测试 https://example.com/result')['status'],'needs_input')
        self.assertEqual(route(f'走查测试 {VIN} 2026-10-19 15:10 到 15:44')['status'],'needs_input')

    def test_timezone_normalization(self):
        self.assertEqual(canonical_time('2026-10-19T07:10:00+00:00'),META['start'])

    def test_unlabeled_roles_follow_input_order_even_when_later_window_is_first(self):
        text=f'走查测试 {VIN}, 2026-10-21 15:48, 2026-10-21 16:22\n{VIN}, 2026-10-21 14:50, 2026-10-21 15:24'
        request=route(text)['normalized']
        self.assertEqual(request['baseline_metadata']['start'],'2026-10-21T15:48:00')
        self.assertEqual(request['replacement_metadata']['start'],'2026-10-21T14:50:00')
        self.assertEqual(request['role_assignment_source'],'input_order_default')
        self.assertEqual(request['input_roles'],['baseline','replacement'])
        explicit=route(text.replace(VIN,'PCM更新 '+VIN,1))['normalized']
        self.assertEqual(explicit['replacement_metadata']['start'],'2026-10-21T15:48:00')
        self.assertEqual(explicit['baseline_metadata']['start'],'2026-10-21T14:50:00')
        self.assertEqual(explicit['role_assignment_source'],'explicit')

    def test_raw_query_exact_classes_env_and_complete_pagination(self):
        item = {'vin':VIN,'fileName':'a.zip','fileKey':'a','collectTime':'x'}
        responses = [{'data':{'total':2,'list':[item]}},{'data':{'total':2,'list':[{**item,'fileKey':'b'}]}}]
        with patch.object(native.cloud,'request_json',side_effect=responses) as query:
            self.assertEqual(len(native.query_class(META,'log_fsdA','private-user')),2)
            params = query.call_args_list[0].args[0]
            self.assertEqual(params['collectTimeMin'],'2026-10-19 15:10:00')
            self.assertEqual(params['logClass'],'log_fsdA')
            self.assertEqual(query.call_args.kwargs['environment'],'test')
        with self.assertRaises(InputError):
            native.query_class(META,'log_fsdA_service','private-user')

    def test_wrong_vin_duplicate_or_incomplete_listing_rejected(self):
        for response in ({'total':1,'list':[{'vin':'OTHER'}]},
                         {'total':2,'list':[]},
                         {'total':2,'list':[{'vin':VIN},{'vin':VIN}]}):
            with patch.object(native.cloud,'request_json',return_value={'data':response}):
                with self.assertRaises(InputError):
                    native.query_class(META,'log_fsdB','private-user')

    def test_archive_discovery_crosses_midnight_without_changing_analysis_window(self):
        metadata={**META,'end':'2026-10-19T23:44:00'}
        expanded=native.archive_query_metadata(metadata)
        self.assertEqual(expanded['end'],'2026-10-20T00:44:00')
        self.assertEqual(metadata['end'],'2026-10-19T23:44:00')
        self.assertEqual(expanded['start'],metadata['start'])

    def test_download_includes_archive_closing_after_requested_end(self):
        content=io.BytesIO()
        with zipfile.ZipFile(content,'w') as archive:
            archive.writestr('parking.log','10-19 15:43:40 synthetic in-window row\n')
        raw=content.getvalue()
        item={'fileName':'vla_parking_container_2026_10_19_15_44_24.zip','vin':VIN,
              'downloadURL':'https://download.invalid/boundary.zip','fileSize':len(raw)}
        def query(metadata,name,username,*,environment='test'):
            self.assertEqual(environment,'test')
            self.assertEqual(metadata['end'],'2026-10-19T16:44:00')
            return [item] if name=='log_fsdA' else []
        def download(url,path,**kwargs):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw);return len(raw)
        with patch.object(native.cloud,'load_settings',return_value={'username':'synthetic'}),patch.object(native,'query_class',side_effect=query),patch.object(native.cloud,'download_to_file',side_effect=download):
            result=native.fetch_window(META,{'cloud_settings_path':'own-config'},self.root/'input')
        self.assertEqual(result['request']['end'],'2026-10-19T15:44:00')
        self.assertEqual(result['counts']['log_fsdA'],1)
        self.assertEqual(result['archive_query']['end'],'2026-10-19T16:44:00')

    def test_native_cache_from_insufficient_discovery_policy_is_rejected(self):
        rules=self.root/'rules.json';rules.write_text('{"synthetic":{}}')
        native.write_json(self.root/'native-result.json',{'request':META,'rules_sha256':native.digest(rules)})
        cfg={'cloud_settings_path':'unused','local_analysis':{'backend':'native_full','execution':'wsl','environment':'test','distribution':'Ubuntu','python':'/venv/bin/python','analyzer_root':'/latest','rules_json':str(rules)}}
        with patch.object(native,'fetch_window',side_effect=AssertionError('must not reuse or overwrite old input')):
            with self.assertRaises(InputError):
                native.analyze_window(META,cfg,self.root,rules)

    def test_wsl_path(self):
        self.assertEqual(native.wsl_path('D:/new skill/report.json'),'/mnt/d/new skill/report.json')
        with self.assertRaises(InputError):
            native.wsl_path('relative/path')

    def test_remote_linux_filename_is_portable_on_windows(self):
        self.assertEqual(native.portable_filename({'fileName':'core.t:FsdFus:fusion.1.zip'},1),'core.t_FsdFus_fusion.1.zip')
        self.assertEqual(native.portable_filename({'fileName':'CON.zip'},1),'archive-CON.zip')
        self.assertEqual(native.portable_filename({'fileName':'vla_parking_container_2026_10_19_15_44_24.zip'},1),'vla_parking_container_2026_10_19_15_44_24.zip')

    def test_explicit_http_only_download_whitelist_is_supported(self):
        cfg={'origin':'https://query.invalid','providerEnvironment':'testtwo','displayName':'test',
             'trustedDownloadHosts':{'https':set(),'http':{'download.invalid'}}}
        with patch.dict(native.cloud.CLOUD_ENVIRONMENTS,{'test':cfg}):
            self.assertEqual(native.cloud.environment_config('test')['trustedDownloadHosts']['http'],{'download.invalid'})

    def test_coverage_discloses_short_tail_and_missing_parking_state(self):
        folder = self.root/'perf/logs_traned'; folder.mkdir(parents=True)
        stamp = datetime(2026,10,19,15,31,47,tzinfo=timezone(timedelta(hours=8))).timestamp()
        (folder/'test+header_delay.csv').write_text(f'a b c {stamp} 261,0\n',encoding='utf-8')
        coverage = collect_coverage(self.root,'20261019151000/20261019154400')
        row = coverage['targets']['header_delay']
        self.assertEqual(row['rows'],1)
        self.assertIn('15:31:47',row['last'])
        self.assertFalse(coverage['continuous_coverage_verified'])
        self.assertEqual(coverage['targets']['vla_parking_header_delay']['rows'],0)

    def test_cached_analysis_never_queries_or_reanalyzes(self):
        html=self.root/'native-report.html'; html.write_text('<html>test</html>')
        rules=self.root/'rules.json'; rules.write_text(json.dumps(synthetic_rules()))
        value=complete_native_fixture(self.root,META,rules)
        cfg={'cloud_settings_path':'unused','local_analysis':{'backend':'native_full','execution':'wsl','environment':'test','distribution':'Ubuntu','python':'/venv/bin/python','analyzer_root':'/latest','rules_json':str(rules)}}
        with patch.object(native,'fetch_window',side_effect=AssertionError('download')),patch.object(native.subprocess,'run',side_effect=AssertionError('analysis')):
            self.assertEqual(native.analyze_window(META,cfg,self.root,rules),value)
        value.pop('analysis_scope')
        native.write_json(self.root/'native-result.json',value)
        with self.assertRaises(InputError):native.cached_result(META,self.root,rules)

    def test_six_metric_directions_and_missing_not_zero(self):
        def ad(p99,fps):
            return {'metrics':{s:{'p99_ms':p99,'frame_rate_hz':fps,'status':'ok','frame_rate_status':'ok'} for s in ('slow_driver','fast_driver','CNOA','parking')}}
        def state(n):
            return {'metrics':{s:{key:{'mean':n,'p99':n,'count':10} for key in ('system_cpu_percent','pcm_p99_infer_delay_ms','pcm_frame_rate_hz','pcm_cpu_percent')} for s in ('slow_driver','fast_driver','CNOA','parking')}}
        result=compare_performance(ad(100,5),ad(90,6),baseline_metadata=META,
            replacement_metadata={**META,'start':'2026-10-20T15:10:00','end':'2026-10-20T15:44:00'},baseline_state=state(20),replacement_state=state(10))
        self.assertEqual(result['status'],'ok')
        rows=result['comparisons']['slow_driver']['metrics']
        self.assertEqual(len(rows),6)
        self.assertEqual(rows['p99_infer_delay_ms']['tone'],'improved')
        self.assertEqual(rows['frame_rate_hz']['tone'],'improved')
        self.assertEqual(rows['pcm_frame_rate_hz']['tone'],'regressed')
        result['issues']=[{'message':'泊车 state 1 缺失'}]
        xml=build_report_document(result)['xml']
        self.assertNotIn('泊车 state 1 缺失',xml)
        self.assertNotIn('不能证明整个请求窗口连续覆盖',xml)
        from xml.etree import ElementTree as ET
        doc = ET.fromstring('<doc>'+xml+'</doc>')
        self.assertEqual([node.tag for node in doc],['title','table'])
        self.assertEqual(sum(len(row)-1 for row in doc.findall('./table/tbody/tr')),48)

    def test_completed_resume_has_no_delivery_or_analysis(self):
        path=self.root/'walkthrough.json'; native.write_json(path,{'kind':'walkthrough_test_local','status':'completed'})
        with patch.object(worker,'load_settings',return_value={'runtime_state_dir':str(self.root)}),patch.object(worker,'publish',side_effect=AssertionError('publish')),patch.object(worker,'analyze_task',side_effect=AssertionError('analysis')):
            self.assertEqual(worker.run_worker(path,'unused'),0)

    def test_delivery_retry_uses_hashed_result(self):
        path=self.root/'walkthrough.json'; result_path=path.with_suffix('.result.json')
        value={'vin':VIN,'request_sha256':'fingerprint','status':'partial','source_policy':native.SOURCE_POLICY,
               'delivery_format':worker.DELIVERY_FORMAT}
        from test_task_environment_selection import task_fixture,pipeline_fixture
        request=route(TEXT)['normalized']
        task=task_fixture(requests={role:request[role+'_metadata'] for role in ('baseline','replacement')})
        value['pipeline']=pipeline_fixture(task)
        native.write_json(result_path,value)
        record={'kind':'walkthrough_test_local','status':'failed','report_operator_open_id':'ou_test','vin':VIN,'request_sha256':'fingerprint','result_sha256':native.digest(result_path),'report_url':'https://example.com/doc',
                'job_key':'synthetic-cached-job','request':request}
        native.write_json(path,record)
        with patch.object(worker,'load_settings',return_value={'runtime_state_dir':str(self.root),
                'local_analysis':{'environment':'test_then_prod'}}),patch.object(worker.policy,'activate_lark_environment'),patch.object(worker.policy,'ensure_user_authorization'),patch.object(worker.product,'validate_result') as validated,patch.object(worker,'publish') as publish,patch.object(worker,'analyze_task',side_effect=AssertionError('reanalysis')):
            self.assertEqual(worker.run_worker(path,'unused'),0)
            self.assertEqual(publish.call_args.args[-1],value)
            self.assertEqual(validated.call_args.args[1],{'vin':VIN,'task_key':'synthetic-cached-job','operator_open_id':'ou_test'})

    def test_uncertain_creation_does_not_duplicate(self):
        path=self.root/'walkthrough.json'
        with patch.object(worker.policy,'ensure_report_bot_authorization'),patch.object(worker,'create_report',side_effect=AssertionError('duplicate')):
            with self.assertRaises(InputError):
                worker.publish({},path,{'report_create_pending':True},{})

    def test_readonly_preflight_never_queries_cloud_or_sends_messages(self):
        from test_native_preflight import good_receipt
        cli=self.root/'lark-cli.exe'; cli.write_text('synthetic')
        settings={'lark_cli':str(cli),'cloud_settings_path':'unused',
                  'lark_home':str(self.root/'synthetic-home'), 'lark_profile':'synthetic-user',
                  'expected_lark_app_id':'cli_syntheticApp','expected_operator_open_id':'ou_syntheticUser',
                  'product_version':{'query_chat_id':'oc_syntheticQuery','asgard_bot_id':'ou_syntheticAsgard',
                                     'timeout_seconds':120,'poll_interval_seconds':2},'local_analysis':{
            'distribution':'Ubuntu','python':'/venv/python','analyzer_root':'/latest'}}
        identity={'appId':'cli_syntheticApp','identities':{'user':{'available':True,'openId':'ou_syntheticUser',
                  'tokenStatus':'valid','expiresAt':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}}
        with patch.object(preflight,'load_settings',return_value=settings),patch.object(preflight,'check_host_dependencies',return_value={'zstandard':'synthetic'}),patch.object(preflight.cloud,'environment_config'),patch.object(preflight.cloud,'load_settings'),patch.object(preflight.cloud,'request_json',side_effect=AssertionError('network')),patch.object(preflight.policy,'auth_status',return_value=identity) as status,patch.object(preflight.policy,'run_lark',side_effect=AssertionError('message')),patch.object(preflight.subprocess,'run',return_value=argparse.Namespace(returncode=0,stdout=json.dumps(good_receipt()))) as run:
            self.assertEqual(preflight.check('unused')['status'],'local_preflight_passed')
            status.assert_called_once_with(settings,verify=False)
            self.assertEqual(run.call_count,1)
            command=run.call_args.args[0]
            self.assertIn('--exec',command)
            self.assertTrue(any(str(arg).endswith('/native_preflight.py') for arg in command))
            self.assertNotIn('--execute',command)
            self.assertEqual(command[-1],'/latest')

    def test_pcm_failure_preserves_available_ad_without_environment_fallback(self):
        request=route(TEXT)['normalized']
        metrics={s:{'p99_ms':100,'frame_rate_hz':5,'status':'ok','frame_rate_status':'ok'} for s in ('slow_driver','fast_driver','CNOA','parking')}
        rules=self.root/'rules.json';rules.write_text(json.dumps(synthetic_rules()))
        from test_task_environment_selection import per_window_task_fixture,per_window_pipeline_fixture
        task=per_window_task_fixture(policy='test',requests={role:request[role+'_metadata'] for role in ('baseline','replacement')})
        pipeline=per_window_pipeline_fixture(task)
        pipeline['source_manifest_sha256']='synthetic'
        for role in ('baseline','replacement'):
            meta=request[role+'_metadata']
            pipeline['windows'][role].update(
                version=window_pipeline.RESULT_VERSION,request=meta,status='partial',
                rules_sha256=native.digest(rules),
                ad=complete_native_fixture(self.root/role,meta,rules,metrics=metrics),
                pcm={'status':'failed','metrics':{},'error':'同环境 PCM 解析失败。','source':{'environment':'testtwo'}},
                sources=window_pipeline.source_plan(meta,'test'))
        with patch.object(worker,'rules_snapshot',return_value=self.root/'rules.json'),patch.object(worker,'run_windows',return_value=pipeline) as run:
            result=worker.analyze_local_task({'cloud_settings_path':'own-private-config',
                'local_analysis':{'environment':'test'}},{'request':request,'request_sha256':'test'},self.root)
            self.assertEqual(result['status'],'partial')
            self.assertEqual(run.call_count,1)
            self.assertNotIn('private diagnostic',json.dumps(result))

    def test_modules_do_not_import_old_skill(self):
        for path in Path(__file__).parent.glob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):
                    self.assertNotIn(node.module,('ad_p99','asgard_worker','asgard_bridge','half_auto','lark_source'),path.name)


if __name__=='__main__':
    unittest.main()
