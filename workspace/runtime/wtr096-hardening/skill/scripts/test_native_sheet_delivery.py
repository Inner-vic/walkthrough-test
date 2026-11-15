"""Synthetic bot transport/state-machine tests. No network or real identifiers."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import native_sheet_delivery as delivery
from request_model import InputError

META={'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:48:00','end':'2026-10-21T16:22:00'}
OTHER={**META,'start':'2026-10-21T14:50:00','end':'2026-10-21T15:24:00'}


def fake_renderer():
    module=ModuleType('native_sheet_data');module.__file__=__file__
    def load(path,expected_sha256):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected_sha256
        data=json.loads(Path(path).read_text(encoding='utf-8'));data['content_sha256']=expected_sha256
        return data
    def plan(data,sheets):
        result=[]
        for sheet in sheets:
            original=sheet['keymap']['valueRanges'][0]
            values=copy.deepcopy(original['values'])
            images=[]
            for ri,row in enumerate(values):
                for ci,value in enumerate(row):
                    if value=='&title':row[ci]=data['title']
                    elif value=='&fps@mean':row[ci]=data['mean']
                    elif value=='&fps@image':row[ci]=None;images.append(f'{chr(ci+66)}{ri+1}')
                    elif value=='&missing@image':row[ci]='无可用数据：窗口内无样本';images.append(f'{chr(ci+66)}{ri+1}')
                    elif isinstance(value,str) and value.startswith('='):row[ci]={'type':'formula','text':value}
            result.append({'sheet_id':sheet['sheet_id'],'sheet_name':sheet['sheet_name'],
                'range':original['range'],'values':values,'image_cells_to_clear':images,
                'placeholders':[{'range':sheet['sheet_id']+'!D2:D2','placeholder':'&fps@mean','paths':[['fps','mean']]}]})
        return {'schema':'m100-native-sheet-plan-v1','native_data_sha256':data['content_sha256'],
            'sheets':result,'unrepresented_metrics':data.get('unrepresented_metrics',[]),'complete_statistics_count':len(data['statistics'])}
    module.load_native_sheet_data=load;module.build_native_sheet_plan=plan
    return module


class FakeLark:
    def __init__(self,root):
        self.root=root;self.calls=[];self.counts={};self.fail={};self.response_identity='bot'
        self.books={'templateSynthetic':self.template('templateSynthetic')}
        self.write_changes_style=False;self.truncated=False;self.copy_changes_layout=False
        self.coerce_numeric_values=False;self.numeric_noise=0
    @staticmethod
    def flag(args,flag):return args[args.index(flag)+1]
    @staticmethod
    def template(token):
        sheets=[]
        for index in range(7):
            info={'sheet_id':'sheet'+str(index),'sheet_name':'Native '+str(index),'index':index,
                'row_count':2,'column_count':20,'merged_cells_count':1,'is_hidden':False,
                'chart_count':0,'pivot_table_count':0,'float_image_count':0}
            style={'number_format':'General','font':{'bold':False}}
            rows=[[{'style':copy.deepcopy(style)} for _ in range(14)] for _ in range(2)]
            rows[0][0]['value']='&title';rows[0][1]['value']='fixed title'
            rows[1][0]['value']='literal 001';rows[1][2]['value']='&fps@mean'
            rows[1][3].update(value='&fps@image',rich_text=[{'type':'embed-image','image_token':'oldImage'}])
            rows[1][4].update(formula='=1+1',value='2');rows[1][5]['value']='&missing@image'
            sheets.append({'info':info,'cells':rows,'layout':{'sheet_id':info['sheet_id'],'row_count':2,
                'column_count':20,'merges':[{'range':'B1:C1'}],'row_heights':{'1':30},'col_widths':{'B':90},
                'hidden_rows':[],'hidden_cols':[],'groups':[],'frozen_row_count':1,'frozen_col_count':0}})
        return {'token':token,'sheets':sheets}
    def result(self,name,effect):
        self.counts[name]=self.counts.get(name,0)+1;failure=self.fail.pop(name,None)
        if failure=='before':raise InputError('synthetic permission denied')
        data=effect()
        if failure=='after':raise TimeoutError('synthetic response lost')
        if failure=='malformed':data={}
        return {'ok':True,'identity':self.response_identity,'data':data}
    def __call__(self,settings,args,*,profile,timeout=150):
        assert profile=='report-bot' and self.flag(args,'--as')=='bot'
        self.calls.append(list(args))
        if args[:2]==['drive','+copy']:
            def effect():
                source=self.flag(args,'--token');token='copy'+str(len(self.books));book=copy.deepcopy(self.books[source])
                book['token']=token;self.books[token]=book
                if self.copy_changes_layout:book['sheets'][0]['layout']['row_heights']['1']=99
                return {'copied':True,'file_token':token,'file_type':'sheet','source_type':'sheet',
                    'source_file_token':source,'name':self.flag(args,'--name'),'url':'https://example.feishu.cn/sheets/'+token}
            return self.result('copy',effect)
        token=self.flag(args,'--spreadsheet-token');book=self.books[token]
        if args[:2]==['sheets','+workbook-info']:
            return self.result('info',lambda:{'token':token,'sheets':[copy.deepcopy(x['info']) for x in book['sheets']]})
        if args[:2]==['sheets','+sheet-info']:
            sheet=next(s for s in book['sheets'] if s['info']['sheet_id']==self.flag(args,'--sheet-id'))
            return self.result('layout',lambda:copy.deepcopy(sheet['layout']))
        if args[:2]==['sheets','+cells-get']:
            def get():
                sheet=next(s for s in book['sheets'] if s['info']['sheet_id']==self.flag(args,'--sheet-id'))
                relative=self.flag(args,'--output-path');assert relative.startswith('./')
                path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
                value={'has_more':False,'ranges':[{'actual_range':'B1:O2','row_indices':[1,2],
                    'col_indices':list('BCDEFGHIJKLMNO'),'cells':copy.deepcopy(sheet['cells'])}]}
                path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
                return {'complete':not self.truncated,'output_path':relative}
            return self.result('get',get)
        if args[:2]==['sheets','+cells-set']:
            path=self.flag(args,'--writes');assert path.startswith('@./')
            writes=json.loads((self.root/path[1:]).read_text(encoding='utf-8'))
            failure=self.fail.get('write')
            def put():
                for count,write in enumerate(writes):
                    sheet=next(s for s in book['sheets'] if s['info']['sheet_id']==write['sheet_id'])
                    assert write['range']=='B1:O2'
                    for ri,row in enumerate(write['cells']):
                        for ci,cell in enumerate(row):
                            assert not {'cell_styles','style','rich_text'}&set(cell)
                            if not cell:continue
                            target=sheet['cells'][ri][ci]
                            for key in ('value','formula','rich_text'):target.pop(key,None)
                            target.update(copy.deepcopy(cell))
                            if self.coerce_numeric_values and isinstance(target.get('value'),str):
                                try:target['value']=float(target['value'])+self.numeric_noise
                                except ValueError:pass
                    if self.write_changes_style:sheet['cells'][0][0]['style']['number_format']='@'
                    if failure=='partial' and count==0:raise TimeoutError('partial batched write')
                return {'formula_errors':[]}
            return self.result('write',put)
        raise AssertionError(args)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        runtime=self.root/'runtime';runtime.mkdir();self.path=runtime/'task.json'
        self.settings={'workspace_root':str(self.root),'runtime_state_dir':str(runtime),'report_lark_profile':'report-bot',
            'lark_profile':'query-user','expected_lark_app_id':'appSynthetic','lark_home':str(self.root/'identity'),
            'report_delivery':{'format':'native_sheets','include_images':False}}
        request={'vin':META['vin'],'baseline_metadata':META,'replacement_metadata':OTHER}
        self.record={'request':request,'request_sha256':delivery.request_digest(request),'vin':META['vin'],
            'job_key':'syntheticJob','sender_id':'ou_syntheticOwner','source_chat_id':'oc_syntheticChat',
            'source_message_id':'om_syntheticMessage','report_secure_label_required':True,
            'report_permissions':{'main_docx_sentinel':True}}
        self.result={'vin':META['vin'],'request_sha256':self.record['request_sha256'],
            'pipeline':{'windows':{}},'native_sheet_data':{}}
        for role,meta in [('baseline',META),('replacement',OTHER)]:
            rules='a'*64;work=self.path.with_suffix('.work')/role;work.mkdir(parents=True)
            path=work/'native-sheet-data.json'
            data={'schema':delivery.DATA_SCHEMA,'metadata':{'role':role,'request':meta,'environment':'testtwo','rules_sha256':rules},
                'native_template_reference':{'token':'templateSynthetic','sheet_id':'sheet0'},
                'title':'Native '+role,'mean':'10.0003','statistics':[{'metric':'fps'}],'rd':{'fps':{'mean':10.0003}},
                'rules':{'synthetic':{'fps':{'ps_type':'ps_time'}}}}
            path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            self.result['native_sheet_data'][role]={'role':role,'request':meta,'request_sha256':delivery.request_digest(meta),
                'environment':'testtwo','rules_sha256':rules,'path':str(path),'sha256':delivery._digest(path),
                'bytes':path.stat().st_size,'schema':delivery.DATA_SCHEMA}
            self.result['pipeline']['windows'][role]={'request':meta,'environment':'testtwo','rules_sha256':rules}
        self.path.write_text(json.dumps(self.record),encoding='utf-8');self.fake=FakeLark(self.root)
        self.governance=[]
        def govern(name):
            def call(settings,url,*args):self.governance.append((name,url))
            return call
        def label(settings,path,record,url,permissions,*,persist_callback):
            permissions['secure_label']={'id':'syntheticL3','name':'L3'};persist_callback()
        patches=[patch.object(delivery.policy,'WORKSPACE_ROOT',self.root),patch.object(delivery.policy,'run_lark',self.fake),
            patch.object(delivery.policy,'ensure_report_bot_authorization',return_value={'profile':'report-bot','available':True}),
            patch.object(delivery.policy,'restrict_collaborator_management',side_effect=govern('restrict')),
            patch.object(delivery.policy,'grant_source_chat_view',side_effect=govern('chat')),
            patch.object(delivery.policy,'transfer_report_owner',side_effect=govern('owner')),
            patch.object(delivery.policy,'configure_report_secure_label',side_effect=label),
            patch.dict(sys.modules,{'native_sheet_data':fake_renderer()})]
        for item in patches:item.start();self.addCleanup(item.stop)
    def run_delivery(self):return delivery.ensure_native_sheets(self.settings,self.path,self.record,self.result)
    def change_data(self,role,change):
        item=self.result['native_sheet_data'][role];path=Path(item['path']);data=json.loads(path.read_text())
        change(data);path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        item.update(sha256=delivery._digest(path),bytes=path.stat().st_size)
    def test_two_native_copies_full_readback_no_images_styles_or_excel(self):
        result=self.run_delivery();self.assertEqual(set(result),set(delivery.ROLES))
        self.assertNotEqual(result['baseline']['token'],result['replacement']['token'])
        self.assertEqual(self.fake.counts['copy'],2);self.assertEqual(self.fake.counts['write'],2)
        self.assertEqual(self.record['report_permissions'],{'main_docx_sentinel':True})
        for role,item in result.items():
            self.assertEqual(item['validation']['status'],'ok');self.assertEqual(item['validation']['verified_cells'],196)
            self.assertEqual(item['permissions']['secure_label']['name'],'L3')
            for sheet in self.fake.books[item['token']]['sheets']:
                self.assertEqual(sheet['cells'][1][3]['value'],'')
                self.assertEqual(sheet['cells'][1][5]['value'],'无可用数据：窗口内无样本')
                self.assertFalse(delivery._has_image(sheet['cells'][1][3]))
                self.assertEqual(sheet['cells'][1][2]['value'],'10.0003')
                self.assertEqual(sheet['cells'][1][4]['formula'],'=1+1')
        self.assertEqual(len(self.governance),6)
        self.assertFalse(list(self.root.rglob('*.xlsx')))
    def test_retry_reads_existing_tables_never_creates_or_writes_twice(self):
        first=self.run_delivery();counts=copy.deepcopy(self.fake.counts);self.governance.clear()
        second=self.run_delivery();self.assertEqual(first,second)
        self.assertEqual(self.fake.counts['copy'],counts['copy']);self.assertEqual(self.fake.counts['write'],counts['write'])
        self.assertEqual(self.governance,[])
    def test_unknown_copy_response_is_not_repeated(self):
        self.fake.fail['copy']='after'
        with self.assertRaises(TimeoutError):self.run_delivery()
        self.assertTrue(self.record['native_sheets']['roles']['baseline']['copy_pending'])
        with self.assertRaisesRegex(InputError,'禁止自动重复'):self.run_delivery()
        self.assertEqual(self.fake.counts['copy'],1)
    def test_malformed_copy_receipt_is_not_repeated(self):
        self.fake.fail['copy']='malformed'
        with self.assertRaisesRegex(InputError,'复制回执'):self.run_delivery()
        with self.assertRaisesRegex(InputError,'复制回执'):self.run_delivery()
        self.assertEqual(self.fake.counts['copy'],1)
    def test_saved_valid_receipt_recovers_without_recopied_file(self):
        first=self.run_delivery();entry=self.record['native_sheets']['roles']['baseline']
        entry.pop('token');entry.pop('url');entry['copy_pending']=True
        second=self.run_delivery();self.assertEqual(first['baseline']['token'],second['baseline']['token'])
        self.assertEqual(self.fake.counts['copy'],2)
    def test_lost_write_response_is_resolved_by_readback(self):
        self.fake.fail['write']='after'
        with self.assertRaises(TimeoutError):self.run_delivery()
        result=self.run_delivery();self.assertEqual(result['baseline']['validation']['status'],'ok')
        self.assertEqual(self.fake.counts['copy'],2);self.assertEqual(self.fake.counts['write'],2)
    def test_partial_write_replays_same_plan_only(self):
        self.fake.fail['write']='partial'
        with self.assertRaises(TimeoutError):self.run_delivery()
        result=self.run_delivery();self.assertEqual(result['replacement']['validation']['status'],'ok')
        self.assertEqual(self.fake.counts['copy'],2);self.assertEqual(self.fake.counts['write'],3)
    def test_unknown_value_during_pending_write_is_not_overwritten(self):
        self.fake.fail['write']='after'
        with self.assertRaises(TimeoutError):self.run_delivery()
        token=self.record['native_sheets']['roles']['baseline']['token']
        self.fake.books[token]['sheets'][0]['cells'][1][2]['value']='manual unknown value'
        with self.assertRaisesRegex(InputError,'人工改动'):self.run_delivery()
        self.assertEqual(self.fake.counts['write'],1)
    def test_ready_table_mutation_is_reported_without_overwrite(self):
        result=self.run_delivery();self.fake.books[result['baseline']['token']]['sheets'][0]['cells'][1][2]['value']='changed'
        with self.assertRaisesRegex(InputError,'内容变化'):self.run_delivery()
        self.assertEqual(self.fake.counts['write'],2)
    def test_source_data_change_between_retry_is_rejected(self):
        self.run_delivery();self.change_data('baseline',lambda data:data.update(mean='99'))
        with self.assertRaisesRegex(InputError,'断点与'):self.run_delivery()
        self.assertEqual(self.fake.counts['copy'],2)
    def test_cross_role_reference_swap_is_rejected_before_remote_call(self):
        self.result['native_sheet_data']['baseline'],self.result['native_sheet_data']['replacement']=self.result['native_sheet_data']['replacement'],self.result['native_sheet_data']['baseline']
        with self.assertRaisesRegex(InputError,'角色'):self.run_delivery()
        self.assertFalse(self.fake.calls)
    def test_rule_or_environment_binding_change_rejected(self):
        self.result['native_sheet_data']['baseline']['rules_sha256']='b'*64
        with self.assertRaisesRegex(InputError,'规则'):self.run_delivery()
        self.assertFalse(self.fake.calls)
    def test_internal_metadata_cannot_be_substituted(self):
        self.change_data('baseline',lambda data:data['metadata'].update(request=OTHER))
        with self.assertRaisesRegex(InputError,'内部来源'):self.run_delivery()
        self.assertFalse(self.fake.calls)
    def test_unmapped_rules_remain_explicit_without_blocking_native_mapping(self):
        self.change_data('baseline',lambda data:data.update(unrepresented_metrics=['unmapped_metric']))
        result=self.run_delivery();self.assertEqual(result['baseline']['validation']['unrepresented_metrics'],['unmapped_metric'])
        self.assertFalse(result['baseline']['validation']['template_represents_every_rule'])
        self.assertTrue(result['baseline']['validation']['complete_local_statistics_retained'])
        self.assertEqual(self.fake.counts['copy'],2)
    def test_override_must_be_exact_template_compatible(self):
        self.settings['report_delivery']['native_template_spreadsheet_token']='otherTemplate'
        self.fake.books['otherTemplate']=self.fake.template('otherTemplate')
        self.fake.books['otherTemplate']['sheets'][0]['cells'][0][1]['value']='old report sample'
        with self.assertRaisesRegex(InputError,'不兼容'):self.run_delivery()
        self.assertNotIn('copy',self.fake.counts)
    def test_truncated_template_is_not_used(self):
        self.fake.truncated=True
        with self.assertRaisesRegex(InputError,'读取未完成'):self.run_delivery()
        self.assertNotIn('copy',self.fake.counts)
    def test_wrong_identity_never_creates(self):
        self.fake.response_identity='user'
        with self.assertRaisesRegex(InputError,'Bot身份|Bot 身份'):self.run_delivery()
        self.assertNotIn('copy',self.fake.counts)
    def test_copy_geometry_mismatch_stops_before_values(self):
        self.fake.copy_changes_layout=True
        with self.assertRaisesRegex(InputError,'没有保留'):self.run_delivery()
        self.assertNotIn('write',self.fake.counts)
    def test_write_style_mutation_fails_verification(self):
        self.fake.write_changes_style=True
        with self.assertRaisesRegex(InputError,'单元格格式'):self.run_delivery()
        self.assertNotEqual(self.record['native_sheets']['roles']['baseline']['status'],'ready')
    def test_unknown_owner_transfer_is_not_repeated(self):
        delivery.policy.transfer_report_owner.side_effect=TimeoutError('owner response unknown')
        with self.assertRaises(TimeoutError):self.run_delivery()
        self.assertTrue(self.record['native_sheets']['roles']['baseline']['permissions']['owner_transfer_pending'])
        with self.assertRaisesRegex(InputError,'交接回执未知'):self.run_delivery()
        self.assertEqual(delivery.policy.transfer_report_owner.call_count,1)
    def test_checkpoint_cannot_reuse_same_target_for_two_roles(self):
        self.run_delivery();entry=self.record['native_sheets']['roles']
        entry['replacement']['token']=entry['baseline']['token'];entry['replacement']['url']=entry['baseline']['url']
        with self.assertRaisesRegex(InputError,'共用了同一份'):self.run_delivery()
        self.assertEqual(self.fake.counts['copy'],2)
    def test_images_are_not_an_enabled_option(self):
        self.settings['report_delivery']['include_images']=True
        with self.assertRaisesRegex(InputError,'不生成或上传图片'):self.run_delivery()
        self.assertFalse(self.fake.calls)
    def test_settings_identity_change_is_rejected_on_resume(self):
        self.run_delivery();self.settings['lark_home']=str(self.root/'otherIdentity')
        with self.assertRaisesRegex(InputError,'发布身份'):self.run_delivery()
    def test_numeric_statistic_string_may_be_stored_as_exact_number(self):
        self.fake.coerce_numeric_values=True
        result=self.run_delivery()
        self.assertEqual(result['baseline']['validation']['numeric_statistic_cells'],7)
        self.assertEqual(result['replacement']['validation']['status'],'ok')
    def test_exact_numeric_readback_recovers_lost_write_response(self):
        self.fake.coerce_numeric_values=True;self.fake.fail['write']='after'
        with self.assertRaises(TimeoutError):self.run_delivery()
        result=self.run_delivery();self.assertEqual(result['baseline']['validation']['status'],'ok')
        self.assertEqual(self.fake.counts['write'],2)
    def test_decimal_comparison_has_no_float_tolerance(self):
        self.fake.coerce_numeric_values=True;self.fake.numeric_noise=1e-9
        with self.assertRaisesRegex(InputError,'完整读回'):self.run_delivery()
        self.assertNotEqual(self.record['native_sheets']['roles']['baseline']['status'],'ready')
    def test_numeric_looking_labels_are_not_interchangeable(self):
        self.assertFalse(delivery._matches({'value':1},'001'))
        self.assertFalse(delivery._matches({'value':273.36},'273.36'))
        self.assertTrue(delivery._matches({'value':273.36},'273.36',numeric_statistic=True))
        self.assertFalse(delivery._matches({'value':273.36000001},'273.36',numeric_statistic=True))
        self.assertFalse(delivery._matches({'value':True},1))
        self.assertFalse(delivery._matches({'value':0},False))
        self.assertFalse(delivery._matches({'value':2},{'type':'formula','text':'=1+1'}))
    def test_original_template_cannot_be_used_as_copy_target(self):
        original=self.fake.result
        def changed(stage,effect):
            value=original(stage,effect)
            if stage=='copy':value['data'].update(file_token='templateSynthetic',url='https://example.feishu.cn/sheets/templateSynthetic')
            return value
        self.fake.result=changed
        with self.assertRaisesRegex(InputError,'复制回执'):self.run_delivery()
        self.assertNotIn('write',self.fake.counts)
    def test_wrong_native_reference_sheet_is_rejected(self):
        self.change_data('baseline',lambda data:data['native_template_reference'].update(sheet_id='notThere'))
        with self.assertRaisesRegex(InputError,'sheet_id'):self.run_delivery()
        self.assertNotIn('copy',self.fake.counts)
    def test_native_ps_time_numeric_string_is_a_numeric_statistic(self):
        for role in delivery.ROLES:self.change_data(role,lambda data:data['rd']['fps'].update(mean='10.0003'))
        self.fake.coerce_numeric_values=True
        result=self.run_delivery();self.assertEqual(result['baseline']['validation']['numeric_statistic_cells'],7)
    def test_ps_string_field_cannot_be_reinterpreted_as_number(self):
        self.change_data('baseline',lambda data:data['rules']['synthetic']['fps'].update(ps_type='ps_string'))
        self.fake.coerce_numeric_values=True
        with self.assertRaisesRegex(InputError,'完整读回'):self.run_delivery()
    def test_literal_suffix_or_leading_zero_is_not_numeric_semantics(self):
        base={'rd':{'fps':{'mean':'10.0003'}},'rules':{'t':{'fps':{'ps_type':'ps_time'}}}}
        item={'range':'sheet0!D2:D2','placeholder':'&fps@mean&=ms','paths':[['fps','mean']]}
        plan={'sheets':[{'sheet_id':'sheet0','values':[[None]*14,[None,None,'10.0003']+[None]*11], 'placeholders':[item]}]}
        self.assertEqual(delivery._numeric_stat_cells(base,plan),set())
        item['placeholder']='&fps@mean';base['rd']['fps']['mean']='001'
        self.assertEqual(delivery._numeric_stat_cells(base,plan),set())
        base['rd']['fps']['mean']='10.0003'
        self.assertEqual(delivery._numeric_stat_cells(base,plan),{('sheet0',1,2)})
    def test_actual_portable_helper_integrates_with_both_bot_copies(self):
        path=Path(__file__).with_name('native_sheet_data.py')
        spec=importlib.util.spec_from_file_location('_actual_native_helper_for_delivery_test',path)
        actual=importlib.util.module_from_spec(spec);spec.loader.exec_module(actual)
        with patch.dict(sys.modules,{'native_sheet_data':actual}):
            from native_sheet_test_fixture import synthetic_renderer_bundle
        for role,meta in [('baseline',META),('replacement',OTHER)]:
            rules={'t':{'fps':{'ps_type':'ps_time'}}}
            rows=[{'theme':'t','metric':'fps','rule':rules['t']['fps'],'statistics':{'mean':10.0003},
                'status':'available','native_publish':True,'diagnostic':'','skip_reason':''}]
            interval='/'.join(value.replace('-','').replace(':','').replace('T','') for value in (meta['start'],meta['end']))
            context={'vin':meta['vin'],'time_range':interval,'request':meta,'environment':'testtwo','role':role,'rules_sha256':'a'*64}
            data=actual.build_native_sheet_data({'title':'Native '+role,'version':'synthetic','fps':{'mean':10.0003,'image':None}},
                rows,rules,context,synthetic_renderer_bundle(),{'token':'templateSynthetic','sheet_id':'sheet0'})
            ref=self.result['native_sheet_data'][role];file=Path(ref['path'])
            file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            ref.update(sha256=delivery._digest(file),bytes=file.stat().st_size)
        self.fake.coerce_numeric_values=True
        with patch.dict(sys.modules,{'native_sheet_data':actual}):
            result=self.run_delivery();again=self.run_delivery()
        self.assertEqual(result,again);self.assertEqual(self.fake.counts['copy'],2)
        self.assertEqual(result['baseline']['validation']['complete_statistics_count'],1)
        self.assertEqual(result['baseline']['validation']['numeric_statistic_cells'],7)
    def test_second_role_unknown_copy_preserves_first_ready_role(self):
        original=self.fake.result
        armed=True
        def fail_second(stage,effect):
            nonlocal armed
            if stage=='copy' and self.fake.counts.get('copy')==1 and armed:
                self.fake.fail['copy']='after';armed=False
            return original(stage,effect)
        self.fake.result=fail_second
        with self.assertRaises(TimeoutError):self.run_delivery()
        first=self.record['native_sheets']['roles']['baseline']
        self.assertEqual(first['status'],'ready');first_token=first['token']
        with self.assertRaisesRegex(InputError,'禁止自动重复'):self.run_delivery()
        self.assertEqual(self.fake.counts['copy'],2)
        self.assertEqual(self.record['native_sheets']['roles']['baseline']['token'],first_token)


if __name__=='__main__':unittest.main()
