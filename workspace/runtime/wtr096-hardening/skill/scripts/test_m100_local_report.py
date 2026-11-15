"""Synthetic tests for single-platform pruning and local attachment export."""
import hashlib
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import shutil
import uuid

import run_m100 as runner
import m100_attachment_worker as worker


CONFIG="""
_PERCEPTION_STATE={'0':'slow_driver','1':'parking','4':'fast_driver','5':'CNOA'}
CONFIG_PATH={'FSDA_M100_ULTRA':['FSDA_M100'],'FSDB_M100_ULTRA':['FSDB_M100'],'FSDA_M100_MAX':[],'FSDA_Thor':[],'FSDA_Orin':[],'XVM_':['XVM_MCU','XVM_BZCU']}
CONFIG_M100_ULTRA={'summary':{'not_plot':False,'template':['synthetic']},'perception_state':_PERCEPTION_STATE}
CONFIG_M100_MAX={}
CONFIG_THOR={}
CONFIG_ORIN={}
CONFIG_MAPS={'m100-ultra':CONFIG_M100_ULTRA,'m100-max':CONFIG_M100_MAX,'thor':CONFIG_THOR,'orin':CONFIG_ORIN}
"""


def records():
    values={}
    for name in ('header_delay','vla_parking_header_delay','VlaModel_FPS','VlaParkingDLM_FPS'):
        values[name]={**{'P99_'+s:100 for s in ('0','4','5','1')},**{'mean_'+s:8 for s in ('0','4','5','1')}}
    return [{'metric':k,'statistics':v} for k,v in values.items()]


