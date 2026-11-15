"""Offline exporter tests; synthetic inputs contain no service addresses or VINs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import openpyxl
import full_workbook as workbook


def fixture():
    rules = {"drive": {
        "header_delay": {"soc_type": "soc", "calculate_type": "", "input": "=literal-expression"},
        "vlamodel_fps": {"soc_type": "soc", "calculate_type": ""},
    }, "parking": {
        "vla_parking_header_delay": {"soc_type": "container", "calculate_type": ""},
        "vlaparkingdlm_fps": {"soc_type": "container", "calculate_type": ""},
    }, "other": {
        "missing": {"soc_type": "XVM_", "calculate_type": ""},
        "conditional": {"soc_type": "XVM_", "calculate_type": "emb_type"},
        "trafficlight_fps": {}, "TrafficLight_FPS": {},
    }}
    report = {"status": "partial", "metadata": {"time_range": "synthetic"},
              "analysis_scope": {"complete_logs_verified": False},
              "skipped_records": [{"metric": "conditional", "reason": "not recorded in old capture"}],
              "metrics": {
                  "slow_driver": {"p99_ms": 10.5, "frame_rate_hz": 20.0},
                  "fast_driver": {"p99_ms": None, "frame_rate_hz": 21.0},
                  "CNOA": {"p99_ms": 11.5, "frame_rate_hz": 22.0},
                  "parking": {"p99_ms": 12.5, "frame_rate_hz": 23.0},
              }, "statistics": [
                  {"metric": "header_delay", "statistics": {"P99_0": 10.5, "P99_4": None, "P99_5": 11.5,
                    "zero": 0, "empty": "", "boolean": False, "placeholder": "-", "nested": {"none": None},
                    "precise": 1.2345678901234567, "big": 1234567890123456789, "negative_zero": -0.0}},
                  {"metric": "vlamodel_fps", "statistics": {"mean_0": 20.0, "mean_4": 21.0, "mean_5": 22.0}},
                  {"metric": "vla_parking_header_delay", "statistics": {"P99_1": 12.5}},
                  {"metric": "vlaparkingdlm_fps", "statistics": {"mean_1": 23.0}},
                  {"metric": "trafficlight_fps", "statistics": {"mean": 1.0}},
                  {"metric": "TrafficLight_FPS", "statistics": {"mean": 2.0}},
              ]}
    return report, rules


class FullWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="full-workbook-test-")
        self.root = Path(self.temp.name)
        self.path = self.root / "synthetic.xlsx"
        self.report, self.rules = fixture()
        self.kwargs = {"request": {"start": "synthetic-start", "end": "synthetic-end"},
                       "role": "Base", "environment": "testtwo", "source_hashes": {"archives": "a" * 64},
                       "scope": {"available_log_classes": ["synthetic"], "all_logs_verified": False}}

    def tearDown(self):
        self.temp.cleanup()

    def export(self, **extra):
        return workbook.export_full_workbook(self.report, self.rules, self.path, **self.kwargs, **extra)

    def rows(self, name):
        book = openpyxl.load_workbook(self.path, data_only=False)
        try:
            return list(book[name].values)
        finally:
            book.close()

    def test_every_field_and_semantics_round_trip(self):
        manifest = self.export()
        raw = self.rows("完整原始字段")
        captured = {}
        for record_index, theme, metric, field, display, kind, exact in raw[1:]:
            captured.setdefault(metric, {})[field] = json.loads(exact)
        self.assertEqual(captured, {row["metric"]: row["statistics"] for row in self.report["statistics"]})
        values = {row[3]: row for row in raw[1:] if row[2] == "header_delay"}
        self.assertEqual(values["empty"][5:], ("string", '""'))
        self.assertEqual(values["P99_4"][5:], ("null", "null"))
        self.assertEqual(values["zero"][4], 0)
        self.assertIs(values["boolean"][4], False)
        self.assertEqual(values["precise"][4], "1.2345678901234567")
        self.assertEqual(values["big"][4], "1234567890123456789")
        self.assertEqual(values["negative_zero"][4], "-0.0")
        self.assertEqual(manifest["validation"]["status"], "ok")
        self.assertEqual(manifest["statistics_fields"], sum(len(r["statistics"]) for r in self.report["statistics"]))

    def test_manifest_source_hashes_and_scope(self):
        manifest = self.export()
        self.assertEqual(manifest["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(manifest["bytes"], self.path.stat().st_size)
        self.assertEqual(manifest["request"], self.kwargs["request"])
        self.assertEqual(manifest["request_sha256"], workbook._digest(self.kwargs["request"]))
        self.assertEqual(manifest["source_hashes"], self.kwargs["source_hashes"])
        self.assertEqual(manifest["scope"]["all_vehicle_log_types_obtained"], "not_verified_by_exporter")
        self.assertFalse(manifest["scope"]["native_execution_reperformed"])
        self.assertFalse(manifest["scope"]["original_seven_sheet_template_reproduced"])
        disk = json.loads(self.path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(disk, manifest)
        metadata = self.rows("元数据")
        self.assertTrue(any("skipped_records" in row[1] for row in metadata[1:]))
        self.assertTrue(any("analysis_scope" in row[1] for row in metadata[1:]))

    def test_missing_and_conditional_without_fabricated_zero(self):
        manifest = self.export()
        audits = {row[1]: row for row in self.rows("规则审计")[1:]}
        self.assertEqual(audits["missing"][2], "not_captured")
        self.assertEqual(audits["conditional"][2], "conditional_not_captured")
        self.assertIn("不能确认", audits["conditional"][4])
        self.assertFalse(audits["conditional"][5])
        self.assertEqual(manifest["scope"]["supplied_rules"], 8)
        self.assertEqual(manifest["statistics_records"], 6)
        native = self.rows("四场景统计")
        fast_p99 = next(row for row in native[1:] if row[0] == "fast_driver" and row[3] == "P99 (ms)")
        self.assertTrue(fast_p99[6])
        self.assertIsNone(fast_p99[7])
        self.assertEqual(fast_p99[8:10], ("null", "null"))

    def test_explicit_native_statuses_and_metadata_preserved(self):
        self.report["statistics"] += [
            {"theme": "other", "metric": "missing", "statistics": {}, "status": "missing",
             "rule": self.rules["other"]["missing"], "native_publish": True, "diagnostic": "no source"},
            {"theme": "other", "metric": "conditional", "statistics": {}, "status": "conditional_skip",
             "rule": self.rules["other"]["conditional"], "native_publish": False, "skip_reason": "explicit hardware condition"},
        ]
        self.report["statistics"][0].update(status="available", theme="drive", native_publish=True,
                                             rule=self.rules["drive"]["header_delay"])
        manifest = self.export()
        audits = {row[1]: row for row in self.rows("规则审计")[1:]}
        self.assertEqual(audits["conditional"][2], "conditional_skip")
        self.assertEqual(audits["conditional"][4], "explicit hardware condition")
        self.assertIs(audits["conditional"][9], False)
        self.assertIs(audits["conditional"][10], True)
        self.assertEqual(audits["missing"][2], "missing")
        self.assertEqual(audits["header_delay"][2], "available")
        self.assertEqual(manifest["rule_status_counts"]["conditional_skip"], 1)
        self.assertEqual(manifest["statistics_records"], 8)

    def test_formula_injection_is_literal(self):
        self.report["statistics"][0]["statistics"].update({
            "=dangerous-header": "=1+1", "+prefix": "+cmd", "@prefix": "@SUM(A1)",
            "tab": "\t=1+1", "link": "https://example.invalid/literal", "error": "#DIV/0!"})
        self.export()
        book = openpyxl.load_workbook(self.path, data_only=False)
        try:
            values = {cell.value for sheet in book for row in sheet for cell in row if isinstance(cell.value, str)}
            self.assertIn("=1+1", values)
            self.assertIn("=dangerous-header", values)
            self.assertIn("#DIV/0!", values)
            for sheet in book:
                for row in sheet:
                    for cell in row:
                        self.assertNotIn(cell.data_type, ("f", "e"))
                        self.assertIsNone(cell.hyperlink)
        finally:
            book.close()
        with zipfile.ZipFile(self.path) as archive:
            self.assertFalse(any("externalLinks" in name for name in archive.namelist()))

    def test_safe_names_and_filter_freeze(self):
        themes = ["[]:*?/\\'", "a" * 80, "A" * 80, "😀" * 30, "总览", "History"]
        names = workbook.safe_sheet_names(themes)
        self.assertEqual(len({name.casefold() for name in names.values()}), len(themes))
        for name in names.values():
            self.assertLessEqual(len(name.encode("utf-16-le")) // 2, 31)
            self.assertFalse(any(char in name for char in "[]:*?/\\"))
        self.export()
        book = openpyxl.load_workbook(self.path)
        try:
            for sheet in book:
                self.assertIsNotNone(sheet.freeze_panes)
                self.assertIsNotNone(sheet.auto_filter.ref)
        finally:
            book.close()

    def test_case_sensitive_native_names_and_unmapped_retained(self):
        self.report["statistics"].append({"metric": "new_metric", "statistics": {"new_field": 123}})
        manifest = self.export()
        values = {(row[2], row[3]): json.loads(row[6]) for row in self.rows("完整原始字段")[1:]}
        self.assertEqual(values[("trafficlight_fps", "mean")], 1)
        self.assertEqual(values[("TrafficLight_FPS", "mean")], 2)
        self.assertEqual(values[("new_metric", "new_field")], 123)
        self.assertEqual(manifest["unmapped_records"], 1)

    def test_ambiguous_and_duplicate_records_rejected(self):
        self.rules["other"]["header_delay"] = {}
        with self.assertRaisesRegex(workbook.WorkbookExportError, "多个主题"):
            self.export()
        del self.rules["other"]["header_delay"]
        self.report["statistics"].append(copy.deepcopy(self.report["statistics"][0]))
        with self.assertRaisesRegex(workbook.WorkbookExportError, "重复"):
            self.export()
        self.assertFalse(self.path.exists())

    def test_limits_and_invalid_xml_rejected_without_output(self):
        for invalid in ("x" * 32768, "😀" * 16384, "bad\x01", "bad\ud800"):
            with self.subTest(invalid_length=len(invalid)):
                self.report["statistics"][0]["statistics"]["invalid"] = invalid
                with self.assertRaises(workbook.WorkbookExportError):
                    self.export()
                self.assertFalse(self.path.exists())
        del self.report["statistics"][0]["statistics"]["invalid"]
        with patch.object(workbook, "MAX_ROWS", 2), self.assertRaisesRegex(workbook.WorkbookExportError, "行列限制"):
            self.export()
        with patch.object(workbook, "MAX_COLUMNS", 2), self.assertRaisesRegex(workbook.WorkbookExportError, "行列限制"):
            self.export()

    def test_nonfinite_and_bad_provenance_rejected(self):
        self.report["statistics"][0]["statistics"]["invalid"] = float("nan")
        with self.assertRaises(workbook.WorkbookExportError):
            self.export()
        del self.report["statistics"][0]["statistics"]["invalid"]
        self.kwargs["source_hashes"] = {"archives": "not-a-sha"}
        with self.assertRaisesRegex(workbook.WorkbookExportError, "SHA256"):
            self.export()
        self.kwargs["source_hashes"] = {"archives": "b" * 64}
        self.kwargs["environment"] = "per_window"
        with self.assertRaisesRegex(workbook.WorkbookExportError, "实际 provider"):
            self.export()

    def test_file_inputs_duplicate_json_keys_and_byte_hash(self):
        report_path = self.root / "report.json"
        rules_path = self.root / "rules.json"
        report_path.write_text(json.dumps(self.report, ensure_ascii=False), encoding="utf-8")
        rules_path.write_text(json.dumps(self.rules, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = workbook.export_full_workbook(report_path, rules_path, self.path, **self.kwargs)
        self.assertEqual(manifest["rules_sha256"], hashlib.sha256(rules_path.read_bytes()).hexdigest())
        self.assertEqual(manifest["rules_sha256_method"], "input_file_bytes")
        result = workbook.verify_full_workbook(self.path, report_path, rules_path, **self.kwargs)
        self.assertEqual(result["status"], "ok")
        report_path.write_text('{"statistics":[],"statistics":[]}', encoding="utf-8")
        with self.assertRaisesRegex(workbook.WorkbookExportError, "重复键"):
            workbook.export_full_workbook(report_path, rules_path, self.root / "bad.xlsx", **self.kwargs)

    def test_readback_detects_tampering_and_no_silent_overwrite(self):
        self.export()
        original_sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(workbook.WorkbookExportError, "输出已存在"):
            self.export()
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), original_sha)
        book = openpyxl.load_workbook(self.path)
        book["完整原始字段"]["G2"] = "999"
        book.save(self.path)
        book.close()
        with self.assertRaisesRegex(workbook.WorkbookExportError, "读回不一致"):
            workbook.verify_full_workbook(self.path, self.report, self.rules, **self.kwargs)

    def test_readback_failure_does_not_publish(self):
        with patch.object(workbook, "_verify", side_effect=workbook.WorkbookExportError("readback failed")):
            with self.assertRaisesRegex(workbook.WorkbookExportError, "readback failed"):
                self.export()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_suffix(".manifest.json").exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_statistics_only_input_is_supported(self):
        manifest = workbook.export_full_workbook(self.report["statistics"], self.rules, self.path, **self.kwargs)
        self.assertEqual(manifest["statistics_records"], 6)
        self.assertTrue(all(row[10] is False for row in self.rows("四场景统计")[1:]))

    def test_pcm_cpu_all_scenarios_fields_and_coverage_preserved(self):
        self.report["pcm_result"] = {
            "status": "partial", "metrics": {
                "slow_driver": {"label": "低速人驾", "perception_state": 0,
                    "pcm_p99_infer_delay_ms": {"p99": 170.0, "count": 20},
                    "pcm_frame_rate_hz": {"mean": 10.0, "count": 5},
                    "pcm_cpu_percent": {"mean": 4.2, "count": 6},
                    "system_cpu_percent": {"mean": 60.2, "count": 6},
                    "extra_metric": {"new_field": "=literal"}},
                "parking": {"label": "泊车", "pcm_p99_infer_delay_ms": {"p99": None, "count": 0}},
            }, "coverage": {"files_read": 2, "gaps": [None, ""]},
            "diagnostics": {"missing": ["parking fps"], "zero_valid": False},
        }
        manifest = self.export()
        rows = self.rows("PCM与CPU")[1:]
        values = {row[8]: json.loads(row[7]) for row in rows if row[4] and row[7] is not None}
        self.assertEqual(values["/metrics/slow_driver/pcm_p99_infer_delay_ms/p99"], 170)
        self.assertEqual(values["/metrics/slow_driver/extra_metric/new_field"], "=literal")
        self.assertIsNone(values["/metrics/parking/pcm_p99_infer_delay_ms/p99"])
        self.assertEqual(values["/coverage/gaps/1"], "")
        self.assertIsNone(values["/coverage/gaps/0"])
        self.assertIs(values["/diagnostics/zero_valid"], False)
        missing = next(row for row in rows if row[8] == "/metrics/fast_driver/pcm_cpu_percent")
        self.assertIs(missing[4], False)
        self.assertEqual(missing[6], "missing_field")
        self.assertTrue(manifest["pcm_result_included"])
        # Adding PCM does not remove a single AD field.
        self.assertEqual(manifest["statistics_fields"], sum(len(r["statistics"]) for r in self.report["statistics"]))

    def test_pcm_failed_result_preserves_error_without_zero_success(self):
        self.report["pcm_result"] = {"status": "failed", "diagnostics": {"kind": "interface_error"}, "metrics": {}}
        self.export()
        rows = self.rows("PCM与CPU")[1:]
        self.assertEqual(sum(row[6] == "missing_field" for row in rows), 16)
        values = {row[8]: json.loads(row[7]) for row in rows if row[4] and row[7] is not None}
        self.assertEqual(values["/status"], "failed")
        self.assertEqual(values["/diagnostics/kind"], "interface_error")


if __name__ == "__main__":
    unittest.main()
