"""New source-paragraph contracts; frozen v2 reports stay byte-compatible."""
from contextlib import ExitStack
import copy
import importlib.util
from pathlib import Path
import json
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import feishu_report as report
from request_model import InputError
import walkthrough_worker as worker
from test_compact_presentation import compact_fixture, VIN, URL
import test_product_pipeline as pipeline_fixture
import test_compact_worker_contract as worker_fixture


def paragraph_fixture():
    result = compact_fixture()
    result['detail_links_style'] = report.SOURCE_PARAGRAPH
    return result


class SourceParagraphTests(unittest.TestCase):
    def test_new_paragraph_has_exact_role_links_and_unchanged_48_cell_table(self):
        old = compact_fixture();new = paragraph_fixture()
        original = copy.deepcopy(new)
        before = report.build_report_document(old)
        after = report.build_report_document(new)
        a = ET.fromstring('<root>'+after['xml']+'</root>')
        b = ET.fromstring('<root>'+before['xml']+'</root>')
        self.assertEqual([n.tag for n in a],['title','table','p'])
        self.assertEqual(ET.tostring(a[1]),ET.tostring(b[1]))
        self.assertEqual(len(a.findall('.//a')),2)
        self.assertEqual([''.join(n.itertext()) for n in a[2].findall('a')],['Baseline 表格','PCM更新表格'])
        self.assertEqual([n.get('href') for n in a[2].findall('a')],after['source_links'])
        self.assertTrue(''.join(a[2].itertext()).startswith('数据来源：Baseline 表格　PCM更新表格'))
        self.assertFalse(any(a.findall('.//'+tag) for tag in ('sheet','source','file','img','cite')))
        proof = report.verify_main_report_document(after['xml'],after)
        self.assertEqual((proof['data_cells_verified'],proof['native_detail_links_verified']),(48,2))
        self.assertEqual(new,original)

    def test_existing_v2_render_and_notification_match_frozen_installed_renderer(self):
        workspace = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').is_file())
        spec = importlib.util.spec_from_file_location('_frozen_v2_renderer',workspace.parent/'tests/fixtures/feishu_report_target5.py')
        frozen = importlib.util.module_from_spec(spec);spec.loader.exec_module(frozen)
        for partial in (False,True):
            with self.subTest(partial=partial):
                result=compact_fixture()
                if partial:result['local_sources']['baseline']['source_quality']={'status':'partial'}
                self.assertEqual(report.build_report_document(result),frozen.build_report_document(result))
                self.assertEqual(report.build_report_notification(result,vin=VIN,url=URL),
                                 frozen.build_report_notification(result,vin=VIN,url=URL))

    def test_notification_is_identical_four_line_text(self):
        self.assertEqual(report.build_report_notification(paragraph_fixture(),vin=VIN,url=URL),
                         report.build_report_notification(compact_fixture(),vin=VIN,url=URL))

    def test_unknown_or_wrong_presentation_style_rejected(self):
        for value in (None,'table','future',{},True):
            with self.subTest(value=value):
                result=paragraph_fixture();result['detail_links_style']=value
                with self.assertRaises(InputError):report.build_report_document(result)
                with self.assertRaises(InputError):report.build_report_notification(result,vin=VIN,url=URL)
        result=paragraph_fixture();result.pop('presentation_version')
        with self.assertRaises(InputError):report.build_report_document(result)

    def test_incomplete_frame_note_states_evidence_without_claiming_missing_window_data(self):
        result=paragraph_fixture()
        result['local_sources']['baseline']['source_quality']={'status':'partial','issues':[{
            'kind':'incomplete_zstd_frame','recovery':'complete_decoded_lines','discarded_tail_bytes':0,
            'relative_path':'log_xcu_mcu/private-source-name.log','source_sha256':'a'*64}]}
        xml=report.build_report_document(result)['xml']
        self.assertIn('Base：1份MCU压缩日志缺少完整结束帧，已保留完整解码行；日志完整性未确认。',xml)
        for invalid in ('请求时段缺失','必然缺失','private-source-name','a'*64):self.assertNotIn(invalid,xml)
        report.verify_main_report_document(xml,report.build_report_document(result))

    def test_bzcu_frame_note_uses_only_its_verified_class_prefix(self):
        result=paragraph_fixture()
        result['local_sources']['replacement']['source_quality']={'status':'partial','issues':[{
            'kind':'incomplete_zstd_frame','recovery':'complete_decoded_lines','discarded_tail_bytes':0,
            'relative_path':'log_bzcu_eth/private-bzcu.log'}]}
        xml=report.build_report_document(result)['xml']
        self.assertIn('PCM更新：1份BZCU压缩日志缺少完整结束帧',xml)
        self.assertNotIn('MCU压缩日志',xml)
        self.assertNotIn('private-bzcu',xml)

    def test_mixed_frame_notes_count_classes_separately(self):
        result=paragraph_fixture()
        result['local_sources']['baseline']['source_quality']={'status':'partial','issues':[
            {'kind':'incomplete_zstd_frame','recovery':'complete_decoded_lines','discarded_tail_bytes':0,
             'relative_path':path} for path in ('log_xcu_mcu/a.log','log_xcu_mcu/b.log',
                                              'log_bzcu_eth/c.log','unknown/d.log')]}
        xml=report.build_report_document(result)['xml']
        for expected in ('Base：2份MCU压缩日志','Base：1份BZCU压缩日志','Base：1份来源压缩日志'):
            self.assertIn(expected,xml)
        self.assertNotIn('4份MCU',xml)

    def test_unknown_or_unsafe_source_path_never_guesses_class(self):
        for path in (None,[], 'private-source-name','unknown/a.log','log_xcu_mcu_fake/a.log',
                     '/log_xcu_mcu/a.log','log_xcu_mcu/../a.log','log_bzcu_eth//a.log',
                     'log_xcu_mcu/./a.log','log_xcu_mcu/a\\b.log','log_xcu_mcu/C:a.log',
                     'log_bzcu_eth/a\x00.log','log_xcu_mcu/'):
            with self.subTest(path=path):
                result=paragraph_fixture()
                result['local_sources']['baseline']['source_quality']={'status':'partial','issues':[{
                    'kind':'incomplete_zstd_frame','recovery':'complete_decoded_lines','discarded_tail_bytes':0,
                    'relative_path':path}]}
                xml=report.build_report_document(result)['xml']
                self.assertIn('Base：1份来源压缩日志缺少完整结束帧',xml)
                self.assertNotIn('份MCU压缩日志',xml)
                self.assertNotIn('份BZCU压缩日志',xml)

    def test_other_or_unproven_partial_issues_keep_generic_warning(self):
        for issues in ([],[{'kind':'other','detail':'private detail'}],
                       [{'kind':'incomplete_zstd_frame'}]):
            with self.subTest(issues=issues):
                result=paragraph_fixture()
                result['local_sources']['replacement']['source_quality']={'status':'partial','issues':issues}
                xml=report.build_report_document(result)['xml']
                self.assertIn('PCM更新：来源日志完整性未确认，相关统计需结合来源审计核查。',xml)
                self.assertNotIn('已保留完整解码行',xml)
                self.assertNotIn('private detail',xml)

    def test_mixed_partial_issues_show_both_proven_frame_and_general_warning(self):
        result=paragraph_fixture()
        result['local_sources']['baseline']['source_quality']={'status':'partial','issues':[
            {'kind':'incomplete_zstd_frame','recovery':'complete_decoded_lines','discarded_tail_bytes':0},
            {'kind':'other'}]}
        xml=report.build_report_document(result)['xml']
        self.assertIn('1份来源压缩日志缺少完整结束帧',xml)
        self.assertIn('Base：来源日志完整性未确认',xml)

    def test_malformed_quality_structures_are_rejected(self):
        for sources in ([],{'baseline':[]},{'baseline':{'source_quality':[]}},
                        {'baseline':{'source_quality':{'status':'partial','issues':'bad'}}},
                        {'baseline':{'source_quality':{'status':'partial','issues':[None]}}}):
            result=paragraph_fixture();result['local_sources']=sources
            with self.subTest(sources=sources),self.assertRaises(InputError):report.build_report_document(result)

    def test_unparsed_metric_remains_explicit(self):
        result=paragraph_fixture()
        result['comparisons']['parking']['metrics']['pcm_cpu_percent'].update(
            baseline=None,baseline_status='unparsed',status='missing')
        document=report.build_report_document(result)
        self.assertIn('未解析',document['xml'])
        self.assertEqual(report.verify_main_report_document(document['xml'],document)['data_cells_verified'],48)

    def test_strict_readback_rejects_swapped_foreign_duplicate_or_extra_links_and_tags(self):
        document=report.build_report_document(paragraph_fixture())
        for variant in ('swap','foreign','duplicate','extra','table','label','values','color'):
            with self.subTest(variant=variant):
                root=ET.fromstring('<root>'+document['xml']+'</root>');a=root[2].findall('a')
                if variant=='swap':a[0].set('href',a[1].get('href'));a[1].set('href',document['source_links'][0])
                elif variant=='foreign':a[0].set('href','https://example.feishu.cn/sheets/Foreign')
                elif variant=='duplicate':a[1].set('href',a[0].get('href'))
                elif variant=='extra':ET.SubElement(root[2],'a',href=URL).text='extra'
                elif variant=='table':root[2].tag='table'
                elif variant=='label':a[0].text='PCM更新表格'
                elif variant=='values':root[1].findall('.//tr')[-1][1].find('p').text='different'
                else:
                    spans=root[1].findall('.//span');spans[0].set('text-color','purple')
                with self.assertRaises(InputError):report.verify_main_report_document(
                    ''.join(ET.tostring(n,encoding='unicode') for n in root),document)

    def test_old_and_new_footer_cannot_cross_readback_guards(self):
        old=report.build_report_document(compact_fixture());new=report.build_report_document(paragraph_fixture())
        with self.assertRaises(InputError):report.verify_main_report_document(old['xml'],new)
        with self.assertRaises(InputError):report.verify_main_report_document(new['xml'],old)
        bad=copy.deepcopy(new);bad['detail_links_style']='future'
        with self.assertRaises(InputError):report.verify_main_report_document(new['xml'],bad)

    def test_record_result_style_mismatch_stops_before_cloud_or_save(self):
        for r,s in ((None,'source_paragraph'),('source_paragraph',None),('other','source_paragraph')):
            record={'presentation_version':worker.PRESENTATION_VERSION}
            result={'presentation_version':worker.PRESENTATION_VERSION,'delivery_format':worker.DELIVERY_FORMAT}
            if r is not None:record['detail_links_style']=r
            if s is not None:result['detail_links_style']=s
            with self.subTest(r=r,s=s),ExitStack() as stack:
                stack.enter_context(patch.object(worker.policy,'ensure_report_bot_authorization',side_effect=AssertionError('no cloud')))
                stack.enter_context(patch.object(worker.policy,'atomic_json',side_effect=AssertionError('no save')))
                with self.assertRaisesRegex(InputError,'明细入口样式不一致'):
                    worker.publish({},Path('unused'),record,result)

    def test_final_marker_propagation_and_old_v2_missing_marker_preserved(self):
        helper=pipeline_fixture.ProductPipelineTests();helper.setUp();self.addCleanup(helper.doCleanups)
        helper.record['presentation_version']=worker.PRESENTATION_VERSION
        for style in (worker.DETAIL_LINKS_STYLE,None):
            if style:helper.record['detail_links_style']=style
            else:helper.record.pop('detail_links_style',None)
            result=helper.local(helper.root/('paragraph' if style else 'old-table'))
            self.assertEqual(result.get('detail_links_style'),style)
            self.assertEqual('detail_links_style' in result,style is not None)

    def test_new_start_persists_style_while_existing_source_replay_is_unchanged(self):
        helper=worker_fixture.CompactWorkerContractTests();helper.setUp();self.addCleanup(helper.doCleanups)
        actual_start=worker.start_job;created=[]
        def start(args):
            result=actual_start(args)
            if not result['idempotent']:
                created.append(json.loads((helper.root/(result['job_key']+'.json')).read_text('utf-8')))
            return result
        with patch.object(worker,'start_job',side_effect=start):
            helper.test_new_record_is_v2_but_exact_old_source_replay_is_not_migrated()
        self.assertEqual(len(created),1)
        self.assertEqual(created[0]['detail_links_style'],worker.DETAIL_LINKS_STYLE)


if __name__=='__main__':unittest.main()
