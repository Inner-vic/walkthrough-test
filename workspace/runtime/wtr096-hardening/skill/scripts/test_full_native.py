"""Full-statistics inventory and hostile archive tests; all fixtures synthetic."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, tzinfo
import io
import json
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import types
import unittest
import uuid
import zipfile
from unittest.mock import patch

import m100_attachment_worker as worker
import run_m100 as runner
import test_m100_local_report as legacy
from test_m100_local_report import records, rules_fixture


class NativeReport:
    """Synthetic native formatting boundary, including native omission semantics."""
    def _format_report_data(self,data):
        result={}
        for key,value in data.items():
            name=key if isinstance(key,str) else key.stem.split('+')[1]
            if value.get('empty'):
                continue
            if name not in result:
                result[name]={k:v for k,v in value.items() if k!='empty'}
        return result

    def _should_skip_emb_type_variable(self,name,data):
        return name=='pnc_to_wcu_receive_delay' and data.get('emb_type',{}).get('mean')!='自研EMB'

    def _get_emb_type_str(self,data):
        return data.get('emb_type',{}).get('mean','')


class FullTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.gettempdir())/('walkthrough-full-'+uuid.uuid4().hex[:12])
        self.root.mkdir()
        self.addCleanup(shutil.rmtree,self.root)

    def make_zip(self,name,members):
        path=self.root/name
        with zipfile.ZipFile(path,'w') as z:
            for key,value in members:
                z.writestr(key,value)
        return path

    def make_tar(self,name,members):
        path=self.root/name
        with tarfile.open(path,'w') as archive:
            for key,value in members:
                if isinstance(key,tarfile.TarInfo):
                    member=key
                else:
                    member=tarfile.TarInfo(key)
                member.size=len(value)
                archive.addfile(member,io.BytesIO(value))
        return path

    def test_full_inventory_retains_missing_native_omissions_and_fields(self):
        rules={'Tc397':{'emb_type':{'func':'emb_type'},
                        'pnc_to_wcu_receive_delay':{'calculate_type':'emb_type'},
                        'unavailable':{'soc_type':'XVM_'},'empty_stat':{'func':'custom'}},
               'other':{'delay':{'func':'multi_log_diff_value'}}}
        source={'Tc397':{'emb_type':{'mean':'博世EMB'},
                         'pnc_to_wcu_receive_delay':{'P99':123,'HGR':2,'custom_state':'native-value'},
                         'empty_stat':{'empty':True}},
                'other':{Path('any+delay.csv'):{'P99_0':19,'mean_1':'8.0','unit':'ms','image':'private'}}}
        original=copy.deepcopy(source)
        result=worker.capture_full_statistics(NativeReport(),source,rules)
        self.assertEqual(len(result),5)
        indexed={r['metric']:r for r in result}
        self.assertEqual(indexed['pnc_to_wcu_receive_delay']['statistics']['P99'],123)
        self.assertEqual(indexed['pnc_to_wcu_receive_delay']['status'],'conditional_skip')
        self.assertIn('博世EMB',indexed['pnc_to_wcu_receive_delay']['skip_reason'])
        self.assertFalse(indexed['pnc_to_wcu_receive_delay']['native_publish'])
        self.assertEqual(indexed['unavailable']['status'],'missing')
        self.assertEqual(indexed['empty_stat']['status'],'missing')
        self.assertEqual(indexed['delay']['statistics'],{'P99_0':19,'mean_1':'8.0','unit':'ms'})
        self.assertEqual(source,original)
        self.assertEqual(worker.full_analysis_scope(rules)['source_metric_count'],5)

    def test_same_metric_in_distinct_themes_retained_with_native_owner(self):
        rules={'first':{'dup':{}},'second':{'dup':{}}}
        result=worker.capture_full_statistics(NativeReport(),{'first':{'dup':{'mean':1}},'second':{'dup':{'mean':2}}},rules)
        self.assertEqual([r['statistics']['mean'] for r in result],[1,2])
        self.assertEqual([r['native_publish'] for r in result],[False,True])
        self.assertTrue(result[0]['skip_reason'])

    def test_missing_non_target_changes_whole_report_to_partial(self):
        rows=records()+[{'theme':'Tc397','metric':'MCU_missing','statistics':{},'status':'missing','diagnostic':'missing log'}]
        result=worker.export_result(rows,{'vin':'DEM0VEH1CLE000001','time_range':'synthetic'},self.root/'report.html')
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['statistics_summary']['total'],5)

    def test_native_publication_methods_blocked(self):
        class Report(NativeReport):
            def summary_report(self,*args):
                raise AssertionError('publication')
            def theme_report(self,*args):
                raise AssertionError('publication')
            generate_report=theme_report
            generate_template=theme_report
        plot=types.SimpleNamespace(FeishuTemplateReport=Report)
        value=types.SimpleNamespace(load_config_from_feishu=lambda *a:None,update_last_valid_time_to_feishu=lambda *a:None)
        client=types.SimpleNamespace(request=lambda *a:(_ for _ in ()).throw(AssertionError('network')))
        config=types.SimpleNamespace(CONFIG_MAPS={'m100-ultra':{'summary':{}}})
        worker.install_hooks(plot,value,client,config,{'theme':{'metric':{}}})
        for name in ('theme_report','generate_report','generate_template'):
            with self.subTest(name=name),self.assertRaises(RuntimeError):
                getattr(Report(),name)({})
        for method in ('GET','POST','PUT','DELETE'):
            with self.subTest(method=method),self.assertRaises(RuntimeError):
                client.request(method,'https://example.test/open-apis/sheets/v2/test')

    def test_native_formatter_complete_values_are_not_recalculated(self):
        values={'mean':1.23456789,'P99':9.87654321,'P90_0':'5.00','mean_1':'-','HGR':42,
                'diagnostic':'无数据：某个状态没有记录','unfamiliar_column':'keep exactly'}
        result=worker.capture_full_statistics(NativeReport(),{'theme':{'m':values}},{'theme':{'m':{'ps_type':'ps_xvm'}}})
        self.assertEqual(result[0]['statistics'],values)
        self.assertEqual(result[0]['rule']['ps_type'],'ps_xvm')

    def test_zip_and_tar_support_safe_plain_logs(self):
        zip_path=self.make_zip('a.zip',[('nested/XVM_MCU_2026_10_21_16_00_00.log',b'abc')])
        tar_path=self.make_tar('b.tar',[('nested/FSDA_M100.log',b'def')])
        result=runner.validate_log_inventory([zip_path,tar_path])
        self.assertEqual(result['log_count'],2)
        self.assertEqual(result['expanded_bytes'],6)

    def test_same_basename_different_content_rejected_across_archives(self):
        a=self.make_zip('a.zip',[('one/XVM_MCU.log',b'abc')])
        b=self.make_tar('b.tar',[('two/XVM_MCU.log',b'different')])
        with self.assertRaisesRegex(ValueError,'同名日志内容不同'):
            runner.validate_log_inventory([a,b])

    def test_identical_basename_content_is_safe_to_deduplicate(self):
        a=self.make_zip('a.zip',[('one/XVM_MCU.log',b'abc')])
        b=self.make_tar('b.tar',[('two/XVM_MCU.log',b'abc')])
        self.assertEqual(runner.validate_log_inventory([a,b])['log_count'],1)

    def test_plain_vs_archived_case_collision_rejected(self):
        a=self.root/'XVM_MCU.log';a.write_bytes(b'first')
        b=self.make_zip('b.zip',[('xvm_mcu.log',b'second')])
        with self.assertRaisesRegex(ValueError,'同名日志内容不同'):
            runner.validate_log_inventory([a,b])

    def test_zip_and_tar_traversal_rejected(self):
        for index,name in enumerate(('../escape.log','/absolute.log','C:/escape.log','safe/../../escape.log','safe\\..\\escape.log')):
            with self.subTest(name=name):
                for path in (self.make_zip(str(index)+'.zip',[(name,b'x')]),self.make_tar(str(index)+'.tar',[(name,b'x')])):
                    with self.assertRaises(ValueError):runner.validate_log_inventory([path])

    def test_archive_links_and_devices_rejected(self):
        z=zipfile.ZipInfo('link.log');z.create_system=3;z.external_attr=(stat.S_IFLNK|0o777)<<16
        paths=[self.make_zip('link.zip',[(z,b'elsewhere')])]
        for index,kind in enumerate((tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.CHRTYPE,tarfile.FIFOTYPE)):
            t=tarfile.TarInfo('link.log');t.type=kind;t.linkname='elsewhere'
            paths.append(self.make_tar(str(index)+'.tar',[(t,b'')]))
        for path in paths:
            with self.subTest(path=path),self.assertRaises(ValueError):runner.validate_log_inventory([path])

    def test_nested_unsupported_and_binary_inputs_fail_closed(self):
        for index,name in enumerate(('nested.clp','nested.clp.zst','nested.zip','nested.tar','nested.gz','MCU.bin','message','XVM.LOG')):
            path=self.make_zip(str(index)+'.zip',[(name,b'x')])
            with self.subTest(name=name),self.assertRaises(ValueError):runner.validate_log_inventory([path])
        for name in ('raw.clp','raw.zst','raw.gz'):
            path=self.root/name;path.write_bytes(b'x')
            with self.subTest(name=name),self.assertRaises(ValueError):runner.validate_log_inventory([path])

    def test_duplicate_archive_target_rejected(self):
        path=self.make_zip('duplicate.zip',[('x/a.log',b'1'),('x/A.log',b'1')])
        with self.assertRaisesRegex(ValueError,'重复目标路径'):
            runner.validate_log_inventory([path])

    def test_linux_colon_and_dot_prefix_are_not_traversal(self):
        path=self.make_zip('safe.zip',[('./nested/XVM:MCU.log',b'valid')])
        self.assertEqual(runner.validate_log_inventory([path])['log_count'],1)

    def native_selection(self):
        """Load synthetic native selector source through the same AST boundary."""
        source=self.root/'selection-source'
        (source/'common').mkdir(parents=True)
        (source/'general_performance').mkdir()
        (source/'general_performance/config.py').write_text("CONFIG_PATH={'FULL_ALIAS':['mach_','additional_']}\n")
        (source/'common/perf_utils.py').write_text(
            "_TS_RE=re.compile(r'(\\d{4}_\\d{2}_\\d{2}_\\d{2}_\\d{2}_\\d{2})')\n"
            "def _parse_cn_dt_from_log_path(path):\n"
            " found=list(_TS_RE.finditer(path.name))\n"
            " return CN_TZ.localize(datetime.strptime(found[-1].group(1),'%Y_%m_%d_%H_%M_%S')) if found else None\n"
            "def should_keep_log(path,time_range,state):\n"
            " stamp=_parse_cn_dt_from_log_path(path)\n"
            " if stamp is None:return True\n"
            " if stamp.timestamp()<time_range[0]:return False\n"
            " if stamp.timestamp()>time_range[1]:\n"
            "  if state['kept_first_after_end']:return False\n"
            "  state['kept_first_after_end']=True\n"
            " return True\n")
        (source/'common/perf_path.py').write_text(
            "def is_file_time_in_range(filename,time_range):\n"
            " m=re.search(r'(\\d{4}_\\d{2}_\\d{2}_\\d{2}_\\d{2}_\\d{2})',filename)\n"
            " if not m:raise ValueError('no archive date')\n"
            " start,end=[datetime.strptime(t,'%Y%m%d%H%M%S') for t in time_range.split('/')]\n"
            " return start<=datetime.strptime(m.group(1),'%Y_%m_%d_%H_%M_%S')<=end+timedelta(hours=1)\n")
        class ChinaTZ(tzinfo):
            def utcoffset(self,value):return timedelta(hours=8)
            def dst(self,value):return timedelta(0)
            def localize(self,value):return value.replace(tzinfo=self)
        rules={'main':{'one':{'soc_type':'FULL_ALIAS'}},'new_theme':{'two':{'soc_type':'third_'}}}
        with patch.dict('sys.modules',{'pytz':types.SimpleNamespace(timezone=lambda name:ChinaTZ())}):
            return runner.NativeLogSelection(source,rules,'20261021145000','20261021152400')

    def test_out_of_window_conflict_excluded_only_after_native_full_union(self):
        selection=self.native_selection()
        first=self.make_zip('first_2026_10_21_15_25_00.zip',[('mach_2026_10_21_15_25_00.log',b'boundary')])
        a=self.make_zip('a_2026_10_21_15_38_50.zip',[('mach_2026_10_21_15_38_50.log',b'one')])
        b=self.make_zip('b_2026_10_21_15_38_50.zip',[('mach_2026_10_21_15_38_50.log',b'two')])
        result=runner.validate_log_inventory([first,a,b],selection)
        self.assertEqual(result['selected_log_count'],1)
        self.assertEqual(len(result['ignored_conflicts']),1)
        self.assertEqual(len(result['ignored_conflicts'][0]['variants']),2)

    def test_first_after_end_conflict_is_still_consumed_and_rejected(self):
        selection=self.native_selection()
        a=self.make_zip('a_2026_10_21_15_38_50.zip',[('mach_2026_10_21_15_38_50.log',b'one')])
        b=self.make_zip('b_2026_10_21_15_38_50.zip',[('mach_2026_10_21_15_38_50.log',b'two')])
        with self.assertRaisesRegex(ValueError,'实际选集'):
            runner.validate_log_inventory([a,b],selection)

    def test_rules_and_config_aliases_cover_more_than_four_targets(self):
        selection=self.native_selection()
        for prefix in ('additional_','third_'):
            a=self.make_zip(prefix+'a_2026_10_21_15_10_00.zip',[(prefix+'2026_10_21_15_10_00.log',b'one')])
            b=self.make_zip(prefix+'b_2026_10_21_15_10_00.zip',[(prefix+'2026_10_21_15_10_00.log',b'two')])
            with self.subTest(prefix=prefix),self.assertRaisesRegex(ValueError,'实际选集'):
                runner.validate_log_inventory([a,b],selection)

    def test_archive_time_filter_precedes_native_file_selector(self):
        selection=self.native_selection()
        a=self.make_zip('a_2026_10_21_15_10_00.zip',[('mach_2026_10_21_15_10_00.log',b'one')])
        b=self.make_zip('b_2026_10_21_16_25_00.zip',[('mach_2026_10_21_15_10_00.log',b'two')])
        result=runner.validate_log_inventory([a,b],selection)
        self.assertEqual(result['selected_log_count'],1)
        self.assertEqual(result['ignored_conflicts'],[])

    def test_unparseable_log_timestamp_stays_consumable(self):
        selection=self.native_selection()
        a=self.make_zip('a_2026_10_21_15_10_00.zip',[('mach_unknown.log',b'one')])
        b=self.make_zip('b_2026_10_21_15_11_00.zip',[('mach_unknown.log',b'two')])
        with self.assertRaisesRegex(ValueError,'实际选集'):
            runner.validate_log_inventory([a,b],selection)

    def test_same_second_boundary_candidates_all_retain_unique_content_and_source(self):
        selection=self.native_selection()
        rows=[
            ('log_fsdA','mach_A_inside_2026_10_21_15_10_00.log',b'inside'),
            ('log_fsdA','mach_A_first_2026_10_21_15_25_00.log',b'A-first'),
            ('log_fsdA','mach_A_second_2026_10_21_15_25_00.log',b'A-second'),
            ('log_fsdA','mach_A_later_2026_10_21_15_26_00.log',b'A-later'),
            ('log_fsdB','mach_B_first_2026_10_21_15_25_00.log',b'B-first'),
            ('log_fsdB','mach_B_second_2026_10_21_15_25_00.log',b'B-second'),
        ]
        files=[]
        for source,name,payload in rows:
            path=self.root/source/name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(payload)
            files.append(path)
        inventory=runner.validate_log_inventory(files,selection,input_root=self.root)
        candidates={name for _,name,_ in rows if '_later_' not in name}
        self.assertEqual(set(inventory['selected_members']),candidates)
        self.assertEqual(inventory['selected_log_count'],5)
        for source,name,payload in rows:
            if name not in candidates:
                continue
            member=inventory['selected_members'][name]
            self.assertEqual(member['source'],source)
            self.assertEqual(member['bytes'],len(payload))
            self.assertEqual(inventory['basename_hashes'][name.casefold()],[(member['sha256'],len(payload))])

    def test_full_rule_mode_does_not_accept_unapproved_source(self):
        helper=legacy.Tests(methodName='test_unapproved_source_is_rejected_before_process_or_publication')
        helper.root=self.root
        source,logs,rules,original_ini=helper.synthetic_source()
        full=rules_fixture();full['Tc397']={'emb_type':{'func':'emb_type','soc_type':'XVM_'}}
        rules.write_text(json.dumps(full))
        start=source/'general_performance/start.py'
        text=start.read_text()
        text=text.replace("'synthetic':{", "'Tc397':{'emb_type':{'mean':'自研EMB'}},'synthetic':{",1)
        start.write_text(text,encoding='utf-8')
        with self.assertRaises(ValueError):
            runner.preflight(source,logs,'20260407150000','20260407160000','m100-ultra',rules)
        self.assertFalse((self.root/'jobs').exists())
        self.assertEqual((source/'general_performance/o.ini').read_text(),original_ini)


if __name__=='__main__':
    unittest.main()
