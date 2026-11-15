"""Explain observed empty native metrics without changing their definitions.

The six supported rules have broad tags that can also match profiling,
configuration, or warning lines.  Only verified, actually selected native input
members are examined.  Unknown layouts remain unresolved; this module never
chooses another numeric field, supplies a sample, or changes native statistics.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat

POLICY = "native-missing-input-shapes-v1"
_TZ = timezone(timedelta(hours=8))
_NUMBER = re.compile(r"-?[0-9]+(.[0-9]+)?")
_EXPECTED = {
    "bevparking_scene_executor_delay": ("bevparking", "FSDA_M100_ULTRA", "ParkingSceneExecutorNode::OnImage", "custom", 'var = "" $18 "," find_digit($10);', "ps_time", 18),
    "bevparking_scene_postprocess_delay": ("bevparking", "FSDA_M100_ULTRA", "ParkingSceneExecutorNode::OnImage", "custom", 'var = "" $27 "," find_digit($10);', "ps_time", 27),
    "bevparking_pipeline_scene_head_delay": ("bevparking", "FSDA_M100_ULTRA", "ParkingSceneExecutorNode::OnImage", "custom", 'var = "" $32 "," find_digit($10);', "ps_time", 32),
    "lidar_safety_fusion_delay": ("lidar", "lidar_container", "exec time:", "default", "12", "ps_default", 12),
    "prediction_obs": ("predictionA", "FSDA_M100_ULTRA", "obstacle::", "key_next", "", "ps_default", None),
    "prediction_obs_dynamic": ("predictionA", "FSDA_M100_ULTRA", "obstacle::", "custom", 'var = key_next("|");', "ps_default", None),
}


class MetricDiagnosticsError(ValueError):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           allow_nan=False).encode("utf-8"))


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def _digit(value):
    match = _NUMBER.search(value)
    return match.group(0) if match else ""


def _configured_value(metric, text, fields):
    index = _EXPECTED[metric][-1]
    if index is not None:
        value = fields[index - 1] if len(fields) >= index else ""
        return _digit(value) if metric == "lidar_safety_fusion_delay" else value
    separator = "|" if metric == "prediction_obs_dynamic" else "obstacle::"
    pieces = text.split(separator)
    value = pieces[1].strip() if len(pieces) > 1 else ""
    return _digit(value) if re.match(r"^-?[0-9]+(.[0-9]+)?", value) else ""


def _shape(metric, text, fields):
    if metric.startswith("bevparking_"):
        if (len(fields) == 9 and fields[6:9] == ["[RecallCheck]", "ParkingSceneExecutorNode::OnImage", "start"]):
            return "recallcheck_start_without_statistic_fields"
        if (len(fields) == 14 and fields[6:10] == ["[RecallCheck]", "ParkingSceneExecutorNode::OnImage", "end,", "cost:"]
                and fields[11:13] == ["ms,", "header:"]):
            return "recallcheck_end_without_statistic_fields"
        if (len(fields) == 18 and fields[5:12] == ["[EDSG]:", "UpdateTaskContext", "cpu", "node:",
                "dag:ParkingSceneExecutorNode::OnImage", "ouptut", "dataID:"]
                and fields[13:17] == ["buffer", "is", "nullptr", "->"]
                and re.fullmatch(r"cpu_node\.cc:[0-9]+", fields[17])):
            return "null_output_warning_without_statistic_fields"
        if "lios-config:" in text and text.rstrip().endswith("ParkingSceneExecutorNode::OnImage,"):
            return "configuration_declaration"
    elif metric == "lidar_safety_fusion_delay":
        if (len(fields) == 27 and fields[5:7] == ["[EDSG]:", "graph:"]
                and fields[8:10] == ["context", "idx:"] and fields[11] == "startTime:"
                and fields[13] == "endTime:" and fields[15:17] == ["running", "time:"]
                and fields[18:23] == ["ms,", "exceed", "max", "exec", "time:"]
                and fields[24:26] == ["ms", "->"]):
            return "graph_budget_warning_not_safety_fusion_statistic"
    else:
        if (re.fullmatch(r"\s*(?:struct|enum)\s+dds_(?:perception|parking)_obstacle::[A-Za-z_][A-Za-z_0-9]*\s*\{\s*", text)
                or re.fullmatch(r"\s*sequence<dds_(?:perception|parking)_obstacle::[A-Za-z_][A-Za-z_0-9]*\s*>\s+[A-Za-z_][A-Za-z_0-9]*;\s*", text)):
            return "dds_idl_definition_without_measurement_timestamp"
        if (any(value.startswith("VBSLOG_") for value in fields[5:7])
                and re.search(r"dds_(?:perception|parking)_obstacle::[A-Za-z_]", text)
                and any(token in text for token in ("topic idl", "Topic ", "enum ", "struct "))):
            return "dds_type_registration_not_obstacle_count"
    return "unknown"


def _lines(raw, tag):
    start = counted = 0
    number = 1
    while True:
        position = raw.find(tag, start)
        if position < 0:
            return
        lower = raw.rfind(b"\n", 0, position) + 1
        upper = raw.find(b"\n", position)
        if upper < 0:
            upper = len(raw)
        number += raw.count(b"\n", counted, lower)
        counted = lower
        yield number, raw[lower:upper].rstrip(b"\r")
        start = upper + 1


def _rule_matches(metric, row):
    theme, soc, tag, function, expression, ps, _ = _EXPECTED[metric]
    rule = row.get("rule") or {}
    return (row.get("theme") == theme and rule.get("soc_type") == soc
            and rule.get("log_tag") == tag and rule.get("func") == function
            and rule.get("input") == expression and rule.get("ps_type") == ps
            and rule.get("calculate_type", "") == "")


def _time_window(report):
    window = report.get("metadata", {}).get("time_range")
    if not isinstance(window, str) or not re.fullmatch(r"[0-9]{14}/[0-9]{14}", window):
        raise MetricDiagnosticsError("指标诊断缺少精确请求窗口。")
    parts = [datetime.strptime(part, "%Y%m%d%H%M%S").replace(tzinfo=_TZ) for part in window.split("/")]
    if parts[0] >= parts[1]:
        raise MetricDiagnosticsError("指标诊断请求窗口无效。")
    return window, parts[0].year, [part.timestamp() for part in parts]


def _path(root, relative):
    if not isinstance(relative, str):
        raise MetricDiagnosticsError("实际成员路径缺失。")
    pure = PurePosixPath(relative)
    if (pure.is_absolute() or "\\" in relative or any(part in ("", ".", "..") for part in relative.split("/"))
            or not pure.parts or pure.parts[0] != "log"):
        raise MetricDiagnosticsError("指标诊断成员路径越界。")
    path = root.joinpath(*pure.parts)
    if not path.resolve(strict=True).is_relative_to(root):
        raise MetricDiagnosticsError("指标诊断实际成员不在本任务目录。")
    return path


def _unavailable(row, name, error, operation):
    """An optional diagnostic cannot invalidate already computed statistics.

    This records only an OS failure, never a successful identity check.  The
    caller must still propagate unsafe paths, non-files, or changed contents.
    """
    row.update(status="unresolved", reason="selected_input_unavailable_unresolved", scan_complete=False)
    row.setdefault("input_read_errors", []).append({"member": name, "operation": operation,
        "error_type": type(error).__name__, "errno": error.errno})


def build_metric_input_diagnostics(report, log_root=None):
    """Return evidence about supported *empty* rules from selected raw inputs.

    Counts precede native state-boundary trimming and are therefore explicitly
    not substituted for native retained-sample counts.  Member hashing binds
    this read to the already completed execution audit.  No raw line is emitted.
    """
    window, year, epochs = _time_window(report)
    runtime = report.get("runtime_execution") or {}
    if (runtime.get("status") != "verified" or runtime.get("request_time_range") != window
            or runtime.get("expected_epoch_range") != epochs):
        raise MetricDiagnosticsError("指标诊断与原生运行审计窗口不一致。")
    supplied_root = log_root if log_root is not None else runtime.get("flat_log_root")
    if supplied_root is None:
        raise MetricDiagnosticsError("指标诊断缺少实际输入目录。")
    root_error = None
    try:
        root = Path(supplied_root).resolve(strict=True)
    except OSError as error:
        root, root_error = None, error
    rows = {}
    missing = [row for row in report.get("statistics", []) if row.get("data_status", row.get("status")) == "missing"]
    for row in missing:
        metric = row.get("metric")
        if metric not in _EXPECTED:
            continue
        if metric in rows:
            raise MetricDiagnosticsError("指标诊断遇到重复规则键。")
        rows[metric] = {
            "theme": row.get("theme"), "metric": metric,
            "native_diagnostic": str(row.get("diagnostic") or ""),
            "required_input": {"soc_type": row.get("rule", {}).get("soc_type"),
                "log_tag": row.get("rule", {}).get("log_tag"), "expression": row.get("rule", {}).get("input"),
                "configured_field": _EXPECTED[metric][-1]},
            "status": "unresolved", "reason": "rule_contract_changed" if not _rule_matches(metric, row) else "pending",
            "counts": Counter({key: 0 for key in ("raw_tag_hits", "invalid_timestamp_tag_hits", "invalid_encoding_tag_hits", "in_window_tag_hits",
                "configured_numeric_values", "configured_non_numeric_values")}),
            "field_counts_in_window": Counter(), "shapes_in_window": Counter(), "untimed_shapes": Counter(),
            "sample_line_sha256": {}, "selected_members": {}, "scan_complete": True,
        }
    extractions = {row.get("theme"): row for row in runtime.get("extractions", [])}
    requests = defaultdict(lambda: defaultdict(list))
    for metric, row in rows.items():
        if row["reason"] != "pending":
            continue
        if root_error is not None:
            _unavailable(row, "", root_error, "resolve_log_root")
            continue
        extraction = extractions.get(row["theme"])
        if not isinstance(extraction, dict):
            row["reason"] = "missing_runtime_theme_observation"
            continue
        members, paths = extraction.get("actual_members", {}), extraction.get("actual_paths", {})
        if set(members) != set(paths) or extraction.get("record_epoch_range") != epochs:
            raise MetricDiagnosticsError("指标诊断实际主题成员或窗口变化。")
        for name, expected in members.items():
            try:
                path = _path(root, paths[name])
            except OSError as error:
                _unavailable(row, name, error, "resolve_member")
                continue
            original_name = expected.get('basename') if runtime.get('schema_version') == 3 else name
            if path.name != original_name:
                raise MetricDiagnosticsError("指标诊断成员名与路径不一致。")
            requests[path][row["required_input"]["log_tag"]].append((metric, name, expected))
        if not members:
            row.update(status="explained", reason="no_selected_source_members")
    for path, tags in requests.items():
        try:
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise MetricDiagnosticsError("指标诊断实际成员不是普通文件。")
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            raw = path.read_bytes()
            digest = _sha(raw)
            after = path.stat()
        except OSError as error:
            for bindings in tags.values():
                for metric, name, _ in bindings:
                    _unavailable(rows[metric], name, error, "read_member")
            continue
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity:
            raise MetricDiagnosticsError("指标诊断过程中原始成员变化。")
        for tag, bindings in tags.items():
            for metric, name, expected in bindings:
                if digest != expected.get("sha256") or len(raw) != expected.get("bytes"):
                    raise MetricDiagnosticsError("指标诊断成员内容与原生运行审计不符。")
                rows[metric]["selected_members"][name] = copy.deepcopy(expected)
            for line_number, line in _lines(raw, tag.encode("utf-8")):
                try:
                    text = line.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    for metric, _, _ in bindings:
                        row = rows[metric]
                        row["counts"]["raw_tag_hits"] += 1
                        row["counts"]["invalid_encoding_tag_hits"] += 1
                        row["scan_complete"] = False
                        if "invalid_encoding" not in row["sample_line_sha256"]:
                            row["sample_line_sha256"]["invalid_encoding"] = {"sha256": _sha(line), "member": path.name, "line": line_number}
                    continue
                fields = text.split()
                try:
                    stamp = datetime.strptime(str(year) + "-" + fields[0] + " " + fields[1],
                                              "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_TZ).timestamp()
                except (ValueError, IndexError):
                    stamp = None
                for metric, _, _ in bindings:
                    row = rows[metric]
                    row["counts"]["raw_tag_hits"] += 1
                    if stamp is None:
                        row["counts"]["invalid_timestamp_tag_hits"] += 1
                        shape = _shape(metric, text, fields)
                        row["untimed_shapes"][shape] += 1
                        if "untimed_" + shape not in row["sample_line_sha256"]:
                            row["sample_line_sha256"]["untimed_" + shape] = {"sha256": _sha(line), "member": path.name, "line": line_number}
                        continue
                    if not epochs[0] <= stamp <= epochs[1]:
                        continue
                    row["counts"]["in_window_tag_hits"] += 1
                    shape = _shape(metric, text, fields)
                    row["shapes_in_window"][shape] += 1
                    row["field_counts_in_window"][str(len(fields))] += 1
                    value = _configured_value(metric, text, fields)
                    row["counts"]["configured_numeric_values" if _finite(value) else "configured_non_numeric_values"] += 1
                    if shape not in row["sample_line_sha256"]:
                        row["sample_line_sha256"][shape] = {"sha256": _sha(line), "member": path.name, "line": line_number}
        # Release this member before reading the next; do not retain vehicle
        # body data as part of the exported diagnostic evidence.
        del raw
    for metric, row in rows.items():
        if row["reason"] == "pending":
            counts = row["counts"]
            if not counts["raw_tag_hits"]:
                row.update(status="explained", reason="no_tag_matches_in_selected_inputs")
            elif counts["invalid_encoding_tag_hits"]:
                row.update(reason="invalid_encoding_in_target_lines_unresolved")
            elif row["untimed_shapes"]["unknown"]:
                row.update(reason="unrecognized_untimed_input_layout_unresolved")
            elif not counts["in_window_tag_hits"]:
                row.update(status="explained", reason="no_tag_matches_inside_request_window")
            elif counts["configured_numeric_values"]:
                row.update(reason="numeric_input_exists_native_missing_unresolved")
            elif row["shapes_in_window"]["unknown"]:
                row.update(reason="unrecognized_input_layout_unresolved")
            else:
                row.update(status="explained", reason="only_recognized_non_metric_or_incompatible_field_layouts")
        if row["reason"] == "only_recognized_non_metric_or_incompatible_field_layouts":
            if metric.startswith("bevparking_"):
                row["message"] = "命中的是调用跟踪、配置或空指针告警，未提供本规则要求的统计字段；保留缺失，不将其它耗时替代本指标。"
            elif metric == "lidar_safety_fusion_delay":
                row["message"] = "命中的是图执行超预算告警，规则指定的第12字段为startTime文本；图耗时和预算不能替代安全融合时延。"
            else:
                row["message"] = "命中的是DDS类型注册文本，未打印本规则要求的障碍物计数；保留缺失。"
        else:
            row["message"] = {
                "rule_contract_changed": "规则定义已变化，本诊断未自动适配。",
                "missing_runtime_theme_observation": "缺少该主题的实际文件消费证明，尚未归因。",
                "no_selected_source_members": "该主题没有选中的源日志文件。",
                "no_tag_matches_in_selected_inputs": "该主题实际选中的日志未命中目标标签。",
                "no_tag_matches_inside_request_window": "该主题实际选中的日志没有可定位到请求窗口内的目标标签。",
                "numeric_input_exists_native_missing_unresolved": "按原规则可提取到数值，但原生统计仍为空，尚未查清过滤或统计阶段原因。",
                "unrecognized_input_layout_unresolved": "命中尚未识别的日志格式，不能断言无有效样本，需继续检查。",
                "unrecognized_untimed_input_layout_unresolved": "存在无法定位采集时间且格式未识别的标签行，不能判断其是否属于请求窗口，需继续检查。",
                "invalid_encoding_in_target_lines_unresolved": "目标标签行含无法按UTF-8解码的字节，未用忽略字符方式推断数值或无样本，需继续检查；原统计值保留。",
                "selected_input_unavailable_unresolved": "附加诊断无法读取部分已选中的来源文件，检查未完成；原统计值保留，不能据此判定无样本。",
            }.get(row["reason"], "尚未归因。")
        for key in ("counts", "field_counts_in_window", "shapes_in_window", "untimed_shapes"):
            row[key] = dict(row[key])
    result = {
        "schema_version": 1, "policy": POLICY, "request_time_range": window,
        "counts_stage": "selected_raw_lines_before_native_state_boundary_trim",
        "counts_are_native_final_samples": False,
        "statistical_values_or_rules_modified": False,
        "continuous_vehicle_log_coverage_proven": False,
        "supported_missing_rules_checked": len(rows),
        "other_missing_rules_not_classified_here": len(missing) - len(rows),
        "unresolved_metrics": sorted(name for name, row in rows.items() if row["status"] != "explained"),
        "metrics": rows,
    }
    result["evidence_sha256"] = _canonical(result)
    return result


def attach_metric_input_diagnostics(report, log_root=None):
    """Copy result and add diagnostics; all preexisting fields stay unchanged."""
    evidence = build_metric_input_diagnostics(report, log_root)
    output = copy.deepcopy(report)
    output["metric_input_diagnostics"] = evidence
    for row in output.get("statistics", []):
        if row.get("metric") in evidence["metrics"]:
            row["input_diagnostic"] = copy.deepcopy(evidence["metrics"][row["metric"]])
    return output
