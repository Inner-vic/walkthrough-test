"""Compact report/notice contracts, entirely synthetic and offline."""
import copy
import json
import unittest
import xml.etree.ElementTree as ET

from feishu_report import (PRESENTATION_V2, build_report_document,
    build_report_notification, verify_main_report_document)
from performance_comparison import UNPARSED_LABEL
from request_model import InputError
from test_native_main_report import fixture


VIN = 'WBA123456789ABCDE'
URL = 'https://example.feishu.cn/docx/SyntheticMain'


def compact_fixture():
    result = fixture()
    result.update(presentation_version=PRESENTATION_V2,vin=VIN,title='AB12 20260101 走查测试')
    result['pipeline'] = {'environment':'testtwo','windows':{role:{'environment':'testtwo','request':{
        'vin':VIN,**copy.deepcopy(result[role]['time_range'])}}
        for role in ('baseline','replacement')}}
    return result


class CompactPresentationTests(unittest.TestCase):
    def notice(self, result=None, *, vin=VIN, url=URL):
        return build_report_notification(compact_fixture() if result is None else result,vin=vin,url=url)

    def test_notice_is_exactly_one_bare_main_url_vin_and_two_complete_windows(self):
        result = compact_fixture();original = copy.deepcopy(result)
        notice = self.notice(result)
        self.assertEqual(notice,{'text':URL + '\nVIN：' + VIN
            + '\nBase：2026-01-01 10:00:00 ～ 2026-01-01 10:30:00'
            + '\nPCM更新：2026-01-01 11:00:00 ～ 2026-01-01 11:30:00'})
        self.assertEqual(notice['text'].count('https://'),1)
        for forbidden in ('颜色','Δ','相关制品','历史时段','未解析','sheets/'):
            self.assertNotIn(forbidden,notice['text'])
        self.assertEqual(result,original)

    def test_cross_day_and_offsets_preserve_both_complete_dates(self):
        result = compact_fixture()
        period = {'start':'2026-01-01T23:55:00+08:00','end':'2026-01-02T00:20:30+08:00'}
        result['baseline']['time_range'] = copy.deepcopy(period)
        result['pipeline']['windows']['baseline']['request'].update(period)
        text = self.notice(result)['text']
        self.assertIn('Base：2026-01-01 23:55:00+08:00 ～ 2026-01-02 00:20:30+08:00',text)

    def test_equivalent_minute_and_second_iso_precision_is_canonical(self):
        result = compact_fixture()
        result['baseline']['time_range']['start'] = '2026-01-01T10:00'
        self.assertIn('Base：2026-01-01 10:00:00',self.notice(result)['text'])

    def test_vin_must_match_result_parameter_and_both_pipeline_requests(self):
        for case in ('parameter','result-missing','result-invalid','baseline','replacement'):
            with self.subTest(case=case):
                result = compact_fixture();vin = VIN
                if case == 'parameter':vin = 'WBA00000000000000'
                elif case == 'result-missing':result.pop('vin')
                elif case == 'result-invalid':result['vin'] = 'private\nhttps://untrusted.test'
                else:result['pipeline']['windows'][case]['request']['vin'] = 'WBA00000000000000'
                with self.assertRaises(InputError):self.notice(result,vin=vin)

    def test_both_exact_role_bindings_required_without_any_fallback(self):
        for case in ('missing-pipeline','missing-role','foreign-role','swap-period','changed-bound-time'):
            with self.subTest(case=case):
                result = compact_fixture()
                if case == 'missing-pipeline':result.pop('pipeline')
                elif case == 'missing-role':result['pipeline']['windows'].pop('baseline')
                elif case == 'foreign-role':result['pipeline']['windows']['other'] = {}
                elif case == 'swap-period':result['baseline']['time_range'] = result['replacement']['time_range']
                else:result['pipeline']['windows']['replacement']['request']['end'] = '2026-01-01T11:50:00'
                result['source'] = {'description':'do not use this time fallback'}
                with self.assertRaises(InputError):self.notice(result)

    def test_invalid_incomplete_reversed_or_mixed_timezone_periods_rejected(self):
        periods = [None,{}, {'start':'2026-01-01','end':'2026-01-02'},
            {'start':'2026-01-01T10:00:00','end':'2026-01-01T09:00:00'},
            {'start':'2026-01-01T10:00:00','end':'2026-01-01T10:00:00'},
            {'start':'2026-02-30T10:00:00','end':'2026-02-30T11:00:00'},
            {'start':'2026-01-01T10:00:00','end':'2026-01-01T11:00:00+08:00'},
            {'start':False,'end':'2026-01-01T11:00:00'}]
        for period in periods:
            with self.subTest(period=period):
                result = compact_fixture()
                result['baseline']['time_range'] = period
                if isinstance(period,dict):result['pipeline']['windows']['baseline']['request'].update(period)
                with self.assertRaises(InputError):self.notice(result)

    def test_main_url_must_be_exact_https_docx_with_no_extra_data(self):
        invalid = ['http://example.feishu.cn/docx/Main','https://evil.test/docx/Main',
            'https://example.feishu.cn.evil.test/docx/Main','https://user@example.feishu.cn/docx/Main',
            URL + '?secret=not-for-message',URL + '#fragment',URL + '/extra',
            'https://example.feishu.cn:443/docx/Main','https://example.feishu.cn/sheets/Main',
            URL + '\nhttps://evil.test',None]
        for url in invalid:
            with self.subTest(url=url),self.assertRaises(InputError):self.notice(url=url)

    def test_two_distinct_valid_details_are_required_before_hiding_them_in_notice(self):
        for case in ('missing','duplicate','query','untrusted','malformed'):
            with self.subTest(case=case):
                result = compact_fixture()
                if case == 'missing':result['native_details'].pop('replacement')
                elif case == 'duplicate':result['native_details']['replacement'] = result['native_details']['baseline']
                elif case == 'query':result['native_details']['replacement']['url'] += '?unapproved=x'
                elif case == 'malformed':result['native_details']['baseline'] = None
                else:result['native_details']['baseline']['url'] = 'https://evil.test/sheets/Base'
                with self.assertRaises(InputError):self.notice(result)
                with self.assertRaises(InputError):build_report_document(result)

    def test_main_table_is_unchanged_and_small_table_contains_two_real_role_links(self):
        result = compact_fixture()
        document = build_report_document(result)
        actual = ET.fromstring('<root>' + document['xml'] + '</root>')
        legacy = copy.deepcopy(result);legacy.pop('presentation_version')
        legacy_root = ET.fromstring('<root>' + build_report_document(legacy)['xml'] + '</root>')
        self.assertEqual([n.tag for n in actual],['title','table','table'])
        self.assertEqual(ET.tostring(actual[1]),ET.tostring(legacy_root[1]))
        rows = actual[1].findall('.//tr')[-4:]
        self.assertEqual(sum(len(row)-1 for row in rows),48)
        detail_rows = actual[2].findall('.//tr')
        self.assertEqual(len(detail_rows),2)
        self.assertEqual([''.join(row[0].itertext()) for row in detail_rows],['Base','PCM更新'])
        self.assertEqual([a.get('href') for a in actual[2].findall('.//a')],document['source_links'])
        self.assertFalse(actual.findall('.//img'))
        for link in document['source_links']:self.assertNotIn(link,self.notice(result)['text'])

    def test_unparsed_and_partial_source_warnings_remain_in_report_only(self):
        result = compact_fixture()
        pair = result['comparisons']['parking']['metrics']['pcm_cpu_percent']
        pair.update(baseline=None,baseline_status='unparsed',status='missing')
        result['local_sources']['replacement']['source_quality'] = {'status':'partial','private':'never forward'}
        document = build_report_document(result)
        root = ET.fromstring('<root>' + document['xml'] + '</root>')
        self.assertIn(UNPARSED_LABEL,ET.tostring(root[1],encoding='unicode'))
        self.assertIn('text-color="red"',ET.tostring(root[1],encoding='unicode'))
        rows = root[2].findall('.//tr')
        self.assertNotIn('不完整日志',''.join(rows[0].itertext()))
        self.assertIn('不完整日志',''.join(rows[1].itertext()))
        text = self.notice(result)['text']
        self.assertNotIn('不完整',text);self.assertNotIn(UNPARSED_LABEL,text)
        self.assertNotIn('never forward',document['xml'])

    def test_readback_verifies_48_cells_and_exact_two_actual_links(self):
        document = build_report_document(compact_fixture())
        actual = document['xml'].replace('text-color="green"','text-color="rgb(46,161,33)"')
        actual = actual.replace('<table>','<table id="server-generated">')
        check = verify_main_report_document(actual,document)
        self.assertEqual(check['data_cells_verified'],48)
        self.assertEqual(check['native_detail_links_verified'],2)
        self.assertEqual(check['native_detail_metadata_links_validated'],2)

    def test_footer_explicit_default_alignment_matches_actual_readback_and_bottom_rejected(self):
        document = build_report_document(compact_fixture())
        root = ET.fromstring('<root>' + document['xml'] + '</root>')
        footer_cells = root[2].findall('.//td')
        self.assertEqual(len(footer_cells),4)
        self.assertTrue(all(cell.get('vertical-align') == 'top' for cell in footer_cells))
        # The service writes its default on each footer cell during real readback.
        for cell in footer_cells:cell.set('vertical-align','top')
        actual = ''.join(ET.tostring(node,encoding='unicode') for node in root)
        check = verify_main_report_document(actual,document)
        self.assertEqual(check['data_cells_verified'],48)
        self.assertEqual(check['native_detail_links_verified'],2)
        footer_cells[0].set('vertical-align','bottom')
        wrong = ''.join(ET.tostring(node,encoding='unicode') for node in root)
        with self.assertRaises(InputError):verify_main_report_document(wrong,document)

    def test_readback_rejects_malicious_href_swapped_roles_or_metadata(self):
        document = build_report_document(compact_fixture())
        for xml in (document['xml'].replace(document['source_links'][0],'https://evil.test/sheets/Fake'),
                    document['xml'].replace('<p>Base</p>','<p>other</p>'),
                    document['xml'].replace('href="','onclick="unapproved" href="',1)):
            with self.subTest(xml=xml[-100:]),self.assertRaises(InputError):
                verify_main_report_document(xml,document)
        wrong = {**document,'source_links':list(reversed(document['source_links']))}
        with self.assertRaises(InputError):verify_main_report_document(document['xml'],wrong)

    def test_readback_rejects_any_extra_nodes_links_images_or_warning_removal(self):
        result = compact_fixture()
        result['local_sources']['baseline']['source_quality'] = {'status':'partial'}
        document = build_report_document(result)
        for xml in (document['xml'] + '<p>unapproved extra</p>',document['xml'] + '<table/>',
                    document['xml'].replace('</title>','</title><a href="https://evil.test">extra</a>'),
                    document['xml'].replace('查看完整原生明细','<img/>',1),
                    document['xml'].replace('<p>MCU 来源存在不完整日志，相关统计仅供参考。</p>','')):
            with self.subTest(xml=xml[-100:]),self.assertRaises(InputError):
                verify_main_report_document(xml,document)

    def test_readback_still_rejects_main_value_color_and_layout_changes(self):
        document = build_report_document(compact_fixture())
        for xml in (document['xml'].replace('>10</p>','>11</p>',1),
                    document['xml'].replace('text-color="green"','text-color="red"',1),
                    document['xml'].replace('width="115"','width="116"',1)):
            with self.subTest(xml=xml[:100]),self.assertRaises(InputError):
                verify_main_report_document(xml,document)

    def test_legacy_default_notice_and_document_contract_remain_unchanged(self):
        result = fixture()
        notice = build_report_notification(result,vin='SYNTHETICVIN',url=URL)
        self.assertEqual(set(notice),{'zh_cn'})
        self.assertIn('主表单位',json.dumps(notice,ensure_ascii=False))
        document = build_report_document(result)
        root = ET.fromstring('<root>' + document['xml'] + '</root>')
        self.assertEqual([n.tag for n in root],['title','table'])
        self.assertFalse(root.findall('.//a'))
        self.assertNotIn('presentation_version',document)
        self.assertEqual(verify_main_report_document(document['xml'],document)['native_detail_links_verified'],0)

    def test_compact_marker_cannot_enable_legacy_non_native_result(self):
        result = compact_fixture();result['delivery_format'] = 'docx_with_full_xlsx_attachments'
        with self.assertRaises(InputError):self.notice(result)
        with self.assertRaises(InputError):build_report_document(result)


if __name__ == '__main__':unittest.main()