def rules_fixture():
    return {'synthetic':{r['metric']:{'soc_type':'synthetic_container','log_tag':'SYNTHETIC',
        'func':'custom','input':'var=$15 "," $7;max_threshold=998',
        'calculate_type':'','ps_type':'ps_time'} for r in records()}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.gettempdir())/('walkthrough-native-'+uuid.uuid4().hex[:12])
        self.root.mkdir()
        self.addCleanup(shutil.rmtree,self.root)

    def test_platform_map_pruned_and_complete_source_mapping_preserved(self):
        path=self.root/'config.py'
        path.write_text(CONFIG)
        runner.slim_platform_config(path)
        namespace={}
        exec(path.read_text(),namespace)
        self.assertEqual(set(namespace['CONFIG_MAPS']),{'m100-ultra'})
        self.assertEqual(namespace['CONFIG_PATH']['XVM_'],['XVM_MCU','XVM_BZCU'])
        self.assertIn('FSDA_M100_ULTRA',namespace['CONFIG_PATH'])
        self.assertNotIn('CONFIG_THOR',namespace)
        self.assertIn('_PERCEPTION_STATE',namespace)

    def test_required_rule_selection_preserves_latest_fields_and_no_source_mutation(self):
        rules=rules_fixture()
        rules['synthetic']['unneeded']={'func':'multi_log_diff_value'}
        rules['irrelevant']={'other':{}}
        original=copy.deepcopy(rules)
        selected=worker.select_required_rules(rules)
        self.assertEqual(selected,rules_fixture())
        self.assertEqual(rules,original)
        selected['synthetic']['header_delay']['input']='changed'
        self.assertEqual(rules,original)
        plot,value,*_=self.hooks(worker.select_required_rules(rules))
        self.assertEqual(value.load_config_from_feishu('unused'),rules_fixture())

    def test_rule_missing_duplicate_or_new_dependency_fails_before_parsing(self):
        missing=rules_fixture();del missing['synthetic']['header_delay']
        duplicate=rules_fixture();duplicate['other']={'HEADER_DELAY':duplicate['synthetic']['header_delay']}
        cases=[missing,duplicate]
        for key,new in (('func','multi_log_diff_value'),('calculate_type','emb_type'),('ps_type','ps'),('soc_type','')):
            rules=rules_fixture();rules['synthetic']['header_delay'][key]=new;cases.append(rules)
        for rules in cases:
            with self.subTest(rules=rules),self.assertRaises(ValueError):worker.select_required_rules(rules)

    def test_unknown_platform_config_contract_fails(self):
        path=self.root/'config.py'
        path.write_text('CONFIG_MAPS=build_maps()\nCONFIG_PATH={}\n')
        with self.assertRaises(ValueError):
            runner.slim_platform_config(path)

    def test_only_m100_ultra_is_supported(self):
        self.assertEqual(runner.PLATFORMS,{'m100-ultra'})

    def test_export_keeps_p99_and_mean_fps_per_state(self):
        result=worker.export_result(records(),{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['metrics']['parking']['p99_source']['statistic'],'P99_1')
        self.assertEqual(result['metrics']['slow_driver']['fps_source']['statistic'],'mean_0')
        self.assertNotIn('<script',(self.root/'report.html').read_text(encoding='utf-8'))

    def test_missing_data_remains_partial(self):
        rows=records()
        rows[0]['statistics']['P99_0']='-'
        result=worker.export_result(rows,{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')
        self.assertEqual(result['status'],'partial')
        self.assertIsNone(result['metrics']['slow_driver']['p99_ms'])

    def test_all_missing_rejected(self):
        with self.assertRaises(ValueError):
            worker.export_result([],{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')

    def test_duplicate_metric_rejected(self):
        rows=records()
        rows.append(rows[0])
        with self.assertRaises(ValueError):
            worker.export_result(rows,{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')

    def test_non_target_case_distinct_metrics_preserved(self):
        rows=records()+[{'metric':'trafficlight_fps','statistics':{'mean':11}},
                        {'metric':'TrafficLight_FPS','statistics':{'mean':22}}]
        result=worker.export_result(rows,{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['statistics'][-2:],rows[-2:])
        html=(self.root/'report.html').read_text(encoding='utf-8')
        self.assertIn('trafficlight_fps',html)
        self.assertIn('TrafficLight_FPS',html)

    def test_html_escapes_diagnostics(self):
        rows=records()
        rows[0]['statistics']['diagnostic']='<script>bad()</script>'
        worker.export_result(rows,{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')
        html=(self.root/'report.html').read_text(encoding='utf-8')
        self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('<script>',html)

    def hooks(self,rules=None):
        class Report:
            def summary_report(self,*args):
                raise AssertionError('native upload must never run')
            def _format_report_data(self,data):
                return data
        plot=types.SimpleNamespace(FeishuTemplateReport=Report)
        value=types.SimpleNamespace(load_config_from_feishu=lambda *a: {},update_last_valid_time_to_feishu=lambda *a: (_ for _ in ()).throw(AssertionError('write')))
        calls=[]
        client=types.SimpleNamespace(request=lambda *a,**k:calls.append(a))
        config=types.SimpleNamespace(CONFIG_MAPS={'m100-ultra':{'summary':{'not_plot':False}}})
        return plot,value,client,config,calls,worker.install_hooks(plot,value,client,config,rules)

    def test_publishing_and_config_updates_are_intercepted(self):
        plot,value,client,config,calls,captured=self.hooks()
        values={r['metric']:{**r['statistics'],'image':'private-image-path'} for r in records()}
        self.assertIsNone(plot.FeishuTemplateReport().summary_report({'theme':values},'title'))
        self.assertEqual(len(captured),4)
        self.assertNotIn('image',str(captured))
        value.update_last_valid_time_to_feishu('unused')
        self.assertTrue(config.CONFIG_MAPS['m100-ultra']['summary']['not_plot'])
        with self.assertRaises(RuntimeError):
            client.request('POST','https://example.com/open-apis/drive/v1/files/copy')
        self.assertEqual(calls,[])

    def test_rule_snapshot_blocks_all_feishu_requests(self):
        plot,value,client,config,calls,captured=self.hooks({'synthetic':{'metric':{}}})
        self.assertEqual(value.load_config_from_feishu('unused'),{'synthetic':{'metric':{}}})
        with self.assertRaises(RuntimeError):
            client.request('GET','https://example.com/open-apis/sheets/v2/synthetic')
        self.assertEqual(calls,[])

    def synthetic_source(self):
        source=self.root/'source'
        (source/'common').mkdir(parents=True)
        (source/'general_performance').mkdir()
        (source/'common/perf_path.py').write_text('# synthetic')
        # The current worker applies a guarded runtime patch to this original
        # execution shape before running start.py; keep the fixture faithful.
        (source/'common/perf_csv.py').write_text(
            'from pathlib import Path\n'
            'def should_keep_log(log_path,time_range,state):return True\n'
            'class RecordPerformance:\n'
            ' def extract_logs(self,theme,soc_type):\n'
            '  valid_filenames=soc_type\n'
            '  for file_name in valid_filenames:\n'
            '   log_paths_list=list(Path(self.log_path).glob(f"{file_name}*.log"))\n'
            '   state={"kept_first_after_end":False}\n'
            '   for log_path in log_paths_list:\n'
            '    if not should_keep_log(log_path,self.time_range,state):continue\n')
        (source/'general_performance/config.py').write_text(CONFIG)
        original_ini='[PARAMETER]\ntheme=summary\nupload=1\n'
        (source/'general_performance/o.ini').write_text(original_ini)
        (source/'common/feishu_client.py').write_text("def request(*a,**k):raise AssertionError('network must not run')\n")
        (source/'common/perf_value.py').write_text("def load_config_from_feishu(*a,**k):raise AssertionError('network must not run')\ndef update_last_valid_time_to_feishu(*a,**k):raise AssertionError('write must not run')\n")
        (source/'common/perf_plot.py').write_text("class FeishuTemplateReport:\n def _format_report_data(self,data):return data\n def summary_report(self,*a):raise AssertionError('upload must not run')\n")
        fake_values={r['metric']:r['statistics'] for r in records()}
        (source/'general_performance/start.py').write_text(
            "import configparser\nfrom common.perf_plot import FeishuTemplateReport\nfrom common import perf_value\n"
            "c=configparser.ConfigParser();c.read('o.ini');c['PARAMETER']['vin']='DEM0VEH1CLE000001'\n"
            "with open('o.ini','w') as f:c.write(f)\n"
            f"FeishuTemplateReport().summary_report({{'synthetic':{fake_values!r}}},'synthetic')\n"
            "perf_value.update_last_valid_time_to_feishu('unused')\n")
        logs=self.root/'input'
        (logs/'log_fsdA').mkdir(parents=True)
        (logs/'log_fsdA/synthetic.log').write_text('SYNTHETIC')
        rules=self.root/'rules.json'
        rules.write_text(json.dumps(rules_fixture()))
        return source,logs,rules,original_ini

    def test_unapproved_source_is_rejected_before_process_or_publication(self):
        source,logs,rules,original_ini=self.synthetic_source()
        with patch.object(runner.subprocess,'Popen',side_effect=AssertionError('No unapproved process')):
            with self.assertRaises(ValueError):
                runner.preflight(source,logs,'20260407150000','20260407160000','m100-ultra',rules)
        self.assertEqual((source/'general_performance/o.ini').read_text(),original_ini)
        self.assertFalse((self.root/'jobs').exists())

    def test_two_unapproved_sources_cannot_produce_success_results(self):
        source,logs,rules,original_ini=self.synthetic_source()
        start=source/'general_performance/start.py'
        source_hash=hashlib.sha256(start.read_bytes()).hexdigest()
        def run(begin,end):
            with self.assertRaises(ValueError):
                runner.preflight(source,logs,begin,end,'m100-ultra',rules)
            return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as executor:
            a=executor.submit(run,'20260407150000','20260407160000')
            b=executor.submit(run,'20260407170000','20260407180000')
            results=[a.result(),b.result()]
        self.assertEqual(results,['rejected','rejected'])
        self.assertFalse((self.root/'jobs').exists())
        self.assertEqual((source/'general_performance/o.ini').read_text(),original_ini)
        self.assertEqual(hashlib.sha256(start.read_bytes()).hexdigest(),source_hash)


if __name__=='__main__':
    unittest.main()
