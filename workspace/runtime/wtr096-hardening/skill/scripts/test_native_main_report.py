"""Template and value-level checks of the new text-only main report."""
import copy
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
from feishu_report import (build_report_document, build_report_notification,
                          verify_main_report_document, METRICS, SCENARIOS)
from request_model import InputError


def fixture():
    return {
        "delivery_format":"docx_with_native_sheet_details","title":"合成走查测试",
        "comparisons":{scenario:{"metrics":{metric:{"status":"ok","baseline":10.0,
            "replacement":9.0,"delta":-1.0,"tone":"improved"} for metric,_ in METRICS}}
            for scenario in SCENARIOS},
        "native_details":{"baseline":{"url":"https://example.feishu.cn/sheets/SyntheticBase"},
                          "replacement":{"url":"https://example.feishu.cn/sheets/SyntheticPcm"}},
        "baseline":{"time_range":{"start":"2026-01-01T10:00:00","end":"2026-01-01T10:30:00"}},
        "replacement":{"time_range":{"start":"2026-01-01T11:00:00","end":"2026-01-01T11:30:00"}},
        "local_sources":{"baseline":{"environment":"testtwo"},"replacement":{"environment":"testtwo"}},
        "product_version":{"status":"missing","reason":"查询超时"}}


class NativeMainReportTests(unittest.TestCase):
    def test_original_structure_48_values_links_and_no_graphs(self):
        result=fixture()
        original=copy.deepcopy(result)
        document=build_report_document(result)
        root=ET.fromstring("<root>"+document["xml"]+"</root>")
        # The target preserves its established table-only body. Detail links
        # are validated metadata and are delivered in the same source reply.
        self.assertEqual([n.tag for n in root],["title","table"])
        table=root.find("table")
        template=ET.fromstring((Path(__file__).parents[1]/"assets/report-table.xml").read_text(encoding="utf-8"))
        self.assertEqual(ET.tostring(table.find("thead")),ET.tostring(template.find("thead")))
        self.assertEqual(ET.tostring(table.find("colgroup")),ET.tostring(template.find("colgroup")))
        rows=table.findall(".//tr")[-4:]
        self.assertTrue(all(len(row)==13 for row in rows))
        self.assertEqual(sum(len(row)-1 for row in rows),48)
        self.assertEqual("".join(rows[0][1].itertext()),"10")
        self.assertEqual("".join(rows[0][2].itertext()),"9 (-1)")
        self.assertEqual(rows[0][2].find("p/span").get("text-color"),"green")
        self.assertEqual(len(root.findall(".//a")),0)
        self.assertEqual(document['source_links'], [result['native_details'][role]['url']
                         for role in ('baseline','replacement')])
        self.assertFalse(root.findall(".//img"))
        self.assertNotIn("Excel",document["xml"])
        self.assertNotIn("{{",document["xml"])
        notice = build_report_notification(result,vin='SYNTHETICVIN',url='https://example.feishu.cn/docx/Report')
        self.assertIn('历史时段的安装版本未验证',str(notice))
        self.assertTrue(all(url in str(notice) for url in document['source_links']))
        self.assertEqual(result,original)

    def test_one_sided_value_is_kept_without_delta_or_color(self):
        result=fixture()
        pair=result["comparisons"]["fast_driver"]["metrics"]["p99_infer_delay_ms"]
        pair.update(status="missing",baseline=316,replacement=None,delta=None,tone=None,
                    replacement_status="empty")
        root=ET.fromstring("<root>"+build_report_document(result)["xml"]+"</root>")
        row=root.find("table").findall(".//tr")[-3]
        self.assertEqual("".join(row[1].itertext()),"316")
        self.assertEqual("".join(row[2].itertext()),"-")
        self.assertFalse(row[2].findall(".//span"))

    def test_missing_or_duplicate_or_untrusted_detail_links_rejected(self):
        for case in ("missing","duplicate","untrusted"):
            with self.subTest(case=case):
                result=fixture()
                if case=="missing": result["native_details"].pop("replacement")
                elif case=="duplicate":result["native_details"]["replacement"]=result["native_details"]["baseline"]
                else:result["native_details"]["replacement"]["url"]="https://invalid.test/sheets/Synthetic"
                with self.assertRaises(InputError):build_report_document(result)

    def test_readback_preserves_rgb_semantics_and_rejects_modified_value_color_or_link(self):
        expected=build_report_document(fixture())
        actual=expected['xml'].replace('text-color="green"','text-color="rgb(46,161,33)"')
        self.assertEqual(verify_main_report_document(actual,expected)['data_cells_verified'],48)
        for wrong in (
            actual.replace('>10</p>','>11</p>',1),
            actual.replace('rgb(46,161,33)','rgb(216,57,49)',1),
            actual+'<p><a href="https://example.feishu.cn/sheets/OtherPcm">unexpected</a></p>',
        ):
            with self.subTest(wrong=wrong[:80]),self.assertRaises(InputError):
                verify_main_report_document(wrong,expected)


if __name__=="__main__": unittest.main()
