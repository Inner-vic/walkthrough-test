from __future__ import annotations
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from native_metric_diagnostics import (MetricDiagnosticsError, _EXPECTED,
                                      attach_metric_input_diagnostics,
                                      build_metric_input_diagnostics)


class MetricDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.window = "20261023150000/20261023153500"
        self.epochs = [datetime(2026, 10, 23, 15, minute, tzinfo=timezone(timedelta(hours=8))).timestamp()
                       for minute in (0, 35)]

    def report(self, metric, lines, *, status="missing"):
        theme, soc, tag, function, expression, ps, _ = _EXPECTED[metric]
        raw = ("\n".join(lines) + "\n").encode()
        path = self.root / "log" / soc / "synthetic.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        rule = {"soc_type": soc, "log_tag": tag, "func": function, "input": expression,
                "ps_type": ps, "calculate_type": ""}
        return {"metadata": {"time_range": self.window}, "status": "partial",
                "statistics": [{"theme": theme, "metric": metric, "status": status, "data_status": status,
                    "rule": rule, "statistics": {"mean": "-", "P99": "-"}, "diagnostic": "native missing"}],
                "runtime_execution": {"status": "verified", "request_time_range": self.window,
                    "expected_epoch_range": self.epochs, "flat_log_root": str(self.root),
                    "extractions": [{"theme": theme, "record_epoch_range": self.epochs,
                        "actual_paths": {path.name: path.relative_to(self.root).as_posix()},
                        "actual_members": {path.name: {"sha256": hashlib.sha256(raw).hexdigest(),
                                                       "bytes": len(raw), "source": "synthetic_source"}}}]}}

    def evidence(self, report, metric):
        return build_metric_input_diagnostics(report)["metrics"][metric]

    def replace_raw(self, report, raw):
        extraction = report["runtime_execution"]["extractions"][0]
        name = next(iter(extraction["actual_members"]))
        path = self.root / extraction["actual_paths"][name]
        path.write_bytes(raw)
        extraction["actual_members"][name].update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        return path

    def test_recallcheck_is_not_three_native_statistics(self):
        lines = ["10-23 15:10:00.001 100 101 W profiling.h:60: [RecallCheck] ParkingSceneExecutorNode::OnImage start",
                 "10-23 15:10:00.002 100 101 W profiling.h:66: [RecallCheck] ParkingSceneExecutorNode::OnImage end, cost: 0 ms, header: 0"]
        for metric in _EXPECTED:
            if metric.startswith("bevparking_"):
                with self.subTest(metric=metric):
                    row = self.evidence(self.report(metric, lines), metric)
                    self.assertEqual(row["status"], "explained")
                    self.assertEqual(row["counts"]["in_window_tag_hits"], 2)
                    self.assertEqual(row["counts"]["configured_numeric_values"], 0)
                    self.assertEqual(row["field_counts_in_window"], {"9": 1, "14": 1})

    def test_budget_warning_cannot_become_safety_fusion_delay(self):
        metric = "lidar_safety_fusion_delay"
        row = self.evidence(self.report(metric, ["10-23 15:10:00.001 100 101 E [EDSG]: graph: synthetic_lidar, context idx: 2, startTime: 1000, endTime: 1001, running time: 172 ms, exceed max exec time: 100 ms -> graph_context.cc:638"]), metric)
        self.assertEqual(row["status"], "explained")
        self.assertEqual(row["counts"]["configured_numeric_values"], 0)
        self.assertEqual(row["shapes_in_window"], {"graph_budget_warning_not_safety_fusion_statistic": 1})

    def test_dds_type_registration_not_prediction_count(self):
        text = "10-23 15:10:00.001 100 101 W VBSLOG_DRTRACEPOINT_MANAGER: Info [1] Reg topic idl, topic id: 41, name: synthetic, idl type: 31,dds_parking_obstacle::Obstacles, define: struct dds_base::Point {"
        for metric in ("prediction_obs", "prediction_obs_dynamic"):
            with self.subTest(metric=metric):
                row = self.evidence(self.report(metric, [text]), metric)
                self.assertEqual(row["status"], "explained")
                self.assertEqual(row["shapes_in_window"], {"dds_type_registration_not_obstacle_count": 1})

    def test_unknown_layout_stays_unresolved(self):
        metric = "bevparking_scene_executor_delay"
        row = self.evidence(self.report(metric, ["10-23 15:10:00.001 100 101 W new-format: ParkingSceneExecutorNode::OnImage renamed fields"]), metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "unrecognized_input_layout_unresolved")

    def test_numeric_configured_input_stays_unresolved_when_native_missing(self):
        metric = "lidar_safety_fusion_delay"
        text = "10-23 15:10:00.001 100 101 W synthetic: a b c exec time: 12.5ms"
        row = self.evidence(self.report(metric, [text]), metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "numeric_input_exists_native_missing_unresolved")
        self.assertEqual(row["counts"]["configured_numeric_values"], 1)

    def test_rule_change_not_auto_accepted(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        report["statistics"][0]["rule"]["input"] = "a_new_expression"
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "rule_contract_changed")

    def test_window_excludes_other_time_and_keeps_closed_end(self):
        metric = "prediction_obs"
        report = self.report(metric, ["10-23 14:59:59.999 100 101 W synthetic: obstacle::7",
                                       "10-23 15:35:00.000 100 101 W synthetic: obstacle::8",
                                       "10-23 15:35:00.001 100 101 W synthetic: obstacle::9"])
        row = self.evidence(report, metric)
        self.assertEqual(row["counts"]["raw_tag_hits"], 3)
        self.assertEqual(row["counts"]["in_window_tag_hits"], 1)
        self.assertEqual(row["counts"]["configured_numeric_values"], 1)

    def test_no_matching_tag_and_invalid_time_are_separate(self):
        metric = "prediction_obs"
        no_tag = self.evidence(self.report(metric, ["10-23 15:10:00.000 100 101 W unrelated"]), metric)
        self.assertEqual(no_tag["reason"], "no_tag_matches_in_selected_inputs")
        invalid = self.evidence(self.report(metric, ["typedef obstacle::Thing"]), metric)
        self.assertEqual(invalid["status"], "unresolved")
        self.assertEqual(invalid["reason"], "unrecognized_untimed_input_layout_unresolved")
        self.assertEqual(invalid["counts"]["invalid_timestamp_tag_hits"], 1)

    def test_untimed_idl_definition_is_identified_without_inventing_time(self):
        metric = "prediction_obs"
        report = self.report(metric, ["struct dds_parking_obstacle::Obstacle {",
                                       "\tsequence<dds_parking_obstacle::Obstacle > obstacles;"])
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "explained")
        self.assertEqual(row["counts"]["in_window_tag_hits"], 0)
        self.assertEqual(row["counts"]["invalid_timestamp_tag_hits"], 2)
        self.assertEqual(row["untimed_shapes"], {"dds_idl_definition_without_measurement_timestamp": 2})

    def test_unrecognized_full_year_timestamp_remains_unresolved(self):
        metric = "prediction_obs"
        report = self.report(metric, ["2026-10-23 15:10:00.000 100 101 W synthetic: obstacle::7"])
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "unrecognized_untimed_input_layout_unresolved")
        self.assertEqual(row["counts"]["invalid_timestamp_tag_hits"], 1)

    def test_unknown_untimed_line_cannot_hide_behind_known_in_window_shape(self):
        metric = "prediction_obs"
        report = self.report(metric, [
            "10-23 15:10:00.001 100 101 W VBSLOG_DRTRACEPOINT_MANAGER: Info [1] Reg topic idl, type: dds_parking_obstacle::Obstacles, define: struct Type {",
            "new unknown prefix obstacle::7",
        ])
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["counts"]["in_window_tag_hits"], 1)
        self.assertEqual(row["counts"]["invalid_timestamp_tag_hits"], 1)

    def test_attach_preserves_all_preexisting_values_and_no_raw_lines(self):
        metric = "prediction_obs"
        report = self.report(metric, ["10-23 15:10:00.000 100 101 W synthetic: obstacle::7"])
        original = copy.deepcopy(report)
        attached = attach_metric_input_diagnostics(report)
        self.assertEqual(report, original)
        evidence = attached.pop("metric_input_diagnostics")
        self.assertFalse(evidence["statistical_values_or_rules_modified"])
        self.assertFalse(evidence["counts_are_native_final_samples"])
        self.assertNotIn("text", json.dumps(evidence))
        attached["statistics"][0].pop("input_diagnostic")
        self.assertEqual(attached, original)

    def test_available_metric_is_never_reclassified(self):
        metric = "prediction_obs"
        report = self.report(metric, [], status="available")
        evidence = build_metric_input_diagnostics(report)
        self.assertEqual(evidence["supported_missing_rules_checked"], 0)
        self.assertEqual(evidence["metrics"], {})

    def test_invalid_utf8_target_line_is_unresolved_and_original_values_survive(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        raw = b"10-23 15:10:00.000 100 101 W synthetic: obstacle::7\xff\n"
        self.replace_raw(report, raw)
        original = copy.deepcopy(report)
        output = attach_metric_input_diagnostics(report)
        row = output["metric_input_diagnostics"]["metrics"][metric]
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "invalid_encoding_in_target_lines_unresolved")
        self.assertEqual(row["counts"]["invalid_encoding_tag_hits"], 1)
        self.assertFalse(row["scan_complete"])
        self.assertEqual(row["counts"]["configured_numeric_values"], 0)
        self.assertEqual(row["sample_line_sha256"]["invalid_encoding"]["sha256"], hashlib.sha256(raw.rstrip(b"\n")).hexdigest())
        output.pop("metric_input_diagnostics")
        output["statistics"][0].pop("input_diagnostic")
        self.assertEqual(output, original)
        self.assertEqual(report, original)

    def test_known_valid_shape_cannot_hide_invalid_utf8_target_line(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        good = b"10-23 15:10:00.001 100 101 W VBSLOG_DRTRACEPOINT_MANAGER: Info [1] Reg topic idl, type: dds_parking_obstacle::Obstacles, define: struct Type {\n"
        bad = b"10-23 15:10:00.002 100 101 W synthetic: obstacle::unknown\xff\n"
        self.replace_raw(report, good + bad)
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "invalid_encoding_in_target_lines_unresolved")
        self.assertEqual(row["counts"]["raw_tag_hits"], 2)
        self.assertEqual(row["counts"]["in_window_tag_hits"], 1)
        self.assertEqual(row["counts"]["invalid_encoding_tag_hits"], 1)
        self.assertEqual(row["shapes_in_window"], {"dds_type_registration_not_obstacle_count": 1})

    def test_permission_error_is_incomplete_diagnostic_not_lost_statistics(self):
        metric = "prediction_obs"
        report = self.report(metric, ["10-23 15:10:00.000 100 101 W synthetic: obstacle::7"])
        original = copy.deepcopy(report)
        with patch.object(Path, "read_bytes", side_effect=PermissionError(13, "synthetic denied")):
            output = attach_metric_input_diagnostics(report)
        row = output["metric_input_diagnostics"]["metrics"][metric]
        self.assertFalse(row["scan_complete"])
        self.assertEqual(row["status"], "unresolved")
        self.assertEqual(row["reason"], "selected_input_unavailable_unresolved")
        output.pop("metric_input_diagnostics")
        output["statistics"][0].pop("input_diagnostic")
        self.assertEqual(output, original)

    def test_missing_member_is_incomplete_diagnostic_not_no_samples(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        path = self.root / "log" / "FSDA_M100_ULTRA" / "synthetic.log"
        path.unlink()
        row = self.evidence(report, metric)
        self.assertEqual(row["status"], "unresolved")
        self.assertFalse(row["scan_complete"])
        self.assertEqual(row["reason"], "selected_input_unavailable_unresolved")

    def test_non_regular_member_remains_hard_error(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        path = self.root / "log" / "FSDA_M100_ULTRA" / "synthetic.log"
        path.unlink()
        path.mkdir()
        with self.assertRaises(MetricDiagnosticsError):
            build_metric_input_diagnostics(report)

    def test_member_tampering_rejected(self):
        metric = "prediction_obs"
        report = self.report(metric, ["10-23 15:10:00.000 100 101 W synthetic: obstacle::7"])
        path = self.root / "log" / "FSDA_M100_ULTRA" / "synthetic.log"
        path.write_text("changed", encoding="utf-8")
        with self.assertRaises(MetricDiagnosticsError):
            build_metric_input_diagnostics(report)

    def test_path_escape_rejected(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        report["runtime_execution"]["extractions"][0]["actual_paths"]["synthetic.log"] = "log/../../outside.log"
        with self.assertRaises(MetricDiagnosticsError):
            build_metric_input_diagnostics(report)

    def test_runtime_window_mismatch_rejected(self):
        metric = "prediction_obs"
        report = self.report(metric, [])
        report["runtime_execution"]["expected_epoch_range"][0] += 1
        with self.assertRaises(MetricDiagnosticsError):
            build_metric_input_diagnostics(report)


if __name__ == "__main__":
    unittest.main()
