"""Two display labels may change only on explicitly bound new reports."""
from contextlib import ExitStack
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import feishu_report as report
import walkthrough_worker as worker
from request_model import InputError
from test_compact_presentation import compact_fixture, VIN, URL
from test_native_main_report import fixture
from test_source_paragraph_presentation import paragraph_fixture
import test_product_pipeline as pipeline_fixture


def new_fixture():
    value = paragraph_fixture()
    value['metric_header_style'] = report.METRIC_HEADER_NO_AD
    return value


class MetricHeaderStyleTests(unittest.TestCase):
    def test_new_header_changes_exactly_two_text_leaves_and_keeps_48_cells_and_styles(self):
        old = report.build_report_document(paragraph_fixture())
        value = new_fixture(); before = copy.deepcopy(value)
        new = report.build_report_document(value)
        self.assertEqual(new['metric_header_style'], 'no_ad')
        original = ET.fromstring('<root>' + old['xml'] + '</root>')
        changed = ET.fromstring('<root>' + new['xml'] + '</root>')
        differences = [(a.tag, a.text, b.text) for a,b in zip(original.iter(), changed.iter())
                       if a.text != b.text]
        self.assertEqual(differences, [('b','AD P99 Infer Delay','P99 Infer Delay'),
                                       ('b','AD Frame Rate','Frame Rate')])
        headers = changed[1].findall('./thead/tr/th')
        self.assertEqual([''.join(h.itertext()) for h in headers][1:3],
                         ['P99 Infer Delay','Frame Rate'])
        headers[1].find('p/b').text = 'AD P99 Infer Delay'
        headers[2].find('p/b').text = 'AD Frame Rate'
        self.assertEqual(ET.tostring(changed), ET.tostring(original))
        proof = report.verify_main_report_document(new['xml'], new)
        self.assertEqual((proof['data_cells_verified'], proof['native_detail_links_verified']), (48,2))
        self.assertEqual(value,before)

    def test_unmarked_legacy_native_and_v2_results_match_frozen_renderer(self):
        workspace = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').is_file())
        spec = importlib.util.spec_from_file_location('_frozen_header_renderer',
            workspace.parent/'tests/fixtures/feishu_report_target6.py')
        frozen = importlib.util.module_from_spec(spec); spec.loader.exec_module(frozen)
        legacy = fixture(); legacy.pop('delivery_format')
        for value in (legacy,fixture(),compact_fixture(),paragraph_fixture()):
            with self.subTest(presentation=value.get('presentation_version'),
                              detail=value.get('detail_links_style')):
                expected = frozen.build_report_document(value)
                actual = report.build_report_document(value)
                self.assertEqual(actual,expected)
                self.assertNotIn('metric_header_style',actual)
                report.verify_main_report_document(actual['xml'],expected)

    def test_plain_text_notification_is_unchanged(self):
        expected = report.build_report_notification(paragraph_fixture(),vin=VIN,url=URL)
        actual = report.build_report_notification(new_fixture(),vin=VIN,url=URL)
        self.assertEqual(actual,expected)
        self.assertEqual(set(actual),{'text'})
        self.assertEqual(len(actual['text'].splitlines()),4)
        self.assertEqual(actual['text'].count('https://'),1)

    def test_invalid_or_non_v2_marker_is_rejected_by_render_and_notification(self):
        for marker in (None,True,{},'future','no_ad '):
            value = new_fixture(); value['metric_header_style'] = marker
            with self.subTest(marker=marker), self.assertRaises(InputError):
                report.build_report_document(value)
            with self.subTest(notification=marker), self.assertRaises(InputError):
                report.build_report_notification(value,vin=VIN,url=URL)
        value = new_fixture(); value.pop('presentation_version')
        with self.assertRaises(InputError): report.build_report_document(value)

    def test_strict_readback_keeps_header_value_color_and_link_guards(self):
        old = report.build_report_document(paragraph_fixture())
        new = report.build_report_document(new_fixture())
        for actual,expected in ((old['xml'],new),(new['xml'],old),
                (new['xml'].replace('>10</p>','>11</p>',1),new),
                (new['xml'].replace('text-color="green"','text-color="red"',1),new),
                (new['xml'].replace(new['source_links'][0],new['source_links'][1]),new)):
            with self.subTest(actual=actual[:40]), self.assertRaises(InputError):
                report.verify_main_report_document(actual,expected)
        invalid = copy.deepcopy(new); invalid['metric_header_style'] = 'future'
        with self.assertRaises(InputError):
            report.verify_main_report_document(new['xml'],invalid)

    def test_new_render_requires_exact_original_header_labels(self):
        source = (Path(report.__file__).parents[1]/'assets/report-table.xml').read_text('utf-8')
        for label in ('AD P99 Infer Delay','AD Frame Rate'):
            with self.subTest(label=label), patch.object(report.Path,'read_text',
                    return_value=source.replace(label,'Unexpected header',1)):
                with self.assertRaisesRegex(InputError,'原指标表头不匹配'):
                    report.build_report_document(new_fixture())

    def test_single_sided_unknown_or_non_v2_binding_stops_before_cloud_or_save(self):
        absent = object()
        for left,right in ((absent,'no_ad'),('no_ad',absent),
                           ('future','no_ad'),('no_ad','future'),(None,'no_ad')):
            record = {'presentation_version':worker.PRESENTATION_VERSION}
            result = {'presentation_version':worker.PRESENTATION_VERSION,
                      'delivery_format':worker.DELIVERY_FORMAT}
            if left is not absent: record['metric_header_style'] = left
            if right is not absent: result['metric_header_style'] = right
            before = copy.deepcopy((record,result))
            with self.subTest(left=left,right=right), ExitStack() as stack:
                for name in ('run_lark','ensure_report_bot_authorization','atomic_json'):
                    stack.enter_context(patch.object(worker.policy,name,side_effect=AssertionError('no cloud/save')))
                with self.assertRaisesRegex(InputError,'指标表头样式不一致'):
                    worker.publish({},Path('unused'),record,result)
            self.assertEqual((record,result),before)
        record = {'metric_header_style':'no_ad'}
        result = {'metric_header_style':'no_ad','delivery_format':worker.DELIVERY_FORMAT}
        with self.assertRaisesRegex(InputError,'指标表头样式不一致'):
            worker.publish({},Path('unused'),record,result)

    def test_final_marker_propagates_without_changing_metrics_or_old_final_shape(self):
        helper = pipeline_fixture.ProductPipelineTests(); helper.setUp(); self.addCleanup(helper.doCleanups)
        helper.record['presentation_version'] = worker.PRESENTATION_VERSION
        old = helper.local(helper.root/'old')
        helper.record['metric_header_style'] = worker.METRIC_HEADER_STYLE
        new = helper.local(helper.root/'new')
        self.assertNotIn('metric_header_style',old)
        self.assertEqual(new['metric_header_style'],'no_ad')
        for key in ('comparisons','metric_rules','table','coverage','issues'):
            self.assertEqual(old[key],new[key])
        helper.record['metric_header_style'] = 'future'
        with self.assertRaisesRegex(InputError,'主报告指标表头样式无效'):
            helper.local(helper.root/'invalid')

    def test_new_start_marks_record_and_old_source_replay_keeps_bytes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            identity = {'source_message_id':'om_header','source_chat_id':'oc_source',
                        'sender_id':'ou_requester','source_thread_id':'om_header'}
            period = {'vin':VIN,'start':'2026-01-01T10:00:00','end':'2026-01-01T10:30:00'}
            request = {'vin':VIN,'baseline_metadata':period,
                       'replacement_metadata':{**period,'start':'2026-01-01T11:00:00','end':'2026-01-01T11:30:00'}}
            settings = {'runtime_state_dir':str(root),'workspace_root':str(root)}
            args = SimpleNamespace(text='synthetic body',ccconnect_context='synthetic context',
                settings=root/'unused-settings.json',received_at='2026-01-01T12:00:00+08:00')
            stack.enter_context(patch.object(worker,'load_settings',return_value=settings))
            stack.enter_context(patch.object(worker,'request_identity',return_value=identity))
            stack.enter_context(patch.object(worker,'source_normalization_anchor',return_value=args.received_at))
            stack.enter_context(patch.object(worker,'route',return_value={'status':'ready','normalized':request}))
            for name in ('activate_lark_environment','ensure_report_bot_authorization','ensure_secure_label_authorization'):
                stack.enter_context(patch.object(worker.policy,name))
            stack.enter_context(patch.object(worker.policy,'ensure_user_authorization',return_value={'open_id':'ou_operator'}))
            stack.enter_context(patch.object(worker.policy,'resolve_report_secure_label',return_value={'synthetic':True}))
            process = stack.enter_context(patch.object(worker.subprocess,'Popen'))
            stack.enter_context(patch('socket.socket',side_effect=AssertionError('no network')))
            created = worker.start_job(args)
            path = root/(created['job_key']+'.json')
            record = json.loads(path.read_text('utf-8'))
            self.assertEqual(record['metric_header_style'],'no_ad')
            record.pop('metric_header_style')
            path.write_text(json.dumps(record),encoding='utf-8')
            original = path.read_bytes()
            replay = worker.start_job(args)
            self.assertTrue(replay['idempotent'])
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(process.call_count,1)


if __name__ == '__main__': unittest.main()
