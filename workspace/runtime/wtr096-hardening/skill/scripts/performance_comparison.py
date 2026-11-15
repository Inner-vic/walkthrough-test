#!/usr/bin/env python3
"""Combine Sheets AD metrics with per-state PCM performance logs."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from request_model import InputError, SCENARIOS, comparison_context


METRIC_SPECS = (
    ("p99_infer_delay_ms", "AD P99 Infer Delay", "p99_ms", None, "mean", "lower", 2),
    ("frame_rate_hz", "AD Frame Rate", "frame_rate_hz", None, "mean", "higher", 2),
    ("system_cpu_percent", "Sys CPU Usage", None, "system_cpu_percent", "mean", "lower", 2),
    ("pcm_p99_infer_delay_ms", "PCM P99 Infer Delay", None, "pcm_p99_infer_delay_ms", "p99", "lower", 2),
    ("pcm_frame_rate_hz", "PCM Frame Rate", None, "pcm_frame_rate_hz", "mean", "higher", 2),
    ("pcm_cpu_percent", "PCM CPU Usage", None, "pcm_cpu_percent", "mean", "lower", 2),
)
REPORT_ORDER = ("slow_driver", "fast_driver", "parking", "CNOA")
SCENARIO_LABELS = {key: label for key, label, _, _ in SCENARIOS}
SCENARIO_LABELS["CNOA"] = "CNOA"
UNPARSED_LABEL = "字段未解析，需排查"


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def sheet_value(result, scenario, field):
    row = result.get("metrics", {}).get(scenario, {})
    value = finite_number(row.get(field))
    status_field = "status" if field == "p99_ms" else "frame_rate_status"
    return value if row.get(status_field) == "ok" else None


def state_value(result, scenario, field, statistic="mean"):
    if not isinstance(result, dict):
        return None, 0
    metric = result.get("metrics", {}).get(scenario, {}).get(field, {})
    if not isinstance(metric, dict):
        return None, 0
    value = finite_number(metric.get(statistic))
    count = metric.get("count")
    count = count if type(count) is int and count >= 0 else 0
    return (value if count else None), count


def _declared_presence(result, field):
    presence = result.get("field_presence")
    if not isinstance(presence, dict):
        return None
    item = presence.get(field)
    if not isinstance(item, dict):
        return False
    count = item.get("observations")
    return item.get("status") == "parsed" and type(count) is int and count > 0


def sheet_field_status(result, scenario, field):
    """A generated metric slot or a matched tag alone is not extraction evidence."""
    if sheet_value(result, scenario, field) is not None:
        return "value"
    target = ("vla_parking_header_delay" if scenario == "parking" else "header_delay") if field == "p99_ms" else (
        "vlaparkingdlm_fps" if scenario == "parking" else "vlamodel_fps")
    known = _declared_presence(result, target)
    if known is not None:
        return "empty" if known else "unparsed"
    # Legacy results may prove recognition with a valid statistic of this exact
    # target in another state. Pre-created keys and record counts cannot do so.
    source_key = "p99_source" if field == "p99_ms" else "fps_source"
    for key, row in result.get("metrics", {}).items():
        metric = str(row.get(source_key, {}).get("metric", "")).casefold()
        if metric == target and sheet_value(result, key, field) is not None:
            return "empty"
    prefix = "P99_" if field == "p99_ms" else "mean_"
    for record in result.get("statistics", []):
        if str(record.get("metric", "")).casefold() == target and any(
                name.startswith(prefix) and finite_number(value) is not None
                for name, value in record.get("statistics", {}).items()):
            return "empty"
    return "unparsed"


def state_field_status(result, scenario, field, statistic="mean"):
    if not isinstance(result, dict):
        return "unparsed"
    if state_value(result, scenario, field, statistic)[0] is not None:
        return "value"
    if result.get("error"):
        return "unparsed"
    known = _declared_presence(result, field)
    if known is not None:
        return "empty" if known else "unparsed"
    for key in result.get("metrics", {}):
        if state_value(result, key, field, statistic)[0] is not None:
            return "empty"
    counter = {"system_cpu_percent": "complete_cpu_samples",
               "pcm_cpu_percent": "complete_cpu_samples",
               "pcm_p99_infer_delay_ms": "latency_samples",
               "pcm_frame_rate_hz": "fps_samples"}.get(field)
    count = result.get("coverage", {}).get(counter)
    return "empty" if type(count) is int and count > 0 else "unparsed"


def compare_pair(before, after, *, direction):
    pair = {"baseline": before, "replacement": after, "delta": None, "status": "missing", "tone": "neutral"}
    if before is None or after is None:
        return pair
    delta = float(Decimal(str(after)) - Decimal(str(before)))
    pair.update(delta=delta, status="ok")
    if delta:
        improved = delta < 0 if direction == "lower" else delta > 0
        pair["tone"] = "improved" if improved else "regressed"
    return pair


def display_number(value, digits):
    return f"{value:.{digits}f}"


def display_pair(pair, digits):
    valid = pair.get("status") in {"ok", "missing"}
    shown = {}
    for side in ("baseline", "replacement"):
        value = finite_number(pair.get(side)) if valid else None
        if value is not None:
            shown[side] = display_number(value, digits)
        else:
            shown[side] = "-" if valid and pair.get(side + "_status") == "empty" else UNPARSED_LABEL
    shown["tone"] = "neutral"
    delta = pair.get("delta")
    if (pair.get("status") == "ok" and finite_number(pair.get("baseline")) is not None
            and finite_number(pair.get("replacement")) is not None
            and isinstance(delta, (int, float)) and not isinstance(delta, bool)
            and math.isfinite(delta)):
        shown["replacement"] += f" ({delta:+.{digits}f})"
        if delta and pair.get("tone") in {"improved", "regressed"}:
            shown["tone"] = pair["tone"]
    return shown


def safe_source(source):
    """Keep reproducibility fields while omitting report URLs and local paths."""
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in ("kind", "revision", "snapshot_sha256", "identity", "name", "sha256") if key in source}


def report_title(vehicle_label, baseline_start, *, validated_product_version=None):
    """Use only an explicitly validated query-time product code, never a vehicle label.

    The caller must first run product_version.validate_result against the fixed
    task/user/app/group context. Old finals/callers omit this keyword and keep
    the existing XXXX placeholder. New trusted-code titles use the code and
    Base date; the validated result retains the query-time provenance and
    explicitly unverified historical-version semantics.
    """
    try:
        day = datetime.fromisoformat(baseline_start.replace("Z", "+00:00")).strftime("%Y%m%d")
    except (ValueError, AttributeError):
        day = "日期未知"
    version = validated_product_version
    if version is not None:
        if not isinstance(version, dict):
            raise InputError("报告制品来源必须是已校验的查询结果。")
        if version.get("status") == "ok":
            if (version.get("semantics") != "query_time_platform_value" or
                    version.get("historical_version_verified") is not False or
                    re.fullmatch(r"[0-9A-F]{4}", str(version.get("code") or "")) is None):
                raise InputError("报告制品来源缺少可信查询时语义，拒绝替换制品占位。")
            return f"{version['code']}（{day}）走查测试"
    return f"XXXX（{day}基线）走查测试"


def compare_performance(baseline_sheet, replacement_sheet, *, baseline_metadata, replacement_metadata,
                        baseline_state=None, replacement_state=None,
                        vehicle_label=None, validated_product_version=None):
    baseline_context = comparison_context(baseline_metadata, "Baseline")
    replacement_context = comparison_context(replacement_metadata, "PCM更新")
    if baseline_context["vin"] != replacement_context["vin"]:
        raise InputError("Baseline 与 PCM更新 metadata 的 vin 不一致，拒绝生成跨 VIN 对比。")

    comparisons, issues, display_rows = {}, [], []
    coverage = {"baseline": {}, "replacement": {}}
    complete_pairs = 0
    for scenario in REPORT_ORDER:
        label = SCENARIO_LABELS[scenario]
        metric_rows = {}
        baseline_coverage = {
            "p99_available": sheet_value(baseline_sheet, scenario, "p99_ms") is not None,
            "frame_rate_available": sheet_value(baseline_sheet, scenario, "frame_rate_hz") is not None,
        }
        replacement_coverage = {
            "p99_available": sheet_value(replacement_sheet, scenario, "p99_ms") is not None,
            "frame_rate_available": sheet_value(replacement_sheet, scenario, "frame_rate_hz") is not None,
        }
        display_row = [label]
        for metric_key, metric_label, sheet_field, state_field, statistic, direction, digits in METRIC_SPECS:
            if sheet_field:
                before = sheet_value(baseline_sheet, scenario, sheet_field)
                after = sheet_value(replacement_sheet, scenario, sheet_field)
                before_count = after_count = 1 if before is not None and after is not None else None
                before_status = sheet_field_status(baseline_sheet, scenario, sheet_field)
                after_status = sheet_field_status(replacement_sheet, scenario, sheet_field)
            else:
                before, before_count = state_value(baseline_state, scenario, state_field, statistic)
                after, after_count = state_value(replacement_state, scenario, state_field, statistic)
                baseline_coverage[f"{metric_key}_samples"] = before_count
                replacement_coverage[f"{metric_key}_samples"] = after_count
                before_status = state_field_status(baseline_state, scenario, state_field, statistic)
                after_status = state_field_status(replacement_state, scenario, state_field, statistic)
            pair = compare_pair(before, after, direction=direction)
            pair.update(baseline_status=before_status, replacement_status=after_status)
            metric_rows[metric_key] = pair
            shown = display_pair(pair, digits)
            display_row.extend((shown["baseline"], shown["replacement"]))
            if pair["status"] == "ok":
                complete_pairs += 1
            else:
                missing = []
                if before is None:
                    missing.append("Baseline")
                if after is None:
                    missing.append("PCM更新")
                issues.append({
                    "scenario": scenario,
                    "metric": metric_key,
                    "message": f"{'、'.join(missing)} 缺少可用的 {metric_label} 数据。",
                })
                for side, state in (("baseline", before_status), ("replacement", after_status)):
                    if state == "unparsed":
                        issues.append({"scenario": scenario, "metric": metric_key, "side": side,
                                       "kind": "field_unparsed",
                                       "message": f"{side} {label} {metric_label}：{UNPARSED_LABEL}。"})
        coverage["baseline"][scenario] = baseline_coverage
        coverage["replacement"][scenario] = replacement_coverage
        comparisons[scenario] = {"label": label, "metrics": metric_rows}
        display_rows.append(display_row)

    total_pairs = len(REPORT_ORDER) * len(METRIC_SPECS)
    for side, state in (("baseline", baseline_state), ("replacement", replacement_state)):
        state_coverage = state.get("coverage", {}) if isinstance(state, dict) else {}
        coverage[side]["unattributed_fps_samples"] = state_coverage.get("fps_samples_unattributed", 0)
        coverage[side]["performance_status"] = state.get("status", "not_provided") if isinstance(state, dict) else "not_provided"
        if isinstance(state, dict):
            coverage[side]["ctx_validation"] = dict(state.get("ctx_validation") or {})
            coverage[side]["frame_diagnostics"] = dict(state.get("frame_diagnostics") or {})
            coverage[side]["fps_diagnostics"] = dict(state.get("fps_diagnostics") or {})
        if isinstance(state, dict) and state.get("error"):
            coverage[side]["performance_error"] = str(state["error"])
            issues.append({
                "scenario": None,
                "metric": "cpu_log_source",
                "message": f"{side} 的 log_fsdA_service 获取或解析失败：{state['error']}",
            })

    columns = ["车辆模式"]
    for _, label, _, _, _, _, _ in METRIC_SPECS:
        columns.extend((f"{label} Base", f"{label} PCM更新"))
    result = {
        "schema_version": 1,
        "field_status_version": 1,
        "kind": "integrated_test_performance_comparison",
        "status": "ok" if complete_pairs == total_pairs else "partial" if complete_pairs else "failed",
        "title": report_title(vehicle_label, baseline_context["start"],
                              validated_product_version=validated_product_version),
        "vehicle_label": vehicle_label,
        "baseline": {"time_range": {"start": baseline_context["start"], "end": baseline_context["end"]},
                     "source": safe_source(baseline_sheet.get("source"))},
        "replacement": {"time_range": {"start": replacement_context["start"], "end": replacement_context["end"]},
                        "source": safe_source(replacement_sheet.get("source"))},
        "metric_rules": {
            "p99_infer_delay_ms": "Sheets P99; lower is better",
            "frame_rate_hz": "Sheets mean; higher is better",
            "system_cpu_percent": "mean(sys_cpu); lower is better",
            "pcm_p99_infer_delay_ms": "nearest-rank P99(Post timing - Pre start), same PID/frame/ctx; lower is better",
            "pcm_frame_rate_hz": "mean(FPSlast10frames), only homogeneous validated 10-frame state windows; higher is better",
            "pcm_cpu_percent": "mean(infer_expert_pc_cpu / 19); lower is better",
        },
        "comparisons": comparisons,
        "table": {"columns": columns, "rows": display_rows},
        "coverage": coverage,
        "issues": issues,
    }
    return result

