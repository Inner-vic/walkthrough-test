"""Synthetic native-boundary integration; no analyzer execution or network."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import environment_selection as selection
import native_log_source as native
import service_log_source as pcm
import walkthrough_worker as worker
from request_model import InputError
from test_source_selection_fixture import synthetic_selection_audit, attach_synthetic_execution

SCENARIOS = (('slow_driver','0'),('fast_driver','4'),('CNOA','5'),('parking','1'))
META = {'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:48:00','end':'2026-10-21T16:22:00'}
OTHER = {**META,'start':'2026-10-21T14:50:00','end':'2026-10-21T15:24:00'}


def synthetic_rules():
    return {'synthetic': {name:{'soc_type':'synthetic','calculate_type':'','func':'custom'} for name in
            ('header_delay','vla_parking_header_delay','vlamodel_fps','vlaparkingdlm_fps','extra_metric')},
            'XVM':{'conditional_metric':{'calculate_type':'emb_type','soc_type':'XVM_'},
                   'missing_metric':{'calculate_type':'','soc_type':'XVM_'}}}


def complete_native_fixture(directory, metadata, rules, *, environment='testtwo', metrics=None, status='ok'):
    """Persist all supplied rules/statistics and both hashed cache artifacts."""
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    rule_data=json.loads(Path(rules).read_text(encoding='utf-8'))
    if metrics is None:
        metrics={s:{'label':s,'p99_ms':5.0,'frame_rate_hz':10.0,'status':'ok','frame_rate_status':'ok'} for s,_ in SCENARIOS}
    statistics=[]
    for theme, values in rule_data.items():
        for metric, config in values.items():
            fields={'mean':17.0,'max':25.0,'min':None,'diagnostic':None}
            if metric.casefold() in ('header_delay','vla_parking_header_delay'):
                fields={('P99_'+state):metrics.get(s,{}).get('p99_ms') for s,state in SCENARIOS
                        if (s=='parking') == (metric.casefold()=='vla_parking_header_delay')}
            elif metric.casefold() in ('vlamodel_fps','vlaparkingdlm_fps'):
                fields={('mean_'+state):metrics.get(s,{}).get('frame_rate_hz') for s,state in SCENARIOS
                        if (s=='parking') == (metric.casefold()=='vlaparkingdlm_fps')}
            record={'theme':theme,'metric':metric,'rule':config,'statistics':fields,'status':'available','native_publish':True}
            if config.get('calculate_type')=='emb_type':
                record.update(statistics={},status='conditional_skip',native_publish=False,skip_reason='synthetic explicit condition')
            if metric=='missing_metric':
                record.update(statistics={'diagnostic':'synthetic missing source'},status='missing',diagnostic='synthetic missing source')
            statistics.append(record)
    detail=root/'report-data.json';html=root/'native-report.html'
    report={'status':status,'metadata':{'vin':metadata['vin'],'time_range':re.sub(r'\D','',metadata['start'])+'/'+re.sub(r'\D','',metadata['end'])},
            'source_selection':synthetic_selection_audit(),
            'metrics':metrics,'statistics':statistics,'analysis_scope':{'profile':native.ANALYSIS_PROFILE,
                'rule_count':sum(len(v) for v in rule_data.values()),'native_execution':'mock_boundary'},
            'skipped_records':[r for r in statistics if r['status']=='conditional_skip']}
    attach_synthetic_execution(report,rule_data)
    from native_sheet_test_fixture import synthetic_native_sheet_payload
    report['native_sheet_data'] = synthetic_native_sheet_payload(report,rule_data,metadata,native.digest(rules))
    native.write_json(detail,report);html.write_text('<html><body>Synthetic native boundary</body></html>',encoding='utf-8')
    manifest=root/'input/download-manifest.json'
    input_hash=native.digest(manifest) if manifest.is_file() else 'a'*64
    result={'status':status,'request':metadata,'environment':environment,'source_policy':native.SOURCE_POLICY,
            'source_selection':report['source_selection'],
            'runtime_execution':report['runtime_execution'], 'analyzer_contract':report['analyzer_contract'],
            'execution_compat':report['execution_compat'],
            'execution_compat_runtime':report['execution_compat_runtime'],
            'rules_sha256':native.digest(rules),'metrics':metrics,'analysis_scope':report['analysis_scope'],
            'source':{'environment':environment,'sha256':native.digest(detail)},
            'artifact':{'path':str(html),'sha256':native.digest(html)},
            'full_statistics':{'path':str(detail),'sha256':native.digest(detail),'bytes':detail.stat().st_size},
            'normalization':{'version':'native-input-v1','input_sha256':input_hash,'xvm_expanded_bytes':0},
            'download':{'counts':dict.fromkeys(native.LOG_CLASSES,1)},'missing_log_classes':[]}
    native.write_json(root/'native-result.json',result)
    return result


def native_boundary(command, **kwargs):
    """Mock only the external process; native adapter validates its full output."""
    def argument(name):return command[command.index(name)+1]
    start=argument('--start');end=argument('--end')
    iso=lambda value:value[:4]+'-'+value[4:6]+'-'+value[6:8]+'T'+value[8:10]+':'+value[10:12]+':'+value[12:14]
    meta={'vin':argument('--expected-vin'),'start':iso(start),'end':iso(end)}
    root=native.host_path(argument('--work-root'))/'synthetic-native'
    value=complete_native_fixture(root,meta,native.host_path(argument('--rules-json')))
    return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'ok','artifact':value['artifact'],
        'upstream_source_sha256':{'synthetic.py':'b'*64}}),stderr='')


def complete_pcm_fixture(metadata,settings,directory,prepared,*,environment='test'):
    env='testtwo' if environment=='test' else 'prod'
    result={'status':'ok','metrics':{s:{name:{'mean':4.0,'p99':20.0,'count':10} for name in
             ('pcm_p99_infer_delay_ms','pcm_frame_rate_hz','pcm_cpu_percent','system_cpu_percent')} for s,_ in SCENARIOS},
            'coverage':{'files_read':1,'lines_read':10},'source':{'environment':env,'selected_files':1,'input_manifest_sha256':'c'*64}}
    checksum=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    native.write_json(Path(directory)/'pcm-result.json',{'request':metadata,'source_policy':pcm.POLICY,
        'environment':env,'result':result,'result_sha256':checksum})
    return result


class FullIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.rules=self.root/'rules.json';native.write_json(self.rules,synthetic_rules())
        self.settings={'cloud_settings_path':'synthetic-private-config','local_analysis':{'backend':'native_full',
            'environment':'test','execution':'wsl','distribution':'Synthetic','python':'/venv/python',
            'analyzer_root':'/latest','rules_json':str(self.rules),'keep_raw_logs':True}}
        self.request={'vin':META['vin'],'baseline_metadata':META,'replacement_metadata':OTHER}

    def selected(self,windows,settings,root):
        from test_task_environment_selection import per_window_task_fixture
        decision=per_window_task_fixture(policy='test',requests=windows)
        native.write_json(Path(root)/'task-environment-selection.json',decision)
        return decision,{role:{'ad':{kind:[{}] for kind in native.LOG_CLASSES},
                               'pcm':[{}],'pcm_error':False} for role in windows}

    def prepare_ad(self,meta,settings,root,**kwargs):
        import zstandard
        root=Path(root);rows=[]
        for index,name in enumerate(native.LOG_CLASSES):
            filename=('XVM_MCU-synthetic.zst' if name=='log_xcu_mcu' else 'XVM_BZCU-synthetic.zst' if name=='log_bzcu_eth' else 'synthetic.zip')
            target=root/name/filename;target.parent.mkdir(parents=True,exist_ok=True)
            payload=b'synthetic text\n'+meta['start'].encode()
            if filename.endswith('.zst'):
                target.write_bytes(zstandard.ZstdCompressor().compress(payload))
            else:
                with zipfile.ZipFile(target,'w') as archive:
                    archive.writestr('synthetic_'+name+'.log',payload)
            rows.append({'relative_path':target.relative_to(root).as_posix(),'sha256':native.digest(target),'bytes':target.stat().st_size,'log_class':name})
        value={'request':meta,'source_policy':native.SOURCE_POLICY,'environment':'testtwo','archive_query':native.archive_query_metadata(meta),
               'files':rows,'counts':dict.fromkeys(native.LOG_CLASSES,1),'bytes':sum(r['bytes'] for r in rows)}
        native.write_json(root/'download-manifest.json',value);return value

    def prepare_pcm(self,meta,settings,root,**kwargs):
        value={'request':meta,'source_policy':pcm.POLICY,'environment':'testtwo','counts':{pcm.LOG_CLASS:1},'files':[]}
        native.write_json(Path(root)/'service-input/download-manifest.json',value);return value

    def test_full_two_windows_native_boundary_sheet_data_and_resume(self):
        barrier=threading.Barrier(2,timeout=5);calls=[]
        def run(command,**kwargs):
            calls.append(command);barrier.wait();return native_boundary(command,**kwargs)
        job=self.root/'job';record={'request':self.request,'request_sha256':'synthetic-comparison'}
        with patch.object(worker,'rules_snapshot',return_value=self.rules),patch.object(selection,'select_task',side_effect=self.selected), \
                patch.object(native,'fetch_window',side_effect=self.prepare_ad),patch.object(pcm,'prepare',side_effect=self.prepare_pcm), \
                patch.object(pcm,'parse',side_effect=complete_pcm_fixture),patch.object(native.subprocess,'run',side_effect=run), \
                patch.object(native.cloud,'request_json',side_effect=AssertionError('network')):
            result=worker.analyze_local_task(self.settings,record,job)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['delivery_format'],'docx_with_native_sheet_details')
        self.assertEqual(set(result['native_sheet_data']),{'baseline','replacement'})
        self.assertFalse(list(job.rglob('*.xlsx')))
        checksums={}
        for role,meta in [('baseline',META),('replacement',OTHER)]:
            attachment=result['native_sheet_data'][role]
            from native_sheet_data import load_native_sheet_data
            payload = load_native_sheet_data(attachment['path'],attachment['sha256'])
            self.assertEqual(len(payload['statistics']),7)
            self.assertEqual(sum(row['status']=='conditional_skip' for row in payload['statistics']),1)
            self.assertEqual(attachment['request'],meta)
            self.assertEqual(attachment['environment'],'testtwo')
            self.assertTrue(Path(attachment['path']).resolve().is_relative_to((job/role).resolve()))
            self.assertEqual(payload['delivery_context']['pcm_result']['source']['environment'],'testtwo')
            checksums[role]=attachment['sha256']
            self.assertEqual(len(result['comparisons']['slow_driver']['metrics']),6)
        self.assertNotEqual(result['native_sheet_data']['baseline']['request_sha256'],result['native_sheet_data']['replacement']['request_sha256'])
        with patch.object(worker,'rules_snapshot',return_value=self.rules), \
                patch.object(native.cloud,'request_json',side_effect=AssertionError('network retry')), \
                patch.object(native.subprocess,'run',side_effect=AssertionError('native retry')), \
                patch.object(native,'fetch_window',side_effect=AssertionError('download retry')), \
                patch.object(pcm,'parse',side_effect=AssertionError('pcm retry')):
            repeated=worker.analyze_local_task(self.settings,record,job)
        self.assertEqual(repeated['comparisons'],result['comparisons'])
        self.assertEqual({r:a['sha256'] for r,a in repeated['native_sheet_data'].items()},checksums)
        data=job/'baseline/report-data.json';data.write_text(data.read_text(encoding='utf-8')+' ',encoding='utf-8')
        with self.assertRaises(InputError):native.cached_result(META,job/'baseline',self.rules)


if __name__=='__main__':
    unittest.main()
