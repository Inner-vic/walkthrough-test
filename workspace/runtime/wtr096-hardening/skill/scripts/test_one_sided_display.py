"""Regression tests: preserve one-sided measurements without inventing comparison."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import unittest
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE if HERE.name == "scripts" else HERE.parent / "scripts"
# request_model is an unchanged dependency supplied by the deployed skill.
BASE = HERE.parents[2] / "walkthrough-test-product-20261023" / "walkthrough-test" / "scripts"
sys.path.insert(0, str(Path(os.environ.get("WALKTHROUGH_ORIGINAL_SCRIPTS", BASE))))
sys.path.insert(0, str(SCRIPTS))

import feishu_report
import performance_comparison as comparison

UNPARSED = comparison.UNPARSED_LABEL
UNPARSED_XML = '<p align="center"><span text-color="red">'+UNPARSED+'</span></p>'


class OneSidedDisplayTests(unittest.TestCase):
    def test_baseline_survives_missing_replacement(self):
        pair = comparison.compare_pair(273.36, None, direction="lower")
        original = copy.deepcopy(pair)
        self.assertEqual(comparison.display_pair(pair, 2), {
            "baseline": "273.36", "replacement": UNPARSED, "tone": "neutral"})
        self.assertEqual(feishu_report._baseline_cell(pair), '<p align="center">273.36</p>')
        self.assertEqual(feishu_report._replacement_cell(pair), UNPARSED_XML)
        self.assertEqual(pair, original)

    def test_replacement_survives_missing_baseline_without_delta_or_color(self):
        pair = comparison.compare_pair(None, 10.01, direction="higher")
        # Stale comparative annotations must not turn an unpaired value green.
        pair.update(delta=1.23, tone="improved")
        self.assertEqual(comparison.display_pair(pair, 2), {
            "baseline": UNPARSED, "replacement": "10.01", "tone": "neutral"})
        self.assertEqual(feishu_report._baseline_cell(pair), UNPARSED_XML)
        self.assertEqual(feishu_report._replacement_cell(pair), '<p align="center">10.01</p>')

    def test_missing_both_stays_missing_not_zero(self):
        pair = comparison.compare_pair(None, None, direction="lower")
        self.assertEqual(comparison.display_pair(pair, 2), {
            "baseline": UNPARSED, "replacement": UNPARSED, "tone": "neutral"})
        self.assertEqual(feishu_report._replacement_cell(pair), UNPARSED_XML)

    def test_explicit_parsed_empty_uses_ascii_dash_and_keeps_known_side(self):
        pair = comparison.compare_pair(1, None, direction="lower")
        pair.update(baseline_status="value", replacement_status="empty")
        self.assertEqual(comparison.display_pair(pair, 2), {
            "baseline":"1.00", "replacement":"-", "tone":"neutral"})
        self.assertEqual(feishu_report._replacement_cell(pair), '<p align="center">-</p>')
        self.assertEqual(feishu_report._baseline_cell(pair), '<p align="center">1</p>')

    def test_unknown_and_error_statuses_do_not_expose_stale_values(self):
        for status in (None, "failed", "invalid", "partial", "unknown"):
            with self.subTest(status=status):
                pair = {"status": status, "baseline": 10, "replacement": 20,
                        "delta": 10, "tone": "regressed"}
                self.assertEqual(comparison.display_pair(pair, 2), {
                    "baseline": UNPARSED, "replacement": UNPARSED, "tone": "neutral"})
                self.assertEqual(feishu_report._baseline_cell(pair), UNPARSED_XML)
                self.assertEqual(feishu_report._replacement_cell(pair), UNPARSED_XML)

    def test_invalid_side_never_creates_a_delta(self):
        for invalid in (None, float("nan"), float("inf"), -1, True, "invalid"):
            with self.subTest(invalid=invalid):
                pair = {"status": "ok", "baseline": invalid, "replacement": 0,
                        "delta": -1, "tone": "improved"}
                self.assertEqual(comparison.display_pair(pair, 2), {
                    "baseline": UNPARSED, "replacement": "0.00", "tone": "neutral"})
                self.assertEqual(feishu_report._replacement_cell(pair), '<p align="center">0</p>')

    def test_invalid_delta_is_omitted_while_valid_values_remain(self):
        for invalid in (None, float("nan"), float("inf"), True, "-2"):
            with self.subTest(invalid=invalid):
                pair = {"status": "ok", "baseline": 10, "replacement": 8,
                        "delta": invalid, "tone": "improved"}
                self.assertEqual(comparison.display_pair(pair, 2), {
                    "baseline": "10.00", "replacement": "8.00", "tone": "neutral"})
                self.assertEqual(feishu_report._replacement_cell(pair), '<p align="center">8</p>')

    def test_complete_pairs_keep_metric_direction_and_format(self):
        for before, after, direction, delta, tone, color in (
            (10, 8, "lower", "-2", "improved", "green"),
            (10, 8, "higher", "-2", "regressed", "red"),
            (8, 10, "lower", "+2", "regressed", "red"),
            (8, 10, "higher", "+2", "improved", "green"),
        ):
            with self.subTest(direction=direction, before=before):
                pair = comparison.compare_pair(before, after, direction=direction)
                self.assertEqual(comparison.display_pair(pair, 2), {
                    "baseline": f"{before:.2f}",
                    "replacement": f"{after:.2f} ({float(delta):+.2f})", "tone": tone})
                self.assertEqual(feishu_report._replacement_cell(pair),
                    f'<p align="center">{after} <span text-color="{color}">({delta})</span></p>')

    def test_unchanged_and_zero_values_have_no_improvement_color(self):
        pair = comparison.compare_pair(0, 0, direction="lower")
        pair["tone"] = "improved"
        self.assertEqual(comparison.display_pair(pair, 2), {
            "baseline": "0.00", "replacement": "0.00 (+0.00)", "tone": "neutral"})
        self.assertEqual(feishu_report._replacement_cell(pair), '<p align="center">0 (0)</p>')

    def test_full_comparison_and_document_preserve_both_one_sided_directions(self):
        baseline_sheet = {"metrics": {"slow_driver": {
            "p99_ms": 273.36, "status": "ok", "frame_rate_status": "missing"}}}
        replacement_sheet = {"metrics": {"slow_driver": {
            "frame_rate_hz": 4.99, "frame_rate_status": "ok", "status": "missing"}}}
        baseline_metadata = {"vin": "SYNTHETIC", "start": "2026-01-02T10:00:00", "end": "2026-01-02T10:01:00"}
        replacement_metadata = {"vin": "SYNTHETIC", "start": "2026-01-02T10:02:00", "end": "2026-01-02T10:03:00"}
        result = comparison.compare_performance(baseline_sheet, replacement_sheet,
            baseline_metadata=baseline_metadata, replacement_metadata=replacement_metadata)
        # Even when no complete pairs exist, each known side remains reviewable.
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["table"]["rows"][0][1:5], ["273.36", UNPARSED, UNPARSED, "4.99"])
        metrics = result["comparisons"]["slow_driver"]["metrics"]
        self.assertEqual(metrics["p99_infer_delay_ms"]["baseline"], 273.36)
        self.assertIsNone(metrics["p99_infer_delay_ms"]["delta"])
        self.assertEqual(metrics["frame_rate_hz"]["replacement"], 4.99)
        xml = feishu_report.build_report_document(result)["xml"]
        root = ET.fromstring("<report>" + xml + "</report>")
        cells = root.findall("./table/tbody/tr")[0].findall("td")
        self.assertEqual(["".join(cell.itertext()) for cell in cells[1:5]], ["273.36", UNPARSED, UNPARSED, "4.99"])
        # Only the unparsed cells are red; known one-sided measurements have
        # no comparison color or delta.
        self.assertFalse(cells[1].findall(".//span"))
        self.assertFalse(cells[4].findall(".//span"))
        self.assertNotIn("(", "".join(cells[1].itertext()))
        self.assertNotIn("(", "".join(cells[4].itertext()))
        self.assertEqual(xml.count(UNPARSED),46)


if __name__ == "__main__":
    unittest.main()
