import json
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import cloud_pipeline as pipeline
import window_pipeline as window_job
import walkthrough_worker as worker
import native_log_source as ad
import service_log_source as pcm
from request_model import InputError
from test_full_integration import complete_native_fixture, synthetic_rules

META={'vin':'DEM0VEH1CLE000001','start':'2026-10-19T15:10:00','end':'2026-10-19T15:44:00'}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.rules=self.root/'rules.json';self.rules.write_text(json.dumps(synthetic_rules()))
        self.settings={'cloud_settings_path':'synthetic','local_analysis':{'backend':'native_full','execution':'wsl','environment':'test','distribution':'Ubuntu',
            'python':'/venv/python','analyzer_root':'/latest','rules_json':str(self.rules),'keep_raw_logs':True}}
        self.windows={'baseline':META,'replacement':{**META,'start':'2026-10-20T15:10:00','end':'2026-10-20T15:44:00'}}
        selection=patch.object(pipeline.selection,'select_task',side_effect=self.select_task)
        self.selection_mock=selection.start()
        self.addCleanup(selection.stop)

    def task_decision(self):
        from test_task_environment_selection import per_window_task_fixture
        return per_window_task_fixture(policy='test', requests=self.windows)

    def selection_decision(self,metadata):
        role=next(role for role,value in self.windows.items() if value==metadata)
        return pipeline.selection.window_decision(self.task_decision(),role)

    def select_task(self,windows,settings,directory):
        self.assertEqual(settings['local_analysis']['environment'],'test')
        self.assertEqual(windows,self.windows)
        target=Path(directory)/'task-environment-selection.json'
        task=self.task_decision()
        if target.exists():
            self.assertEqual(json.loads(target.read_text()),task)
            return task,None
        ad.write_json(target,task)
        return task,{role:{'ad':{name:[{}] for name in ad.LOG_CLASSES},
                           'pcm':[{}],'pcm_error':False} for role in windows}

    def parser_result(self,**fields):
        result={'environment':'testtwo','source':{'environment':'testtwo'},'metrics':{}}
        result.update(fields)
        return result

    def ad_prepare(self,metadata,settings,root,*,environment='test',selected=None):
        self.assertEqual(environment,'test')
        value={'request':metadata,'environment':'testtwo','source_policy':ad.SOURCE_POLICY,'counts':dict.fromkeys(ad.LOG_CLASSES,1),'files':[]}
        ad.write_json(root/'download-manifest.json',value);return value

    def pcm_prepare(self,metadata,settings,root,*,environment='test',selected=None):
        self.assertEqual(environment,'test')
        value={'request':metadata,'environment':'testtwo','counts':{'log_fsdA_service':1},'files':[]}
        ad.write_json(root/'service-input/download-manifest.json',value);return value

    def test_own_ad_downloads_gate_overlap_without_waiting_other_window(self):
        ready=set();lock=threading.Lock();stages=[];baseline_parsing=threading.Event()
        def a(meta,cfg,root,**kwargs):
            if root.parent.name=='replacement':
                self.assertTrue(baseline_parsing.wait(3),'baseline was blocked by another window')
            value=self.ad_prepare(meta,cfg,root,**kwargs)
            with lock:ready.add((root.parent.name,'ad'))
            return value
        def p(meta,cfg,root,**kwargs):
            value=self.pcm_prepare(meta,cfg,root,**kwargs)
            with lock:ready.add((root.name,'pcm'))
            return value
        def parse(*args,**kwargs):
            role=Path(args[2]).name
            self.assertIn((role,'ad'),ready)
            if 'prepared' not in kwargs:self.assertIn((role,'pcm'),ready)
            self.assertTrue(kwargs.get('prepared') is not None or len(args)==4)
            if role=='baseline':baseline_parsing.set()
            self.assertEqual(kwargs.get('environment'),'test')
            return self.parser_result(status='ok')
        def progress(event):
            snapshot=json.loads((self.root/'job/pipeline-manifest.json').read_text())
            if event['scope']=='window':
                role=event['window'];stage=event['stage']
                self.assertEqual(snapshot['windows'][role]['stage'],stage)
                self.assertEqual(event['request'],self.windows[role])
                stages.append(role+':'+stage)
            else:stages.append(event['stage'])
        with patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=a),patch.object(pcm,'prepare',side_effect=p),patch.object(ad,'analyze_window',side_effect=parse),patch.object(pcm,'parse',side_effect=parse):
            result=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules,on_stage=progress)
        self.assertEqual(stages[0],'selecting_task_environment');self.assertIn('processing_windows',stages);self.assertEqual(stages[-1],'metrics_ready')
        self.assertLess(stages.index('baseline:processing_sources'),stages.index('replacement:sources_ready'))
        for role in self.windows:
            self.assertEqual([stage for stage in stages if stage.startswith(role+':')],
                [role+':'+s for s in ('preparing_sources','processing_sources','sources_ready','metrics_ready')])
        sources=result['source_manifest']['windows']['baseline']['sources']
        self.assertEqual(sources['ad']['query']['end'],'2026-10-19T16:44:00')
        self.assertEqual(sources['pcm']['query']['end'],'2026-10-19T15:44:00')
        self.assertEqual(sources['pcm']['log_classes'],['log_fsdA_service'])

    def test_native_windows_overlap_and_preserve_each_windows_inputs(self):
        barrier=threading.Barrier(2,timeout=3)
        running=[];lock=threading.Lock()
        def native(meta,*args,**kwargs):
            # This rendezvous fails if the two native window jobs are serial.
            barrier.wait()
            with lock:running.append(meta['start'])
            self.assertEqual(kwargs.get('environment'),'test')
            return self.parser_result(metrics={'window':meta['start']})
        def service(meta,*args,**kwargs):
            self.assertEqual(kwargs.get('environment'),'test')
            return self.parser_result(metrics={'window':meta['start']})
        with patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=self.ad_prepare),patch.object(pcm,'prepare',side_effect=self.pcm_prepare),patch.object(ad,'analyze_window',side_effect=native),patch.object(pcm,'parse',side_effect=service):
            result=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        self.assertCountEqual(running,[m['start'] for m in self.windows.values()])
        for role in self.windows:
            self.assertEqual(result['windows'][role]['ad']['metrics']['window'],self.windows[role]['start'])
            self.assertEqual(result['windows'][role]['pcm']['metrics']['window'],self.windows[role]['start'])
            self.assertEqual(result['windows'][role]['environment_selection']['request'],self.windows[role])
            self.assertEqual(result['windows'][role]['environment'],'testtwo')
            self.assertTrue(all(source['environment']=='testtwo' for source in result['windows'][role]['sources'].values()))

    def test_configured_one_worker_runs_whole_windows_serially(self):
        self.settings['local_analysis']['window_workers']=1
        events=[]
        def native(meta,*args,**kwargs):events.append(('ad',meta['start']));return self.parser_result()
        def service(meta,*args,**kwargs):events.append(('pcm',meta['start']));return self.parser_result()
        with patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=self.ad_prepare),patch.object(pcm,'prepare',side_effect=self.pcm_prepare),patch.object(ad,'analyze_window',side_effect=native),patch.object(pcm,'parse',side_effect=service):
            result=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        for index,meta in enumerate(self.windows.values()):
            self.assertCountEqual(events[index*2:index*2+2],[(kind,meta['start']) for kind in ('ad','pcm')])
        self.assertEqual(result['source_manifest']['window_workers'],1)

    def test_invalid_worker_limit_fails_before_cloud_access(self):
        with patch.object(ad,'fetch_window') as fetch:
            for value in (0,3,True,'2'):
                self.settings['local_analysis']['window_workers']=value
                with self.assertRaises(InputError):pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        fetch.assert_not_called()

    def test_one_pcm_source_failure_keeps_other_window_and_ad(self):
        def prepare(meta,cfg,root,**kwargs):
            if root.name=='replacement':raise RuntimeError('secret URL must not escape')
            return self.pcm_prepare(meta,cfg,root,**kwargs)
        with patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=self.ad_prepare),patch.object(pcm,'prepare',side_effect=prepare),patch.object(ad,'analyze_window',return_value=self.parser_result()),patch.object(pcm,'parse',return_value=self.parser_result(status='ok',metrics={'synthetic':1})):
            result=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        self.assertEqual(result['windows']['baseline']['pcm']['status'],'ok')
        self.assertEqual(result['windows']['replacement']['pcm']['status'],'failed')
        self.assertNotIn('secret',json.dumps(result))

    def test_required_ad_source_failure_keeps_other_window_completion(self):
        def prepare(meta,cfg,root,**kwargs):
            if root.parent.name=='baseline':raise RuntimeError('failed source')
            return self.ad_prepare(meta,cfg,root,**kwargs)
        with patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=prepare),patch.object(pcm,'prepare',side_effect=self.pcm_prepare),patch.object(ad,'analyze_window',return_value=self.parser_result()) as parse_ad,patch.object(pcm,'parse',return_value=self.parser_result()) as parse_pcm:
            with self.assertRaises(RuntimeError):pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        self.assertEqual(parse_ad.call_count,1);self.assertEqual(parse_pcm.call_count,1)
        self.assertEqual(parse_ad.call_args.args[0],self.windows['replacement'])
        saved=json.loads((self.root/'job/pipeline-manifest.json').read_text())
        self.assertEqual(saved['stage'],'failed')
        self.assertEqual(saved['windows']['baseline']['stage'],'failed')
        self.assertEqual(saved['windows']['replacement']['stage'],'metrics_ready')
        self.assertTrue((self.root/'job/replacement/window-result.json').is_file())
        self.assertFalse((self.root/'job/baseline/window-result.json').exists())

    def test_parsed_cache_does_not_query_or_download_again(self):
        cached=self.parser_result(metrics={'synthetic':1},source={'sha256':'source','environment':'testtwo'},download={'counts':{}})
        with patch.object(ad,'cached_result',return_value=cached),patch.object(pcm,'cached_result',return_value=cached),patch.object(ad,'fetch_window',side_effect=AssertionError('fetch')),patch.object(pcm,'prepare',side_effect=AssertionError('fetch')),patch.object(ad,'analyze_window',side_effect=AssertionError('parse')),patch.object(pcm,'parse',side_effect=AssertionError('parse')):
            result=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        self.assertEqual(result['windows']['baseline']['ad'],cached)

    def test_data_plan_rejects_other_vehicle_or_changed_request(self):
        with self.assertRaises(InputError):pipeline.run_windows({**self.windows,'replacement':{**META,'vin':'OTHER'}},self.settings,self.root/'job',self.rules)
        ad.write_json(self.root/'job/pipeline-manifest.json',{'version':pipeline.VERSION,'request':{},'rules_sha256':ad.digest(self.rules)})
        with self.assertRaises(InputError):pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)

    def test_result_from_other_window_environment_or_rules_is_rejected(self):
        good={'version':window_job.RESULT_VERSION,'request':META,'environment':'testtwo',
              'environment_selection':self.selection_decision(META),
              'rules_sha256':ad.digest(self.rules),'status':'ok',
              'ad':self.parser_result(status='ok'),'pcm':self.parser_result(status='ok'),
              'sources':window_job.source_plan(META,'test')}
        window_job.validate_result(good,META,ad.digest(self.rules),environment_policy='test')
        for override in ({'request':self.windows['replacement']},{'environment':'prod'},
                         {'rules_sha256':'other'},{'pcm':None},{'sources':{}},{'ad':{'status':'failed'}},
                         {'environment_selection':self.selection_decision(self.windows['replacement'])},
                         {'ad':self.parser_result(environment='prod')},
                         {'pcm':self.parser_result(source={'environment':'prod'})},
                         {'sources':window_job.source_plan(META,'prod')}):
            def invalid_window(metadata, settings, directory, rules, **kwargs):
                role=Path(directory).name
                value=deepcopy(good)
                value.update(request=dict(metadata),
                    environment_selection=self.selection_decision(metadata),
                    sources=window_job.source_plan(metadata,'test'))
                if role=='baseline':value.update(override)
                return value
            with patch.object(pipeline,'process_window',side_effect=invalid_window):
                with self.assertRaises(InputError):pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        saved=json.loads((self.root/'job/pipeline-manifest.json').read_text())
        self.assertEqual(saved['stage'],'failed')

    def test_serial_and_parallel_complete_flows_produce_same_six_metric_comparison(self):
        scenarios=('slow_driver','fast_driver','CNOA','parking')
        def native(meta,*args,**kwargs):
            base=meta==META
            return complete_native_fixture(args[1],meta,args[2],metrics={s:{'p99_ms':100 if base else 90,
                'frame_rate_hz':5 if base else 6,'status':'ok','frame_rate_status':'ok'} for s in scenarios})
        def service(meta,*args,**kwargs):
            return self.parser_result(status='ok',metrics={s:{key:{'mean':20 if meta==META else 10,
                'p99':20 if meta==META else 10,'count':10} for key in ('system_cpu_percent',
                'pcm_p99_infer_delay_ms','pcm_frame_rate_hz','pcm_cpu_percent')} for s in scenarios})
        results=[]
        for workers in (1,2):
            self.settings['local_analysis']['window_workers']=workers
            record={'request':{'vin':META['vin'],**{r+'_metadata':m for r,m in self.windows.items()}},'request_sha256':'synthetic'}
            with patch.object(worker,'rules_snapshot',return_value=self.rules),patch.object(ad,'cached_result',return_value=None),patch.object(pcm,'cached_result',return_value=None),patch.object(ad,'fetch_window',side_effect=self.ad_prepare),patch.object(pcm,'prepare',side_effect=self.pcm_prepare),patch.object(ad,'analyze_window',side_effect=native),patch.object(pcm,'parse',side_effect=service):
                results.append(worker.analyze_local_task(self.settings,record,self.root/f'workers-{workers}'))
        self.assertEqual(results[0]['comparisons'],results[1]['comparisons'])
        self.assertEqual(results[0]['status'],'ok')
        for row in results[1]['comparisons'].values():
            self.assertEqual(len(row['metrics']),6)
            self.assertTrue(all(value['status']=='ok' for value in row['metrics'].values()))
            self.assertEqual(row['metrics']['p99_infer_delay_ms']['delta'],-10)
            self.assertEqual(row['metrics']['frame_rate_hz']['delta'],1)
            self.assertEqual(row['metrics']['pcm_frame_rate_hz']['tone'],'regressed')

    def test_partial_window_retry_reuses_ad_and_completed_window_caches(self):
        import hashlib
        attempts=[]
        def native(meta,cfg,root,rules,**kwargs):
            return complete_native_fixture(root,meta,rules)
        def service(meta,cfg,root,prepared,**kwargs):
            attempts.append(root.name)
            if root.name=='replacement' and attempts.count('replacement')==1:raise RuntimeError('synthetic interruption')
            result={'status':'ok','metrics':{'synthetic':1},'source':{'selected_files':1,'input_manifest_sha256':'avm-source','environment':'testtwo'}}
            checksum=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            ad.write_json(root/'pcm-result.json',{'request':meta,'source_policy':pcm.POLICY,'environment':'testtwo','result':result,'result_sha256':checksum})
            return result
        with patch.object(ad,'fetch_window',side_effect=self.ad_prepare) as fetch,patch.object(pcm,'prepare',side_effect=self.pcm_prepare),patch.object(ad,'analyze_window',side_effect=native) as analyze,patch.object(pcm,'parse',side_effect=service):
            first=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
            self.assertEqual(first['windows']['replacement']['status'],'partial')
            second=pipeline.run_windows(self.windows,self.settings,self.root/'job',self.rules)
        self.assertEqual(analyze.call_count,2);self.assertEqual(fetch.call_count,2)
        self.assertEqual(attempts.count('baseline'),1);self.assertEqual(attempts.count('replacement'),2)
        self.assertTrue(all(value['status']=='ok' for value in second['windows'].values()))
        self.assertEqual(second['windows']['baseline']['sources']['pcm']['input_sha256'],'avm-source')

    def test_pcm_preparation_checks_vin_and_excludes_unrelated_service_logs(self):
        query=SimpleNamespace(environment='test',provider_environment='testtwo',items=[
            {'vin':'OTHER','fileName':'AVM_Service-test.zst','downloadURL':'https://example.com/a'},
            {'vin':META['vin'],'fileName':'netbw-test.zst'}])
        with patch.object(pcm.cloud,'load_settings',return_value={'username':'synthetic'}),patch.object(pcm.cloud,'query_files',return_value=query),patch.object(pcm.cloud,'download_to_file') as download:
            with self.assertRaises(InputError):pcm.prepare(META,self.settings,self.root/'role')
        download.assert_not_called()

    def test_pcm_parser_uses_prepared_files_and_never_fetches(self):
        root=self.root/'role';archive=root/'service-input/item-0001/AVM_Service-test.zst'
        archive.parent.mkdir(parents=True);archive.write_bytes(b'synthetic')
        manifest={'request':META,'source_policy':pcm.POLICY,'environment':'testtwo','files':[
            {'relative_path':archive.relative_to(root/'service-input').as_posix(),'sha256':ad.digest(archive)}]}
        ad.write_json(root/'service-input/download-manifest.json',manifest)
        def decompress(source,destination):
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text('synthetic log')
        with patch.object(pcm.cloud,'query_files',side_effect=AssertionError('network')),patch.object(pcm.cloud,'download_to_file',side_effect=AssertionError('download')),patch.object(pcm,'decompress_zst',side_effect=decompress),patch.object(pcm,'extract',return_value={'status':'ok','metrics':{'synthetic':1}}):
            result=pcm.parse(META,self.settings,root,manifest)
        self.assertEqual(result['source']['selected_files'],1)
        self.assertEqual(pcm.cached_result(META,root),result)

    def test_cached_pcm_integrity_is_checked_and_cache_cleanup_keeps_metrics(self):
        result={'status':'ok','metrics':{'synthetic':1},'source':{'environment':'testtwo'}}
        import hashlib
        checksum=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        ad.write_json(self.root/'pcm-result.json',{'request':META,'source_policy':pcm.POLICY,'environment':'testtwo','result':result,'result_sha256':checksum})
        raw=self.root/'service-input';raw.mkdir();(raw/'raw.zst').write_bytes(b'synthetic')
        self.assertEqual(pcm.cached_result(META,self.root),result)
        pcm.cleanup_inputs(self.root,{'local_analysis':{'keep_raw_logs':False}})
        self.assertFalse(raw.exists());self.assertTrue((self.root/'pcm-result.json').is_file())
        saved=json.loads((self.root/'pcm-result.json').read_text());saved['result']['metrics']['synthetic']=2
        ad.write_json(self.root/'pcm-result.json',saved)
        with self.assertRaises(InputError):pcm.cached_result(META,self.root)


if __name__=='__main__':unittest.main()
