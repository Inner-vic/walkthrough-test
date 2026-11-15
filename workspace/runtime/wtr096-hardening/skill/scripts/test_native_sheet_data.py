"""Synthetic no-network tests of native capture, completeness and rendering."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import types
import unittest
import uuid

import m100_attachment_worker as worker
import native_sheet_data as data
from native_sheet_test_fixture import synthetic_native_sheet_payload, synthetic_renderer_bundle

REQUEST={'vin':'DEM0VEH1CLE000001','start':'2026-01-01T10:00:00','end':'2026-01-01T10:30:00'}
FILE_SHA='a'*64


def fixture():
    rules={'vla-arch':{name:{'func':'custom','ps_type':'ps_time','calculate_type':''}
        for name in ('header_delay','VlaModel_FPS','missing','unmapped','emb_skip')}}
    rows=[]
    for name,values in [('header_delay',{'P99_0':'123.45','P99_4':'-'}),
                        ('VlaModel_FPS',{'mean_0':'5.02','mean_4':'-'}),
                        ('missing',{'diagnostic':'无数据：未找到源日志文件'}),
                        ('unmapped',{'mean':'99'}),('emb_skip',{'mean':'8'})]:
        rows.append({'theme':'vla-arch','metric':name,'rule':copy.deepcopy(rules['vla-arch'][name]),
            'statistics':values,'native_publish':name!='emb_skip',
            'status':'conditional_skip' if name=='emb_skip' else ('missing' if name=='missing' else 'available'),
            'diagnostic':values.get('diagnostic',''),'skip_reason':'native EMB gate' if name=='emb_skip' else ''})
    payload=synthetic_native_sheet_payload({'statistics':rows},rules,REQUEST,FILE_SHA)
    return payload,rules,rows


def templates():
    sheets=[]
    for index in range(7):
        values=[[None]*14 for _ in range(3)]
        values[0][0]='&title'
        values[0][2]='=SUM(D2:D3)'
        values[0][5]=' '
        if index==0:
            values[1][0]='&header_delay@P99_0'
            values[1][3]='&header_delay@image'
            values[2][0]='&missing@image'
            values[2][3]='&emb_skip@mean'
            values[2][4]='&unknown@image'
        if index==1:
            values[1][0]='&VlaModel_FPS@mean_0'
            values[1][3]='&VlaModel_FPS@image'
        sheets.append({'sheet_id':f's{index}','sheet_name':f'Synthetic {index}',
            'row_count':3,'column_count':15,
            'keymap':{'valueRanges':[{'range':f'old{index}!B1:O3','values':values}]}})
    return sheets


class NativeSheetDataTests(unittest.TestCase):
    def test_original_renderer_keeps_formulas_and_diagnostic_but_never_images(self):
        payload,_,_=fixture();sheets=templates();before=copy.deepcopy(sheets)
        plan=data.build_native_sheet_plan(payload,sheets)
        self.assertEqual(sheets,before)
        self.assertEqual(plan['sheets'][0]['values'][1][0],'123.45')
        self.assertEqual(plan['sheets'][1]['values'][1][0],'5.02')
        self.assertEqual(plan['sheets'][0]['values'][0][2],{'type':'formula','text':'=SUM(D2:D3)'})
        self.assertIsNone(plan['sheets'][0]['values'][1][3])
        self.assertEqual(plan['sheets'][0]['values'][2][0],'无数据：未找到源日志文件')
        self.assertIsNone(plan['sheets'][0]['values'][2][4])
        self.assertEqual(plan['sheets'][0]['values'][2][3],'-')
        self.assertEqual(plan['sheets'][0]['image_cells_to_clear'],['s0!E2:E2','s0!B3:B3','s0!F3:F3'])
        self.assertTrue(all(not x['image_uploads'] for x in plan['sheets']))
        self.assertEqual(plan['complete_statistics_count'],5)
        self.assertEqual([x['metric'] for x in plan['unrepresented_metrics']],['unmapped'])
        self.assertFalse(plan['template_represents_every_rule'])

    def test_each_role_and_each_page_uses_independent_template_copy(self):
        first,_,_=fixture();second=copy.deepcopy(first)
        second['rd']['title']='Other synthetic role'
        second=data.build_native_sheet_data(second['rd'],second['statistics'],second['rules'],second['metadata'],second['renderer_bundle'],second['native_template_reference'])
        sheets=templates()
        a=data.build_native_sheet_plan(first,sheets);b=data.build_native_sheet_plan(second,sheets)
        self.assertEqual(a['sheets'][0]['values'][0][0],'Synthetic native performance')
        self.assertEqual(b['sheets'][0]['values'][0][0],'Other synthetic role')
        self.assertEqual(sheets[0]['keymap']['valueRanges'][0]['values'][0][0],'&title')

    def test_metadata_binding_preserves_statistics_and_adds_context(self):
        payload,_,_=fixture();old=copy.deepcopy(payload)
        bound=data.bind_native_sheet_metadata(payload,{'request':REQUEST,'environment':'prod','role':'baseline',
            'rules_sha256':FILE_SHA,'request_sha256':data.canonical_sha256(REQUEST)},
            {'pcm_result':{'p99_ms':17},'source_hashes':{'a':'b'},'product_version':'synthetic'})
        self.assertEqual(payload,old)
        self.assertEqual(bound['rd'],old['rd'])
        self.assertEqual(bound['statistics'],old['statistics'])
        self.assertEqual(bound['rules_sha256'],old['rules_sha256'])
        self.assertEqual(bound['metadata']['rules_sha256'],FILE_SHA)
        self.assertNotEqual(bound['content_sha256'],old['content_sha256'])
        self.assertIn('delivery_context',bound)
        self.assertIs(data.validate_native_sheet_data(bound),bound)

    def test_metadata_cannot_cross_vin_window_environment_or_digest(self):
        payload,_,_=fixture()
        for updates in [
            {'request':{**REQUEST,'vin':'DEM0VEH1CLE000002'},'environment':'testtwo','rules_sha256':FILE_SHA},
            {'request':{**REQUEST,'end':'2026-01-01T10:31:00'},'environment':'testtwo','rules_sha256':FILE_SHA},
            {'request':REQUEST,'environment':'unknown','rules_sha256':FILE_SHA},
            {'request':REQUEST,'environment':'testtwo','rules_sha256':'invalid'},
            {'request':REQUEST,'environment':'testtwo','rules_sha256':FILE_SHA,'request_sha256':'0'*64},
        ]:
            with self.subTest(updates=updates),self.assertRaises(data.NativeSheetDataError):
                data.bind_native_sheet_metadata(payload,updates)

    def test_mutated_native_value_rule_and_context_are_detected(self):
        payload,_,_=fixture()
        for field in ('rd','statistics','rules','metadata'):
            bad=copy.deepcopy(payload)
            if field=='rd':bad[field]['header_delay']['P99_0']='999'
            elif field=='statistics':bad[field][0]['statistics']['P99_0']='999'
            elif field=='rules':bad[field]['vla-arch']['header_delay']['input']='changed'
            else:bad[field]['vin']='changed'
            with self.subTest(field=field),self.assertRaises(data.NativeSheetDataError):data.validate_native_sheet_data(bad)
        bad=copy.deepcopy(payload);bad['delivery_context']={'unknown':'mutation'}
        with self.assertRaises(data.NativeSheetDataError):data.validate_native_sheet_data(bad)

    def test_final_source_annotations_are_retained_without_changing_native_statistics(self):
        payload,_,_=fixture();rows=copy.deepcopy(payload['statistics'])
        rows[0]['source_quality']={'status':'partial','issues':[{'kind':'incomplete_frame'}]}
        rows[0]['source_quality_diagnostic']='Synthetic partial source'
        bound=data.bind_native_sheet_metadata(payload,{'request':REQUEST,'environment':'testtwo','rules_sha256':FILE_SHA},statistics=rows)
        self.assertEqual(bound['statistics'][0]['statistics'],payload['statistics'][0]['statistics'])
        self.assertEqual(bound['statistics'][0]['source_quality']['status'],'partial')
        rows[0]['statistics']['P99_0']='999'
        with self.assertRaises(data.NativeSheetDataError):
            data.bind_native_sheet_metadata(payload,{'request':REQUEST,'environment':'testtwo','rules_sha256':FILE_SHA},statistics=rows)

    def test_dynamic_inventory_has_no_fixed_201_assumption_but_may_not_drop_rule(self):
        payload,rules,rows=fixture()
        self.assertEqual(len(data.validate_native_sheet_data(payload)['statistics']),5)
        for changed in (rows[:-1],rows+[copy.deepcopy(rows[0])]):
            with self.assertRaises(data.NativeSheetDataError):
                data.build_native_sheet_data(payload['rd'],changed,rules,payload['metadata'],payload['renderer_bundle'],payload['native_template_reference'])

    def test_renderer_source_is_pinned_and_arbitrary_code_never_executes(self):
        bundle=synthetic_renderer_bundle()
        bundle['methods']['update_values']['source']+='\nraise AssertionError("must not execute")'
        payload,rules,rows=fixture()
        with self.assertRaises(data.NativeSheetDataError):
            data.build_native_sheet_data(payload['rd'],rows,rules,payload['metadata'],bundle,payload['native_template_reference'])
        bundle=synthetic_renderer_bundle();bundle['methods']['parse_sheet_coordinate']['source']=bundle['methods']['parse_sheet_coordinate']['source'].replace('65','66')
        with self.assertRaises(data.NativeSheetDataError):
            data.build_native_sheet_data(payload['rd'],rows,rules,payload['metadata'],bundle,payload['native_template_reference'])

    def test_image_path_is_rejected_before_original_encoder_can_read_it(self):
        payload,rules,rows=fixture();rd=copy.deepcopy(payload['rd']);rd['header_delay']['image']='secret-file.png'
        with self.assertRaises(data.NativeSheetDataError):
            data.build_native_sheet_data(rd,rows,rules,payload['metadata'],payload['renderer_bundle'],payload['native_template_reference'])

    def test_template_partial_ranges_unknown_resources_and_duplicate_ids_rejected(self):
        payload,_,_=fixture()
        changes=[lambda s:s.pop(),lambda s:s[0].update(sheet_id='s1'),
            lambda s:s[0]['keymap']['valueRanges'][0].update(range='s0!C1:O3'),
            lambda s:s[0]['keymap']['valueRanges'][0]['values'].pop(),
            lambda s:s[0]['keymap']['valueRanges'][0]['values'][0].pop(),
            lambda s:s[0]['keymap']['valueRanges'][0]['values'][0].__setitem__(1,{'unknown':'resource'})]
        for change in changes:
            sheets=templates();change(sheets)
            with self.subTest(change=change),self.assertRaises(data.NativeSheetDataError):data.build_native_sheet_plan(payload,sheets)

    def test_portable_file_reference_hash_and_idempotent_write(self):
        payload,_,_=fixture()
        parent=Path(__file__).resolve().parent
        folder=parent/('_native-sheet-test-'+uuid.uuid4().hex)
        folder.mkdir()
        self.assertEqual(folder.resolve().parent,parent)
        try:
            path=Path(folder)/'native-sheet-data.json'
            ref=data.write_native_sheet_data(path,payload)
            self.assertEqual(ref['path'],path.name)
            self.assertEqual(ref['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(data.load_native_sheet_data(path,ref['sha256']),payload)
            self.assertEqual(data.write_native_sheet_data(path,payload),ref)
            with self.assertRaises(data.NativeSheetDataError):data.load_native_sheet_data(path,'0'*64)
            path.write_text('{}')
            with self.assertRaises(data.NativeSheetDataError):data.write_native_sheet_data(path,payload)
        finally:
            if folder.resolve().parent!=parent:raise AssertionError('Unsafe synthetic cleanup target')
            shutil.rmtree(folder)


class CaptureTests(unittest.TestCase):
    def install(self):
        class Report:
            package_version='native synthetic version'
            template_token=('synthetic-native-token','synthetic-native-id')
            client=object()
            access_token='prior-local-value'
            calls=0
            def _format_report_data(self,source):
                return copy.deepcopy(source)
            def _should_skip_emb_type_variable(self,key,source):
                return key=='emb_skip'
            def _get_emb_type_str(self,source):return 'synthetic EMB'
            def summary_report(self,themes,title):
                self.calls+=1
                flattened={}
                for values in themes.values():flattened.update(values)
                rd=self._format_report_data(flattened)
                rd={k:v for k,v in rd.items() if not self._should_skip_emb_type_variable(k,rd)}
                rd['title']=title;rd['version']=self.package_version
                self.access_token=self.client.get_tenant_access_token()
                return self.generate_report(*self.template_token,rd,title)
            def generate_report(self,*args):raise AssertionError('network publish')
            theme_report=generate_report
            generate_template=generate_report
        class Signal:
            def plot(self,*args):raise AssertionError('must not execute plotting')
            plot_two_columns=plot
        plot=types.SimpleNamespace(FeishuTemplateReport=Report,Plot_Singal=Signal)
        value=types.SimpleNamespace(load_config_from_feishu=lambda *a:None,update_last_valid_time_to_feishu=lambda *a:None)
        client=types.SimpleNamespace(request=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('network')))
        config=types.SimpleNamespace(CONFIG_MAPS={'m100-ultra':{'summary':{}}})
        rules={'theme':{'metric':{},'emb_skip':{}}};capture={}
        rows=worker.install_hooks(plot,value,client,config,rules,native_sheet_capture=capture)
        return Report,Signal,client,config,capture,rows

    def test_original_summary_reaches_capture_boundary_without_auth_or_publication(self):
        Report,Signal,client,config,capture,rows=self.install();report=Report();oldclient=report.client
        values={'theme':{'metric':{'mean':'7','image':None},'emb_skip':{'mean':'8','image':None}}}
        self.assertIsNone(report.summary_report(values,'actual native title'))
        self.assertEqual(report.calls,1)
        self.assertEqual(capture['rd'],{'metric':{'mean':'7','image':None},'title':'actual native title','version':'native synthetic version'})
        self.assertEqual(capture['native_template_reference'],{'token':'synthetic-native-token','sheet_id':'synthetic-native-id'})
        self.assertEqual(len(rows),2)
        self.assertFalse(rows[1]['native_publish'])
        self.assertIs(report.client,oldclient)
        self.assertEqual(report.access_token,'prior-local-value')
        self.assertTrue(config.CONFIG_MAPS['m100-ultra']['summary']['not_plot'])
        for method in (Signal().plot,Signal().plot_two_columns):
            with self.assertRaises(RuntimeError):method()
        for method in ('GET','POST','PUT'):
            with self.assertRaises(RuntimeError):client.request(method,'https://example.test/resource')
        with self.assertRaises(RuntimeError):report.generate_report('x','y',{},'title')

    def test_capture_rejects_graph_artifact_or_caught_graph_failure(self):
        for fields in ({'mean':'7','img_path':'unexpected.png'},{'mean':'7','diagnostic':'图片异常：绘图失败'}):
            Report,_,_,_,_,_=self.install()
            with self.subTest(fields=fields),self.assertRaises(RuntimeError):Report().summary_report({'theme':{'metric':fields}},'title')

    def test_summary_called_twice_cannot_mix_windows(self):
        Report,_,_,_,_,_=self.install();report=Report()
        report.summary_report({'theme':{'metric':{'mean':'7'}}},'title')
        with self.assertRaises(ValueError):report.summary_report({'theme':{'metric':{'mean':'9'}}},'other')


if __name__=='__main__':unittest.main()
