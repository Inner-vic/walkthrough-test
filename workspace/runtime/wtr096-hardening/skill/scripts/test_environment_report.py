import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import walkthrough_worker as worker
from feishu_report import build_report_document as render_report
from feishu_report import _environment_evidence
from request_model import InputError
import environment_selection
import window_pipeline
import cloud_pipeline
from test_full_integration import complete_native_fixture, synthetic_rules


META = {'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:48:00','end':'2026-10-21T16:22:00'}
OTHER_META = {**META,'start':'2026-10-21T14:50:00','end':'2026-10-21T15:24:00'}


def build_report_document(result):
    return render_report({**result,'native_details':{
        'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
        'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}})


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = {'vin':META['vin'],'baseline_metadata':dict(META),
                        'replacement_metadata':dict(OTHER_META)}
        self.settings = {'local_analysis':{'environment':'test_then_prod'}}
        self.rules=self.root/'rules.json'
        self.rules.write_text(json.dumps(synthetic_rules()),encoding='utf-8')
        metrics = {scenario:{'p99_ms':100,'frame_rate_hz':5,'status':'ok','frame_rate_status':'ok'}
                   for scenario in ('slow_driver','fast_driver','CNOA','parking')}
        windows = {}
        for role,metadata in (('baseline',META),('replacement',OTHER_META)):
            provider = 'prod'
            windows[role] = {'version':window_pipeline.RESULT_VERSION,'request':dict(metadata),'status':'partial',
                'rules_sha256':window_pipeline.ad.digest(self.rules),'environment':provider,
                'ad':complete_native_fixture(self.root/role,metadata,self.rules,environment=provider,metrics=copy.deepcopy(metrics)),
                'pcm':{'status':'failed','metrics':{},'source':{'environment':provider}},'sources':window_pipeline.source_plan(metadata,'prod')}
        self.pipeline = {'version':environment_selection.TASK_PIPELINE_VERSION,'windows':windows,'source_manifest_sha256':'synthetic'}
        self.bind_task()

    def bind_task(self, environment='prod', chosen_policy='test_then_prod'):
        requests=environment_selection.task_requests({role:self.request[role+'_metadata'] for role in ('baseline','replacement')})
        counts={name:int(name in ('log_fsdA','log_fsdB')) for name in environment_selection.CLASSES}
        attempts=[]
        if environment=='prod' and chosen_policy=='test_then_prod':
            attempts.append({'environment':'testtwo','windows':{
                'baseline':{'counts':dict(counts),'pcm_status':'ok'},
                'replacement':{'counts':dict.fromkeys(environment_selection.CLASSES,0),'pcm_status':'ok'}}})
        provider=environment_selection.provider(environment)
        attempts.append({'environment':provider,'windows':{
            role:{'counts':dict(counts),'pcm_status':'ok'} for role in ('baseline','replacement')}})
        task={'version':environment_selection.TASK_VERSION,'status':'selected','request':requests,
              'policy':chosen_policy,'environment':environment,'provider_environment':provider,'attempts':attempts}
        self.pipeline.update(environment=provider,environment_policy=chosen_policy,
            task_environment_selection=task,task_selection_sha256=environment_selection.task_selection_sha256(task))
        self.settings['local_analysis']['environment']=chosen_policy
        for role,window in self.pipeline['windows'].items():
            window['environment']=provider
            window['sources']=window_pipeline.source_plan(window['request'],environment)
            window['ad']['environment']=provider
            window['ad']['source']['environment']=provider
            window['pcm']['source']['environment']=provider
            window['environment_selection']=environment_selection.window_decision(task,role)

    def result(self):
        with patch.object(worker,'rules_snapshot',return_value=self.root/'rules.json'), \
                patch.object(worker,'run_windows',return_value=self.pipeline):
            return worker.analyze_local_task(self.settings,
                {'request':self.request,'request_sha256':'synthetic'},self.root)

    def test_worker_preserves_one_environment_and_task_evidence(self):
        result=self.result()
        self.assertEqual(result['pipeline']['environment'],'prod')
        self.assertEqual(result['pipeline']['environment_policy'],'test_then_prod')
        self.assertEqual(result['pipeline']['task_environment_selection'],self.pipeline['task_environment_selection'])
        for role,expected in self.pipeline['windows'].items():
            actual=result['pipeline']['windows'][role]
            self.assertEqual(actual['environment'],'prod')
            self.assertEqual(actual['environment_selection'],expected['environment_selection'])
            self.assertEqual(actual['request'],self.request[role+'_metadata'])
        self.assertGreater(self.request['baseline_metadata']['start'],self.request['replacement_metadata']['start'])

    def test_report_explains_task_fallback_without_claiming_base_test_was_empty(self):
        result=self.result()
        document=build_report_document(result)
        root=ElementTree.fromstring('<doc>'+document['xml']+'</doc>')
        self.assertEqual([node.tag for node in root],['title','table'])
        for role in ('baseline','replacement'):
            environment,note=_environment_evidence(result['pipeline'],role,{})
            self.assertEqual(environment,'prod')
            self.assertIn('整次统一使用 prod',note)
            self.assertNotIn('所需候选均为空',note)
        self.assertTrue(any(result['pipeline']['task_environment_selection']['attempts'][0]['windows']['baseline']['counts'].values()))
        self.assertEqual(len(document['source_links']),2)

    def test_mixed_environment_result_cannot_finalize_or_render(self):
        result=self.result()
        result['pipeline']['windows']['replacement']['environment']='testtwo'
        with self.assertRaises(InputError):build_report_document(result)
        self.pipeline['windows']['replacement']['environment']='testtwo'
        with self.assertRaises(InputError):self.result()

    def test_fixed_prod_does_not_claim_empty_test_fallback(self):
        self.bind_task(environment='prod',chosen_policy='prod')
        result=self.result()
        xml=build_report_document(result)['xml']
        self.assertNotIn('回退原因：',xml)
        for role in ('baseline','replacement'):
            environment,note=_environment_evidence(result['pipeline'],role,{})
            self.assertEqual(environment,'prod')
            self.assertIn('按配置直接使用 prod',note)
            self.assertNotIn('回退原因：',note)

    def test_test_task_does_not_claim_production_fallback(self):
        self.bind_task(environment='test',chosen_policy='test_then_prod')
        result=self.result()
        for role in ('baseline','replacement'):
            environment,note=_environment_evidence(result['pipeline'],role,{})
            self.assertEqual(environment,'testtwo')
            self.assertNotIn('回退原因：',note)
        self.assertEqual(len(build_report_document(result)['source_links']),2)

    def test_report_does_not_render_private_transport_fields(self):
        result=self.result()
        result['pipeline']['debug_url']='https://private.invalid/query?token=secret'
        result['pipeline']['debug_error']='secret transport detail'
        document=build_report_document(result)
        self.assertNotIn('private.invalid',document['xml'])
        self.assertNotIn('secret',document['xml'])

    def test_failed_query_is_not_rendered_as_confirmed_empty_fallback(self):
        result=self.result()
        result['pipeline']['task_environment_selection']['attempts'][0]['windows']['replacement']['pcm_status']='failed'
        with self.assertRaises(InputError):build_report_document(result)
        with self.assertRaises(InputError):_environment_evidence(result['pipeline'],'replacement',{})

    def test_worker_preserves_no_log_error_and_redacts_interface_error(self):
        cases = ((InputError('testtwo 和 prod 均未查到所需日志。'),'testtwo 和 prod 均未查到所需日志。'),
                 (RuntimeError('https://private.invalid/query?token=secret'),'走查测试处理失败；请查看私有诊断。'))
        for index,(error,expected) in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                path = self.root/f'synthetic-task-{index}.json'
                record = {'kind':'walkthrough_test_local','status':'queued','report_operator_open_id':'ou_synthetic',
                          'analysis_profile':worker.ANALYSIS_PROFILE,
                          'vin':META['vin'],'request_sha256':'synthetic'}
                path.write_text(json.dumps(record),encoding='utf-8')
                settings = {'workspace_root':str(self.root),'runtime_state_dir':str(self.root),
                            'local_analysis':{'synthetic':'readiness is mocked at boundary'}}
                with patch.object(worker,'load_settings',return_value=settings), \
                        patch.object(worker.policy,'activate_lark_environment'), \
                        patch.object(worker.policy,'ensure_user_authorization'), \
                        patch.object(worker,'check_native_environment',return_value={'status':'native_preflight_passed'}), \
                        patch.object(worker,'analyze_task',side_effect=error), \
                        patch.object(worker,'publish') as publish:
                    with self.assertRaises(type(error)):
                        worker.run_worker(path,'unused')
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(saved['status'],'failed')
                self.assertEqual(saved['error'],expected)
                self.assertFalse(path.with_suffix('.result.json').exists())
                self.assertFalse(path.with_suffix('.worker.lock').exists())
                publish.assert_not_called()

    def test_cli_preserves_no_log_error_without_mislabeling_interface_error(self):
        for error in (InputError('testtwo 和 prod 均未查到所需日志。'),
                      RuntimeError('https://private.invalid/query?token=secret')):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(sys,'argv',['worker','worker','--record','synthetic.json','--settings','unused']), \
                        patch.object(worker,'run_worker',side_effect=error),contextlib.redirect_stderr(output):
                    self.assertEqual(worker.main(),1)
                value = json.loads(output.getvalue())
                self.assertEqual(value['status'],'failed')
                if isinstance(error,InputError):
                    self.assertEqual(value['error'],str(error))
                else:
                    self.assertNotIn('secret',value['error'])
                    self.assertNotIn('private.invalid',value['error'])
                    self.assertNotIn('均未查到',value['error'])


if __name__ == '__main__':
    unittest.main()
