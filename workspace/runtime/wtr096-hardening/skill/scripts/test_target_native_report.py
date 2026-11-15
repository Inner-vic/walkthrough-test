"""Target report compatibility: local contracts plus native 201-item delivery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import native_sheet_delivery as delivery
import report_policy as policy
from feishu_report import (build_report_document, build_report_notification,
                          verify_main_report_document, _baseline_cell,
                          _replacement_cell)
from performance_comparison import report_title, display_pair, UNPARSED_LABEL
from request_model import InputError
from test_native_main_report import fixture


class TargetNativeReportTests(unittest.TestCase):
    def test_trusted_product_title_is_explicitly_query_time_and_old_calls_keep_placeholder(self):
        start = '2026-10-21T10:00:00'
        self.assertEqual(report_title('AB12',start),'XXXX（20261021基线）走查测试')
        version = {'status':'ok','code':'AB12','semantics':'query_time_platform_value',
                   'historical_version_verified':False}
        self.assertEqual(report_title(None,start,validated_product_version=version),
                         'AB12（20261021）走查测试')
        for change in ({'code':'VIN000'}, {'semantics':'historical_install'},
                       {'historical_version_verified':True}):
            with self.subTest(change=change),self.assertRaises(InputError):
                report_title(None,start,validated_product_version={**version,**change})
        self.assertEqual(report_title(None,start,validated_product_version={'status':'timeout'}),
                         'XXXX（20261021基线）走查测试')

    def test_native_missing_cells_preserve_evidence_and_keep_exact_48_cells(self):
        result = fixture()
        pair = result['comparisons']['parking']['metrics']['p99_infer_delay_ms']
        pair.update(baseline=None, replacement=None, status='missing',delta=None,
                    baseline_status='empty',replacement_status='unparsed')
        document = build_report_document(result)
        root = ET.fromstring('<root>'+document['xml']+'</root>')
        self.assertEqual([node.tag for node in root],['title','table'])
        row = root.find('table').findall('.//tr')[-2]
        self.assertEqual(''.join(row[1].itertext()),'-')
        self.assertEqual(''.join(row[2].itertext()),UNPARSED_LABEL)
        self.assertEqual(row[2].find('p/span').get('text-color'),'red')
        check = verify_main_report_document(document['xml'],document)
        self.assertEqual(check['data_cells_verified'],48)
        self.assertEqual(check['native_detail_metadata_links_validated'],2)
        self.assertEqual(check['native_detail_links_verified'],0)

    def test_single_notice_includes_both_native_links_and_specific_unparsed_warning(self):
        result = fixture()
        result['comparisons']['parking']['metrics']['pcm_cpu_percent'].update(
            baseline=None,baseline_status='unparsed')
        notice = build_report_notification(result,vin='SYNTHETICVIN',url='https://example.feishu.cn/docx/Report')
        text = json.dumps(notice,ensure_ascii=False)
        self.assertIn('Base · 泊车：PCM CPU Usage',text)
        for role in ('baseline','replacement'):
            self.assertIn(result['native_details'][role]['url'],text)
        self.assertEqual(sum(item.get('tag')=='a' for row in notice['zh_cn']['content'] for item in row),3)
        result['native_details']['replacement'] = result['native_details']['baseline']
        with self.assertRaises(InputError):
            build_report_notification(result,vin='SYNTHETICVIN',url='https://example.feishu.cn/docx/Report')

    def test_main_readback_rejects_mutated_metadata_links(self):
        document = build_report_document(fixture())
        for urls in ([document['source_links'][0]]*2,
                     ['https://invalid.test/sheets/Unexpected',document['source_links'][1]]):
            with self.subTest(urls=urls),self.assertRaises(InputError):
                verify_main_report_document(document['xml'],{**document,'source_links':urls})

    def test_error_values_and_nonfinite_delta_never_render_stale_or_false_color(self):
        pair = {'status':'error','baseline':123,'replacement':124,
                'baseline_status':'empty','replacement_status':'empty','delta':1,'tone':'regressed'}
        for render in (_baseline_cell,_replacement_cell):
            self.assertIn(UNPARSED_LABEL,render(pair))
            self.assertNotIn('123',render(pair));self.assertNotIn('124',render(pair))
        self.assertEqual(display_pair(pair,2)['baseline'],UNPARSED_LABEL)
        for invalid in (float('nan'),float('inf'),True,None):
            current = {**pair,'status':'ok','delta':invalid}
            xml = _replacement_cell(current)
            self.assertIn('124',xml)
            self.assertNotIn('<span',xml)
            self.assertNotIn('(',xml)

    def test_native_sheets_pass_requester_and_persist_source_access_audit(self):
        audit = {'chat_mode':'p2p','member_type':'openid','member_id':'ou_requester','perm':'view'}
        record = {'source_chat_id':'oc_source','sender_id':'ou_requester'}
        entry = {'url':'https://example.feishu.cn/sheets/Synthetic',
                 'permissions':{'collaborator_management':True,'owner_transfer':True}}
        with patch.object(policy,'grant_source_chat_view',return_value=audit) as grant, \
             patch.object(policy,'configure_report_secure_label') as secure:
            persisted = []
            delivery._govern({},Path('synthetic.json'),record,'baseline',entry,
                             lambda: persisted.append(copy.deepcopy(entry)))
        grant.assert_called_once_with({},entry['url'],'oc_source','ou_requester')
        self.assertEqual(entry['permissions']['source_chat_access'],audit)
        self.assertTrue(persisted[0]['permissions']['source_chat_view'])
        self.assertIn('persist_callback',secure.call_args.kwargs)

    def test_native_source_lookup_is_report_bot_and_unknown_type_never_grants(self):
        settings = {'report_lark_profile':'synthetic-report-bot'}
        for mode,expected in [('p2p','openid'),('group','openchat'),('topic','openchat')]:
            with self.subTest(mode=mode),patch.object(policy,'run_lark',side_effect=[
                {'ok':True,'identity':'bot','data':{'chat_mode':mode}}, {'ok':True}]) as run:
                audit = policy.grant_source_chat_view(settings,'https://example.feishu.cn/sheets/Synthetic',
                                                     'oc_source','ou_requester')
                self.assertEqual(audit['member_type'],expected)
                self.assertTrue(all(call.kwargs['profile']=='synthetic-report-bot' for call in run.call_args_list))
        with patch.object(policy,'run_lark',return_value={
                'ok':True,'identity':'bot','data':{'chat_mode':'unknown'}}) as run:
            with self.assertRaises(InputError):
                policy.grant_source_chat_view(settings,'https://example.feishu.cn/sheets/Synthetic',
                                              'oc_source','ou_requester')
            self.assertEqual(run.call_count,1)


if __name__=='__main__':
    unittest.main()
