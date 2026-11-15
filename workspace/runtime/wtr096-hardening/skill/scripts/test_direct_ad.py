"""Synthetic boundaries for the native-equivalent memory adapter."""
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
import direct_ad as ad

VIN='DEM0VEH1CLE000001'
META={'vin':VIN,'start':'2026-10-21T14:00:00','end':'2026-10-21T14:10:00'}
def rules():
    return {'fixture':{name:{'func':'custom','calculate_type':'','ps_type':'ps_time',
        'soc_type':'synthetic_container','log_tag':'SYNTHETIC','input':'var = "" $10 "," find_digit($7);max_threshold=1000'}
        for name in ('header_delay','vla_parking_header_delay','VlaModel_FPS','VlaParkingDLM_FPS')}}

class DirectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)

    def fixture(self):
        path=self.root/'synthetic_2026_10_21_14_10_00.zip'
        line='10-21 14:01:00.000 42 x x x state:0 x x 5.0 SYNTHETIC\n'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr(f'FSDA_M100-{VIN}-2026_10_21_14_10_00.log','synthetic identity')
            archive.writestr('synthetic_container_2026_10_21_14_10_00.log',line*20)
            archive.writestr('unrelated_container.log','not parsed\n'*30)
        manifest={'request':META,'environment':'testtwo','files':[{'relative_path':path.name,'sha256':ad.digest(path)}]}
        (self.root/'download-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return path,manifest

    def test_streams_zip_only_without_csv_or_subprocess(self):
        self.fixture()
        with patch('subprocess.run',side_effect=AssertionError('subprocess forbidden')):
            result=ad.analyze(self.root,META,rules())
        self.assertEqual(result['sources']['target_members'],1)
        self.assertTrue(all(v['rows']==20 for v in result['samples'].values()))
        self.assertEqual(result['records'][0]['statistics']['mean_0'],'5.0')
        self.assertEqual(list(self.root.rglob('*.csv')),[])
        self.assertEqual(list(self.root.rglob('*.log')),[])

    def test_tampering_and_wrong_identity_rejected(self):
        path,_=self.fixture()
        with self.assertRaises(ValueError):ad.analyze(self.root,{**META,'vin':'WRONG'},rules())
        path.write_bytes(path.read_bytes()+b'tampered')
        with self.assertRaises(ValueError):ad.analyze(self.root,META,rules())

    def test_timestamp_less_archive_follows_native_exclusion(self):
        _,manifest=self.fixture()
        ignored=self.root/'without-native-timestamp.zip'
        with zipfile.ZipFile(ignored,'w') as archive:
            archive.writestr('synthetic_container_2026_10_21_14_10_00.log','SYNTHETIC invalid target row')
        manifest['files'].append({'relative_path':ignored.name,'sha256':ad.digest(ignored)})
        (self.root/'download-manifest.json').write_text(json.dumps(manifest))
        result=ad.analyze(self.root,META,rules())
        self.assertEqual(result['sources']['archives_considered'],1)
        self.assertTrue(all(v['rows']==20 for v in result['samples'].values()))

    def test_archive_path_escape_rejected(self):
        _,manifest=self.fixture()
        manifest['files'][0]['relative_path']='../outside.zip'
        (self.root/'download-manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):ad.analyze(self.root,META,rules())

    def test_rule_changes_do_not_execute_code_or_guess_dependencies(self):
        for text in ('var=system("malicious");','var = "" $10 "," find_digit($7);unknown=1'):
            changed=rules();changed['fixture']['header_delay']['input']=text
            with self.assertRaises(ValueError):ad.compile_rules(changed)
        changed=rules();changed['fixture']['header_delay']['input']='var = "" find_digit($15) "," $8;min_threshold=1;max_threshold=999'
        _,compiled=ad.compile_rules(changed)
        self.assertEqual(compiled['header_delay']['terms'],[(14,True),(7,False)])
        self.assertEqual(compiled['header_delay']['thresholds'],{'min_threshold':1.,'max_threshold':999.})

    def test_first_archive_after_end_and_selected_conflict_boundary(self):
        start=datetime.fromisoformat(META['start']).replace(tzinfo=ad.TZ)
        end=datetime.fromisoformat(META['end']).replace(tzinfo=ad.TZ)
        names=['synthetic_container_2026_10_21_13_59_00.log','synthetic_container_2026_10_21_14_05_00.log',
               'synthetic_container_2026_10_21_14_11_00.log','synthetic_container_2026_10_21_14_12_00.log']
        members={n:{'name':n} for n in names};members[names[-1]]['conflicts']=[{}]
        self.assertEqual([m['name'] for m in ad.choose_members(members,['synthetic_container'],start,end)],names[1:3])
        members[names[2]]['conflicts']=[{}]
        with self.assertRaises(ValueError):ad.choose_members(members,['synthetic_container'],start,end)

    def test_state_transition_trim_before_range_and_linear_p99(self):
        rows=[('10-21','14:00:00','0' if i<10 else '4',float(i),str(i),'') for i in range(20)]
        result=ad.statistics(rows,{},0,19)
        self.assertEqual(result['mean_0'],'3.5')
        self.assertEqual(result['P99_0'],'6.93')
        self.assertEqual(result['mean_4'],'14.5')
        self.assertEqual(result['P99_4'],'16.95')
        self.assertEqual(result['mean_1'],'-')

    def test_threshold_empty_and_missing_diagnostics(self):
        rows=[('10-21','14:00:00','0',float(i),str(i),'') for i in range(20)]
        result=ad.statistics(rows,{'max_threshold':3},0,19)
        self.assertEqual(result['mean_0'],'1.5')
        result=ad.statistics(rows,{'min_threshold':100},0,19)
        self.assertIn('阈值过滤后无数据',result['diagnostic'])
        self.assertEqual(result['P99'],'-')
        self.assertIn('未找到源日志文件',ad.statistics([],{},0,19,False)['diagnostic'])
        self.assertIn('日志标签未命中',ad.statistics([],{},0,19)['diagnostic'])

    def test_timeout_stops_without_partial_success(self):
        self.fixture()
        with self.assertRaises(TimeoutError):ad.analyze(self.root,META,rules(),deadline=time.monotonic()-1)

if __name__=='__main__':unittest.main()
