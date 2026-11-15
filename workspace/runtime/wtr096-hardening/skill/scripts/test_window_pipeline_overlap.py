"""Offline proofs for one-window AD/PCM overlap and owned-future lifetime."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Event, Lock, get_ident
import unittest
from unittest.mock import patch

import cloud_log_client as cloud
from environment_policy import provider
from request_model import InputError
from test_task_environment_selection import REQUESTS, per_window_task_fixture
import window_pipeline as window


class WindowOverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rules=self.root/'rules.json'
        self.rules.write_text('{}',encoding='utf-8')
        self.settings={'local_analysis':{'environment':'prod_then_test','keep_raw_logs':False}}
        self.task=per_window_task_fixture({'baseline':'test','replacement':'prod'},policy='prod_then_test',requests=REQUESTS)
        self.discovery={'ad':{},'pcm':[],'pcm_error':False}
        self.events=[]
        self.cleanup=[]
        self.lock=Lock()

    def parser_result(self,environment,status='ok'):
        return {'status':status,'environment':provider(environment),
                'source':{'environment':provider(environment)},'metrics':{}}

    def fetch(self,metadata,settings,root,*,environment,selected):
        root=Path(root)
        self.assertTrue((root.parent/'environment-selection.json').is_file())
        self.assertEqual(metadata['vin'],REQUESTS['baseline']['vin'])
        value={'counts':dict.fromkeys(window.ad.LOG_CLASSES,1)}
        window.ad.write_json(root/'download-manifest.json',value)
        return value

    def prepare(self,metadata,settings,root,*,environment,selected):
        self.assertTrue((Path(root)/'input/download-manifest.json').is_file())
        value={'counts':{window.pcm.LOG_CLASS:1}}
        window.ad.write_json(Path(root)/'service-input/download-manifest.json',value)
        return value

    def native(self,metadata,settings,root,rules,*,environment,prepared):
        self.assertTrue(settings['local_analysis']['keep_raw_logs'])
        return self.parser_result(environment)

    def service(self,metadata,settings,root,prepared,*,environment):
        self.assertTrue(settings['local_analysis']['keep_raw_logs'])
        return self.parser_result(environment)

    def fixtures(self,*,fetch=None,prepare=None,native=None,service=None,ad_cache=None,pcm_cache=None):
        stack=ExitStack()
        stack.enter_context(patch.object(window.ad,'validate_config',return_value=self.settings['local_analysis']))
        stack.enter_context(patch.object(window.ad,'cached_result',return_value=ad_cache))
        stack.enter_context(patch.object(window.pcm,'cached_result',return_value=pcm_cache))
        stack.enter_context(patch.object(window.ad,'fetch_window',side_effect=fetch or self.fetch))
        stack.enter_context(patch.object(window.pcm,'prepare',side_effect=prepare or self.prepare))
        stack.enter_context(patch.object(window.ad,'analyze_window',side_effect=native or self.native))
        stack.enter_context(patch.object(window.pcm,'parse',side_effect=service or self.service))
        stack.enter_context(patch.object(window.ad,'cleanup_inputs',side_effect=lambda *args:self.cleanup.append(('ad',get_ident()))))
        stack.enter_context(patch.object(window.pcm,'cleanup_inputs',side_effect=lambda *args:self.cleanup.append(('pcm',get_ident()))))
        return stack

    def run_window(self,role='baseline',**kwargs):
        return window.process_window(REQUESTS[role],self.settings,self.root/role,self.rules,
            task_selection=self.task,role=role,discovery=deepcopy(self.discovery),
            on_stage=kwargs.pop('on_stage',lambda stage,details:self.events.append((stage,details,get_ident()))),**kwargs)

    def test_pcm_prepare_and_parse_overlap_ad_analysis_only_after_ad_download(self):
        fetched=Event();native_running=Event();pcm_finished=Event()
        controller=get_ident()
        def fetch(*args,**kwargs):
            value=self.fetch(*args,**kwargs)
            fetched.set()
            return value
        def prepare(*args,**kwargs):
            self.assertTrue(fetched.is_set())
            self.assertTrue(native_running.wait(2),'PCM preparation did not overlap AD analysis')
            return self.prepare(*args,**kwargs)
        def native(*args,**kwargs):
            native_running.set()
            self.assertTrue(pcm_finished.wait(2),'AD analysis waited for PCM before starting')
            return self.native(*args,**kwargs)
        def service(*args,**kwargs):
            self.assertTrue(native_running.is_set())
            value=self.service(*args,**kwargs)
            pcm_finished.set()
            return value
        original=deepcopy(self.settings)
        with self.fixtures(fetch=fetch,prepare=prepare,native=native,service=service):
            result=self.run_window()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.settings,original)
        self.assertEqual([row[0] for row in self.events],['preparing_sources','processing_sources','sources_ready','metrics_ready'])
        self.assertEqual({row[2] for row in self.events},{controller})
        self.assertEqual(self.cleanup,[('ad',controller),('pcm',controller)])
        spans=self.events[-2][1]['source_timings']
        self.assertEqual(set(spans),{'ad_fetch','ad_analyze','pcm_prepare','pcm_parse'})
        for row in spans.values():
            self.assertEqual(row['status'],'ok')
            self.assertGreaterEqual(row['elapsed_seconds'],0)
            self.assertLessEqual(row['started_at'],row['ended_at'])
        self.assertLessEqual(spans['ad_fetch']['ended_at'],spans['pcm_prepare']['started_at'])
        self.assertLessEqual(spans['ad_analyze']['started_at'],spans['pcm_parse']['ended_at'])
        self.assertLessEqual(spans['pcm_parse']['ended_at'],spans['ad_analyze']['ended_at'])

    def test_ad_download_failure_never_starts_pcm_or_analysis(self):
        def fail(*args,**kwargs):raise InputError('bounded fixture failure')
        with self.fixtures(fetch=fail),patch.object(window.pcm,'prepare') as prepare,patch.object(window.ad,'analyze_window') as native:
            with self.assertRaises(InputError):self.run_window()
        prepare.assert_not_called();native.assert_not_called()
        self.assertEqual(self.cleanup,[])

    def test_ad_failure_drains_pcm_writer_and_preserves_its_completed_cache(self):
        native_failed=Event();written=Event()
        def native(*args,**kwargs):
            native_failed.set()
            raise InputError('native fixture failed')
        def service(metadata,settings,root,prepared,*,environment):
            self.assertTrue(native_failed.wait(2))
            target=Path(root)/'owned-pcm-cache.json'
            target.write_text('complete',encoding='utf-8')
            written.set()
            return self.parser_result(environment)
        with self.fixtures(native=native,service=service):
            with self.assertRaisesRegex(InputError,'native fixture'):self.run_window()
        self.assertTrue(written.is_set())
        self.assertEqual((self.root/'baseline/owned-pcm-cache.json').read_text(),'complete')
        self.assertEqual(self.cleanup,[])
        self.assertEqual(self.events[-1][0],'sources_failed')
        self.assertEqual(self.events[-1][1]['source_outcomes'],{'ad':'failed','pcm':'ready'})
        self.assertIn('ended_at',self.events[-1][1]['source_timings']['pcm_parse'])

    def test_pcm_auth_failure_is_fatal_after_ad_finishes(self):
        completed=Event()
        error=cloud.CloudAuthError(operation='query',environment='test',http_status=401)
        def prepare(*args,**kwargs):raise error
        def native(*args,**kwargs):
            completed.set()
            return self.native(*args,**kwargs)
        with self.fixtures(prepare=prepare,native=native):
            with self.assertRaises(cloud.CloudAuthError) as caught:self.run_window()
        self.assertIs(caught.exception,error)
        self.assertTrue(completed.is_set())
        self.assertEqual(self.cleanup,[])
        self.assertEqual(self.events[-1][1]['sources']['pcm']['error_diagnostic'],cloud.auth_failure_diagnostic(error))

    def test_optional_pcm_prepare_failure_is_partial_and_keeps_failed_inputs(self):
        def prepare(*args,**kwargs):raise RuntimeError('private fixture response must not escape')
        with self.fixtures(prepare=prepare),patch.object(window.pcm,'parse') as parse:
            result=self.run_window()
        parse.assert_not_called()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['ad']['status'],'ok')
        self.assertEqual(result['pcm']['status'],'failed')
        self.assertNotIn('private fixture',json.dumps(result))
        self.assertEqual([row[0] for row in self.cleanup],['ad'])
        self.assertEqual(self.events[-2][1]['source_timings']['pcm_parse']['status'],'skipped')

    def test_pcm_response_contract_failure_is_fatal_and_wins_over_generic_ad_failure(self):
        error=cloud.CloudResponseContractError(reason='data_not_object',operation='query',environment='test',http_status=200,business_code=200)
        def prepare(*args,**kwargs):raise error
        def native(*args,**kwargs):raise InputError('native fixture failed')
        with self.fixtures(prepare=prepare,native=native):
            with self.assertRaises(cloud.CloudResponseContractError) as caught:self.run_window()
        self.assertIs(caught.exception,error)
        self.assertEqual(self.events[-1][1]['source_outcomes'],{'ad':'failed','pcm':'failed'})
        self.assertEqual(self.cleanup,[])

    def test_progress_callback_failure_before_submit_leaves_no_pcm_writer(self):
        def progress(stage,details):
            if stage=='processing_sources':raise InputError('callback fixture failed')
        with self.fixtures(),patch.object(window.pcm,'prepare') as prepare,patch.object(window.ad,'analyze_window') as analyze:
            with self.assertRaisesRegex(InputError,'callback fixture'):
                self.run_window(on_stage=progress)
        prepare.assert_not_called();analyze.assert_not_called()
        self.assertEqual(self.cleanup,[])

    def test_optional_pcm_parse_failure_keeps_diagnostic_inputs(self):
        def service(*args,**kwargs):raise OSError('private fixture')
        with self.fixtures(service=service):result=self.run_window()
        self.assertEqual(result['status'],'partial')
        self.assertEqual([row[0] for row in self.cleanup],['ad'])

    def test_pcm_renewed_query_error_never_becomes_optional_missing(self):
        with self.fixtures(),patch.object(window.pcm,'discover',side_effect=RuntimeError('private query')) as discover:
            with self.assertRaisesRegex(InputError,'来源查询失败'):
                window.process_window(REQUESTS['baseline'],self.settings,self.root/'baseline',self.rules,
                    task_selection=self.task,role='baseline',discovery=None)
        discover.assert_called_once()
        self.assertEqual(self.cleanup,[])

    def test_manifest_hashes_are_frozen_before_serial_cleanup(self):
        expected={}
        def prepare(*args,**kwargs):
            value=self.prepare(*args,**kwargs)
            expected['pcm']=window.ad.digest(Path(args[2])/'service-input/download-manifest.json')
            return value
        def native(*args,**kwargs):
            expected['ad']=window.ad.digest(Path(args[2])/'input/download-manifest.json')
            return self.native(*args,**kwargs)
        def cleanup(directory,settings):
            for relative in ('input/download-manifest.json','service-input/download-manifest.json'):
                (Path(directory)/relative).unlink(missing_ok=True)
        with self.fixtures(prepare=prepare,native=native),patch.object(window.ad,'cleanup_inputs',side_effect=cleanup):
            result=self.run_window()
        self.assertEqual({key:value['input_sha256'] for key,value in result['sources'].items()},expected)

    def test_valid_parser_caches_skip_downloads_queries_and_parsers(self):
        native=self.parser_result('test');native['download']={'counts':dict.fromkeys(window.ad.LOG_CLASSES,1)}
        service=self.parser_result('test');service['source'].update(selected_files=2,input_manifest_sha256='a'*64)
        with self.fixtures(ad_cache=native,pcm_cache=service),patch.object(window.ad,'fetch_window') as fetch,patch.object(window.pcm,'prepare') as prepare,patch.object(window.ad,'analyze_window') as analyze,patch.object(window.pcm,'parse') as parse,patch.object(window.pcm,'discover') as discover:
            result=self.run_window()
        for method in (fetch,prepare,analyze,parse,discover):method.assert_not_called()
        self.assertEqual(result['ad'],native);self.assertEqual(result['pcm'],service)
        self.assertTrue(all(row['status']=='cached' for row in self.events[-2][1]['source_timings'].values()))

    def test_mixed_environment_windows_never_share_inputs_or_results(self):
        seen=[];lock=Lock()
        def native(metadata,settings,root,rules,*,environment,prepared):
            with lock:seen.append((Path(root).name,'ad',environment))
            return self.parser_result(environment)
        def service(metadata,settings,root,prepared,*,environment):
            with lock:seen.append((Path(root).name,'pcm',environment))
            return self.parser_result(environment)
        with self.fixtures(native=native,service=service):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures={role:pool.submit(self.run_window,role) for role in ('baseline','replacement')}
                results={role:future.result() for role,future in futures.items()}
        self.assertCountEqual(seen,[('baseline','ad','test'),('baseline','pcm','test'),('replacement','ad','prod'),('replacement','pcm','prod')])
        for role,environment in (('baseline','testtwo'),('replacement','prod')):
            self.assertEqual(results[role]['environment'],environment)
            self.assertEqual({row['environment'] for row in results[role]['sources'].values()},{environment})


if __name__=='__main__':unittest.main()
