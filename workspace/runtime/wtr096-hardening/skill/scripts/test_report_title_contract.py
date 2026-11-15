"""Offline report-title contract; no source logs, network, or report writes."""
from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from feishu_report import build_report_document
from performance_comparison import compare_performance, report_title
from request_model import parse_comparison_request


class ReportTitleContractTests(unittest.TestCase):
    def test_trusted_query_code_uses_compact_code_and_base_date(self):
        version = {'status':'ok','code':'9AEE','semantics':'query_time_platform_value',
                   'historical_version_verified':False}
        before = json.dumps(version, sort_keys=True)
        self.assertEqual(report_title(None, '2026-10-23T15:48:00',
                                     validated_product_version=version),
                         '9AEE（20261023）走查测试')
        self.assertEqual(json.dumps(version, sort_keys=True), before)

    def test_new_title_still_requires_verified_code_and_semantics(self):
        version = {'status':'ok','code':'9AEE','semantics':'query_time_platform_value',
                   'historical_version_verified':False}
        for change in ({'code':'9aee'}, {'code':'9AE'}, {'code':'XXXX'},
                       {'semantics':'historical_version'},
                       {'historical_version_verified':True},
                       {'historical_version_verified':None}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    report_title(None, '2026-10-23T15:48:00',
                                 validated_product_version={**version, **change})

    def test_trusted_title_uses_explicit_base_role_date(self):
        request = parse_comparison_request(
            '走查测试 PCM更新 WBA123456789ABCDE, 2026-10-23 15:48, 2026-10-23 16:22\n'
            'Base WBA123456789ABCDE, 2026-10-21 14:50, 2026-10-21 15:24')
        result = compare_performance({}, {},
            baseline_metadata=request['baseline_metadata'],
            replacement_metadata=request['replacement_metadata'],
            validated_product_version={'status':'ok','code':'9AEE',
                'semantics':'query_time_platform_value','historical_version_verified':False})
        self.assertEqual(result['title'], '9AEE（20261021）走查测试')

    def test_frozen_old_final_title_is_not_regenerated_by_document_builder(self):
        result = self._compare_request(
            '走查测试 WBA123456789ABCDE, 2026-10-23 15:48, 2026-10-23 16:22\n'
            'WBA123456789ABCDE, 2026-10-21 14:50, 2026-10-21 15:24')
        result['title'] = '9AEE（查询时制品；20261023基线）走查测试'
        before = json.dumps(result, sort_keys=True, ensure_ascii=False)
        document = build_report_document(result)
        self.assertEqual(document['title'], result['title'])
        self.assertEqual(ET.fromstring('<document>'+document['xml']+'</document>').findtext('title'), result['title'])
        self.assertEqual(json.dumps(result, sort_keys=True, ensure_ascii=False), before)

    def test_unimplemented_artifact_name_is_four_uppercase_placeholders(self):
        self.assertEqual(
            report_title(None, "2026-10-21T15:48:00"),
            "XXXX（20261021基线）走查测试",
        )

    def test_vehicle_label_is_not_used_as_an_artifact_name(self):
        for label in ("2534", "WBA123456789ABCDE", "not an artifact/name"):
            with self.subTest(label=label):
                self.assertEqual(
                    report_title(label, "2026-10-21T15:48:00+08:00"),
                    "XXXX（20261021基线）走查测试",
                )

    def _compare_request(self, text):
        request = parse_comparison_request(text)
        return compare_performance(
            {}, {},
            baseline_metadata=request["baseline_metadata"],
            replacement_metadata=request["replacement_metadata"],
        )

    def test_default_base_is_first_input_not_earliest_date(self):
        result = self._compare_request(
            "走查测试\n"
            "WBA123456789ABCDE, 2026-10-23 15:48, 2026-10-23 16:22\n"
            "WBA123456789ABCDE, 2026-10-21 14:50, 2026-10-21 15:24"
        )
        self.assertEqual(result["title"], "XXXX（20261023基线）走查测试")

    def test_explicit_base_can_be_second_input(self):
        result = self._compare_request(
            "走查测试\n"
            "PCM更新 WBA123456789ABCDE, 2026-10-23 15:48, 2026-10-23 16:22\n"
            "Base WBA123456789ABCDE, 2026-10-21 14:50, 2026-10-21 15:24"
        )
        self.assertEqual(result["title"], "XXXX（20261021基线）走查测试")

    def test_document_title_and_xml_title_use_comparison_title(self):
        result = self._compare_request(
            "走查测试\n"
            "WBA123456789ABCDE, 2026-10-21 15:48, 2026-10-21 16:22\n"
            "WBA123456789ABCDE, 2026-10-21 14:50, 2026-10-21 15:24"
        )
        document = build_report_document(result)
        expected = "XXXX（20261021基线）走查测试"
        self.assertEqual(document["title"], expected)
        xml = ET.fromstring("<document>" + document["xml"] + "</document>")
        self.assertEqual(xml.findtext("title"), expected)


if __name__ == "__main__":
    unittest.main()
