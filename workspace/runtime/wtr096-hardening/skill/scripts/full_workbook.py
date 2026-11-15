"""Lossless, offline XLSX export of the supplied native statistics and rules.

This module formats existing results. It neither downloads logs nor reruns the
analyzer, and cannot establish that all vehicle log types were available. It
does not reproduce the original online seven-sheet template. ``value_json``
and ``value_type`` distinguish null, an empty string and a missing field; the
adjacent display cells are conveniences, not a replacement for those columns.

Requires openpyxl >= 3.1, installed by the deployment owner (never auto-installed).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile

FORMAT_VERSION = "native-full-workbook-v1"
MAX_ROWS = 1_048_576
MAX_COLUMNS = 16_384
MAX_CELL_UNITS = 32_767
SCENARIOS = (("slow_driver", "低速人驾", "0"),
             ("fast_driver", "高速人驾", "4"),
             ("CNOA", "CNOA", "5"), ("parking", "泊车", "1"))
BASE_SHEETS = ("总览", "元数据", "四场景统计", "规则审计", "完整原始字段", "规则原始字段", "PCM与CPU")
SCOPE_NOTICE = (
    "本工作簿完整导出输入的原生统计，并审计全部所提供规则；记录数量不代表日志完整性。"
    "没有统计记录、无源日志和条件跳过不可混同；缺失值不补零。"
    "日志来源与覆盖范围见元数据，逐项缺失原因见规则审计；工作表按本次统计主题组织。"
)


class WorkbookExportError(ValueError):
    """Input or readback is unsafe/ambiguous; no verified artifact is published."""


def _json(value):
    def check_keys(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise WorkbookExportError("JSON 对象键必须为字符串。")
                check_keys(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                check_keys(child)
    check_keys(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkbookExportError("输入必须是有限数值、字符串键的 JSON 数据。") from exc


def _digest(value):
    try:
        return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise WorkbookExportError("字段含无效 Unicode 字符；拒绝静默修改。") from exc


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkbookExportError("JSON 含重复键，不能静默覆盖。")
        result[key] = value
    return result


def _load(value):
    if isinstance(value, (str, Path)):
        path = Path(value)
        with path.open(encoding="utf-8-sig") as stream:
            data = json.load(stream, object_pairs_hook=_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(
                                 WorkbookExportError("JSON 含非有限数值。")))
        return data, _file_sha(path)
    # A JSON round trip also prevents caller mutation during export.
    return json.loads(_json(value), object_pairs_hook=_pairs), None


def _type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise WorkbookExportError("不支持的统计字段类型。")


def _display(value):
    """Excel only guarantees 15 significant digits; exact JSON stays separate."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 999_999_999_999_999 else str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkbookExportError("统计字段包含非有限数值。")
        # Use a numeric cell only when Excel's documented precision is enough.
        if (value == 0 and math.copysign(1, value) < 0) or float(format(value, ".15g")) != value:
            return repr(value)
        return value
    return value if isinstance(value, str) else _json(value)


def _text_check(value):
    if not isinstance(value, str):
        return
    if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > MAX_CELL_UNITS:
        raise WorkbookExportError("字段超过 Excel 单元格 32767 UTF-16 字符限制；拒绝截断。")
    for char in value:
        number = ord(char)
        if (number < 32 and number not in (9, 10, 13)) or 0xD800 <= number <= 0xDFFF or number in (0xFFFE, 0xFFFF):
            raise WorkbookExportError("字段含 XML 不支持的控制字符；拒绝静默修改。")


def safe_sheet_names(themes, reserved=BASE_SHEETS):
    """Stable, unique case-insensitive names; original themes remain in tables."""
    occupied = {name.casefold() for name in reserved}
    names = {}
    for index, theme in enumerate(themes, 1):
        if not isinstance(theme, str) or not theme:
            raise WorkbookExportError("规则主题必须是非空字符串。")
        cleaned = re.sub(r"[\[\]:*?/\\]", "_", theme).strip("'").strip() or "主题"
        _text_check(cleaned)
        prefix = f"T{index:02d}_"
        # Count UTF-16 units, including supplementary characters, conservatively.
        name = prefix
        for char in cleaned:
            if len((name + char).encode("utf-16-le")) // 2 > 31:
                break
            name += char
        suffix = 1
        candidate = name
        while candidate.casefold() in occupied:
            ending = f"_{suffix}"
            candidate = name
            while len((candidate + ending).encode("utf-16-le")) // 2 > 31:
                candidate = candidate[:-1]
            candidate += ending
            suffix += 1
        names[theme] = candidate
        occupied.add(candidate.casefold())
    return names


def _tree_rows(section, value, pointer=""):
    """A JSON-pointer leaf table, retaining empty containers and null leaves."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkbookExportError("JSON 对象键必须为字符串。")
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from _tree_rows(section, child, pointer + "/" + escaped)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            yield from _tree_rows(section, child, pointer + "/" + str(index))
    else:
        yield (section, pointer or "/", _type(value), _display(value), _json(value))


def _audit(rule, record):
    if record is None:
        if rule.get("calculate_type") == "emb_type":
            return ("conditional_not_captured", "条件规则未收录", "规则含 emb_type 条件；原生结果未记录跳过原因，不能确认条件跳过或日志缺失。")
        return ("not_captured", "未收录统计记录", "所提供原生结果中没有该规则记录；未取得跳过/缺失原因。")
    status = record.get("status")
    diagnostic = record.get("diagnostic") or record["statistics"].get("diagnostic")
    quality_note = record.get("source_quality_diagnostic")
    if quality_note:
        diagnostic = str(diagnostic or '') + ('；' if diagnostic else '') + str(quality_note)
    input_note = record.get('input_diagnostic')
    if isinstance(input_note, dict) and input_note.get('message'):
        diagnostic = str(diagnostic or '') + ('；' if diagnostic else '') + str(input_note['message'])
    if status == "conditional_skip":
        return ("conditional_skip", "原生解析明确条件跳过",
                str(record.get("skip_reason") or diagnostic or "原生记录标明 conditional_skip，未提供详细条件原因。"))
    if status == "missing":
        return ("missing", "原生解析明确缺失",
                str(diagnostic or record.get("skip_reason") or "原生记录标明 missing，未提供详细缺失原因。"))
    if status == "available":
        if record.get('source_quality',{}).get('status') == 'partial':
            return ('available_incomplete_source','有统计，MCU 来源不完整',str(diagnostic or '来源日志尾部未完成，统计仅供参考。'))
        return ("available", "原生解析标记可用",
                str(diagnostic or "可用状态沿用原生解析；逐字段空值与占位文本保持原义。"))
    if diagnostic not in (None, ""):
        return ("captured_with_diagnostic", "有记录，含诊断", str(diagnostic))
    if not record["statistics"]:
        return ("captured_empty", "记录无统计字段", "原生结果保留了指标记录，但统计字段为空。")
    return ("captured", "已生成统计记录", "是否有值请逐字段查看；空值和占位文本均保留原义。")


def _pcm_rows(pcm):
    """Show four PCM/CPU metric groups and preserve every remaining PCM leaf."""
    if not isinstance(pcm, dict):
        raise WorkbookExportError("pcm_result 必须是对象，失败结果也应保留 status/diagnostics。")
    metrics = pcm.get("metrics", {})
    if not isinstance(metrics, dict):
        raise WorkbookExportError("pcm_result.metrics 必须是对象。")
    required = ("pcm_p99_infer_delay_ms", "pcm_frame_rate_hz", "pcm_cpu_percent", "system_cpu_percent")
    labels = {key: label for key, label, _ in SCENARIOS}
    scenarios = list(labels) + [key for key in metrics if key not in labels]
    rows = []
    for scenario in scenarios:
        values = metrics.get(scenario, {})
        if not isinstance(values, dict):
            raise WorkbookExportError("PCM 场景必须为对象。")
        label = values.get("label", labels.get(scenario, scenario))
        if not isinstance(label, str):
            raise WorkbookExportError("PCM 场景标签必须为字符串。")
        # Missing expected metric groups are explicit, not manufactured zero rows.
        for metric in required:
            if metric not in values:
                pointer = "/metrics/" + scenario.replace("~", "~0").replace("/", "~1") + "/" + metric
                rows.append(("metrics", scenario, label, metric, False, None, "missing_field", None, pointer))
        for _, pointer, kind, display, exact in _tree_rows("metrics", values):
            metric = pointer.split("/", 2)[1].replace("~1", "/").replace("~0", "~") if pointer != "/" else ""
            full_pointer = "/metrics/" + scenario.replace("~", "~0").replace("/", "~1") + (pointer if pointer != "/" else "")
            rows.append(("metrics", scenario, label, metric, scenario in metrics, display, kind, exact, full_pointer))
    for section, value in pcm.items():
        if section == "metrics":
            continue
        for _, pointer, kind, display, exact in _tree_rows(section, value):
            full_pointer = "/" + section.replace("~", "~0").replace("/", "~1") + (pointer if pointer != "/" else "")
            rows.append((section, None, None, None, True, display, kind, exact, full_pointer))
    return rows


def _build_model(report, rules, request, role, environment, source_hashes, scope,
                 report_file_sha, rules_file_sha):
    if isinstance(report, list):
        report = {"statistics": report}
    if not isinstance(report, dict) or not isinstance(report.get("statistics"), list):
        raise WorkbookExportError("须提供包含 statistics 列表的原生结果，或该列表本身。")
    if not isinstance(rules, dict) or not rules:
        raise WorkbookExportError("须提供完整、非空规则对象；导出器无法验证其是否为部署最新版。")
    if not isinstance(request, dict) or not request or not isinstance(role, str) or not role:
        raise WorkbookExportError("须提供非空 request 和 role。")
    if environment not in ("testtwo", "prod"):
        raise WorkbookExportError("environment 须为本窗口实际 provider 环境 testtwo 或 prod。")
    if not isinstance(source_hashes, dict) or not source_hashes or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or
            not re.fullmatch(r"[0-9a-fA-F]{64}", v) for k, v in source_hashes.items()):
        raise WorkbookExportError("source_hashes 须为非空的名称到 SHA256 字符串映射。")
    if scope is not None and not isinstance(scope, dict):
        raise WorkbookExportError("scope 须为对象；它记录调用方的输入范围，不能证明未提供日志完整。")
    source_hashes = {key: value.lower() for key, value in source_hashes.items()}
    rule_rows = []
    rule_by_metric = defaultdict(list)
    for theme, metrics in rules.items():
        if not isinstance(theme, str) or not theme or not isinstance(metrics, dict):
            raise WorkbookExportError("规则必须是 theme -> metric -> 配置对象。")
        for metric, config in metrics.items():
            if not isinstance(metric, str) or not metric or not isinstance(config, dict):
                raise WorkbookExportError("规则名称必须非空，规则配置必须为对象。")
            rule_rows.append((theme, metric, config))
            rule_by_metric[metric].append(theme)
    if not rule_rows:
        raise WorkbookExportError("规则中没有指标。")
    records = report["statistics"]
    indexed = {}
    record_themes = []
    unknown = []
    for record_index, record in enumerate(records):
        if (not isinstance(record, dict) or not isinstance(record.get("metric"), str)
                or not record["metric"] or not isinstance(record.get("statistics"), dict)):
            raise WorkbookExportError("每条原生记录必须包含 metric 字符串和 statistics 对象。")
        if any(not isinstance(field, str) for field in record["statistics"]):
            raise WorkbookExportError("统计字段名必须为字符串。")
        metric = record["metric"]
        candidates = rule_by_metric.get(metric, [])
        if "theme" in record:
            theme = record["theme"]
            if not isinstance(theme, str) or not theme:
                raise WorkbookExportError("原生记录 theme 必须为非空字符串。")
            mapped = theme in candidates
        elif len(candidates) == 1:
            theme, mapped = candidates[0], True
        elif len(candidates) > 1:
            raise WorkbookExportError("同名指标属于多个主题但原生记录未标主题，不能任意匹配。")
        else:
            theme, mapped = "未匹配规则", False
        key = (theme, metric)
        if key in indexed:
            raise WorkbookExportError("同主题同名原生统计重复，不能静默覆盖。")
        indexed[key] = record
        record_themes.append(theme)
        if not mapped:
            unknown.append((record_index, theme, metric))
    audit_rows = []
    counts = Counter()
    for theme, metric, config in rule_rows:
        record = indexed.get((theme, metric))
        status, label, reason = _audit(config, record)
        counts[status] += 1
        native_rule_match = (record.get("rule") == config if record and "rule" in record else None)
        audit_rows.append((theme, metric, status, label, reason,
                           record is not None, len(record["statistics"]) if record else 0,
                           config.get("soc_type"), config.get("calculate_type"),
                           record.get("native_publish") if record else None, native_rule_match, _json(config)))
    raw_rows = []
    for index, (record, theme) in enumerate(zip(records, record_themes)):
        for field, value in record["statistics"].items():
            raw_rows.append((index, theme, record["metric"], field, _display(value),
                             _type(value), _json(value)))
    rule_fields = []
    for theme, metric, config in rule_rows:
        for field, value in config.items():
            rule_fields.append((theme, metric, field, _display(value), _type(value), _json(value)))
    themes = list(rules)
    for _, theme, _ in unknown:
        if theme not in themes:
            themes.append(theme)
    names = safe_sheet_names(themes)
    scope_info = {
        "notice": SCOPE_NOTICE,
        "input_statistics_records": len(records), "supplied_rules": len(rule_rows),
        "supplied_rule_themes": len(rules), "rule_status_counts": dict(counts),
        "all_vehicle_log_types_obtained": "not_verified_by_exporter",
        "native_execution_reperformed": False,
        "original_seven_sheet_template_reproduced": False,
        "caller_scope": scope or {},
    }
    hashes = {"report_canonical_sha256": _digest(report), "rules_canonical_sha256": _digest(rules),
              "request_canonical_sha256": _digest(request), "source_sha256": source_hashes,
              "report_input_file_sha256": report_file_sha, "rules_input_file_sha256": rules_file_sha}
    metadata = {"format_version": FORMAT_VERSION, "role": role, "environment": environment,
                "request": request, "hashes": hashes, "scope": scope_info,
                "report": {k: v for k, v in report.items() if k != "statistics"},
                "statistics_record_metadata": [{k: v for k, v in record.items() if k != "statistics"}
                                               for record in records],
                "sheet_theme_mapping": names,
                "cell_semantics": "value_json 为权威原值；null、空字符串、缺字段不同。高精度数值以文本展示，JSON 原值不变。"}
    model = {}
    model["总览"] = ("项目", "值", "说明"), [
        ("导出格式", FORMAT_VERSION, "完整输入字段导出；无解析、下载或上传"),
        ("角色", role, "沿用调用方角色，不按时间先后重新分配"),
        ("实际环境", environment, "单窗口 provider 环境"),
        ("输入统计记录", len(records), "不等于全部规则成功，更不等于所有日志已取得"),
        ("提供的规则数", len(rule_rows), "所有规则均列入规则审计"),
        ("规则主题数", len(rules), "按实际主题布局，不复刻原在线七子表"),
        ("原始统计字段数", len(raw_rows), "每个字段及其 JSON 类型、原值均可逐行检查"),
        ("未匹配规则的记录", len(unknown), "仍完整保留；不会丢弃或冒充已匹配"),
        ("范围限制", SCOPE_NOTICE, "调用方补充的输入范围在元数据 scope 中"),
        ("精度及缺失", "value_json 为权威原值", "缺失不填零；空值/空字符串有独立类型；高精度数值用文本避免 Excel 舍入"),
    ] + [("规则状态 " + key, value, "详见规则审计") for key, value in sorted(counts.items())]
    product = report.get('product_version')
    if product is not None:
        model['总览'][1].insert(3,('相关制品信息',product.get('artifact_info') if product.get('status')=='ok' else '未获取',
            '查询时平台返回值，未验证属于本历史日志时段；查询来源、时间和状态见元数据。'))
    quality = report.get('source_quality') or (scope or {}).get('source_quality') or {}
    if quality.get('status') == 'partial':
        model['总览'][1].insert(3,('MCU 日志完整性','不完整',
            str(len(quality.get('issues',[])))+' 份来源日志尾部未写完；仅保留成功解码的完整行，MCU 统计仅供参考。来源文件与恢复记录见元数据。'))
    model["元数据"] = ("section", "json_pointer", "value_type", "value", "value_json"), list(_tree_rows("export", metadata))
    # Keep both native source values and the already projected report values.
    # No percentile/FPS computation is performed here.
    scenario_rows = []
    folded = defaultdict(list)
    for record in records:
        folded[record["metric"].casefold()].append(record)
    projected = report.get("metrics", {})
    if not isinstance(projected, dict):
        raise WorkbookExportError("metrics 必须为对象。")
    for scenario, label, state in SCENARIOS:
        for kind, metric, field, projected_key in (
                ("P99 (ms)", "vla_parking_header_delay" if scenario == "parking" else "header_delay", "P99_" + state, "p99_ms"),
                ("FPS (Hz)", "vlaparkingdlm_fps" if scenario == "parking" else "vlamodel_fps", "mean_" + state, "frame_rate_hz")):
            candidates = folded.get(metric, [])
            if len(candidates) > 1:
                raise WorkbookExportError("四场景源指标有多个大小写同名记录，不能任意投影。")
            native = candidates[0]["statistics"] if candidates else {}
            present = field in native
            value = native.get(field)
            projected_row = projected.get(scenario, {})
            if not isinstance(projected_row, dict):
                raise WorkbookExportError("四场景 metrics 项必须为对象。")
            result_present = projected_key in projected_row
            result_value = projected_row.get(projected_key)
            scenario_rows.append((scenario, label, state, kind, metric, field, present,
                                  _display(value), _type(value) if present else "missing_field",
                                  _json(value) if present else None, result_present,
                                  _display(result_value), _json(result_value) if result_present else None))
    model["四场景统计"] = ("scenario", "场景", "state", "指标", "source_metric", "source_field",
                            "native_field_present", "native_value", "native_value_type", "native_value_json",
                            "report_field_present", "report_value", "report_value_json"), scenario_rows
    model["规则审计"] = ("theme", "metric", "status", "状态", "说明/原生诊断", "record_present",
                          "statistic_field_count", "soc_type", "calculate_type", "native_publish",
                          "native_rule_matches_supplied_rule", "rule_json"), audit_rows
    model["完整原始字段"] = ("record_index", "theme", "metric", "field", "value", "value_type", "value_json"), raw_rows
    model["规则原始字段"] = ("theme", "metric", "field", "value", "value_type", "value_json"), rule_fields
    if "pcm_result" in report:
        model["PCM与CPU"] = (("section", "scenario", "场景", "source_metric", "field_present",
                               "value", "value_type", "value_json", "json_pointer"), _pcm_rows(report["pcm_result"]))
    for theme in themes:
        entries = [(metric, config, indexed.get((theme, metric)))
                   for t, metric, config in rule_rows if t == theme]
        entries.extend((metric, None, records[index]) for index, t, metric in unknown if t == theme)
        fields = list(dict.fromkeys(field for _, _, record in entries if record
                                   for field in record["statistics"]))
        rows = []
        for metric, config, record in entries:
            status, label, reason = _audit(config, record) if config is not None else (
                "unmapped_record", "未匹配规则的记录", "未找到唯一精确规则；原始字段完整保留。")
            stats = record["statistics"] if record else {}
            rows.append((theme, metric, status, label, reason, record is not None,
                         _json(config) if config is not None else None,
                         _json(list(stats))) + tuple(_display(stats.get(field)) for field in fields))
        # Prefix fixed columns; original statistic field names are not renamed.
        model[names[theme]] = (("[meta] theme", "[meta] metric", "[meta] status", "[meta] 状态",
                               "[meta] 说明/诊断", "[meta] record_present", "[meta] rule_json",
                               "[meta] present_fields_json") + tuple(fields), rows)
    for sheet, (headers, rows) in model.items():
        if len(rows) + 1 > MAX_ROWS or len(headers) > MAX_COLUMNS:
            raise WorkbookExportError("工作表超过 Excel 行列限制；拒绝截断。")
        for row in [headers, *rows]:
            if len(row) != len(headers):
                raise WorkbookExportError("内部列数不一致。")
            for value in row:
                _text_check(value)
    manifest = {"format_version": FORMAT_VERSION, "hashes": hashes, "role": role,
                "environment": environment, "scope": scope_info, "theme_sheets": names,
                "statistics_records": len(records), "statistics_fields": len(raw_rows),
                "rule_fields": len(rule_fields), "rule_status_counts": dict(counts),
                "pcm_result_included": "pcm_result" in report,
                "unmapped_records": len(unknown),
                "sheet_rows": {name: len(rows) + 1 for name, (_, rows) in model.items()}}
    return model, manifest


def _openpyxl():
    try:
        import openpyxl
    except ImportError as exc:
        raise WorkbookExportError("完整 XLSX 导出需要 openpyxl >= 3.1；请由部署环境预装，导出器不会联网安装。") from exc
    version = tuple(int(part) for part in openpyxl.__version__.split(".")[:2])
    if version < (3, 1):
        raise WorkbookExportError("完整 XLSX 导出需要 openpyxl >= 3.1。")
    return openpyxl


def _write(model, path):
    openpyxl = _openpyxl()
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    book = openpyxl.Workbook()
    book.remove(book.active)
    book.properties.title = "m100-ultra 原生统计工程明细"
    book.properties.description = SCOPE_NOTICE
    header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    font = Font(name="Arial", size=10)
    fill = PatternFill("solid", fgColor="17365D")
    for name, (headers, rows) in model.items():
        sheet = book.create_sheet(name)
        for row_index, values in enumerate([headers, *rows], 1):
            for col_index, value in enumerate(values, 1):
                cell = sheet.cell(row_index, col_index, value)
                # Every string is literal, even '=...', '+...', '-', '@...', or a URL.
                if isinstance(value, str):
                    cell.data_type = "s"
                cell.font = header_font if row_index == 1 else font
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if row_index == 1:
                    cell.fill = fill
        sheet.freeze_panes = "D2" if len(headers) >= 7 else "B2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
        sheet.row_dimensions[1].height = 30
        for col_index, header in enumerate(headers, 1):
            width = 24
            if "json" in header or "说明" in header or header in ("value", "值"):
                width = 56
            elif "metric" in header or "field" in header or "pointer" in header:
                width = 36
            elif "type" in header or "present" in header:
                width = 21
            sheet.column_dimensions[get_column_letter(col_index)].width = width
        sheet.sheet_view.showGridLines = False
    book.save(path)
    book.close()


def _verify(path, model):
    """Read every cell, not just totals. Exact JSON columns retain null semantics."""
    openpyxl = _openpyxl()
    book = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
    cells = 0
    try:
        if book.sheetnames != list(model):
            raise WorkbookExportError("XLSX 读回的工作表集合/顺序不一致。")
        for name, (headers, rows) in model.items():
            sheet = book[name]
            expected = [headers, *rows]
            if sheet.max_row != len(expected) or sheet.max_column != len(headers):
                raise WorkbookExportError("XLSX 读回行列数不一致。")
            for row_number, (actual, wanted) in enumerate(zip(sheet.iter_rows(), expected), 1):
                for column, (cell, value) in enumerate(zip(actual, wanted), 1):
                    if cell.data_type == "f" or cell.data_type == "e":
                        raise WorkbookExportError("XLSX 出现非预期公式/错误单元格。")
                    # Empty literal strings have no physical Excel value; their
                    # original type/value remain explicit in the JSON columns.
                    value = None if value == "" else value
                    if cell.value != value or isinstance(cell.value, bool) != isinstance(value, bool):
                        raise WorkbookExportError(f"XLSX 字段读回不一致：{name} 行{row_number} 列{column}。")
                    if isinstance(value, str) and cell.data_type not in ("s", "inlineStr"):
                        raise WorkbookExportError("文本单元格被错误解释。")
                    cells += 1
        if getattr(book, "_external_links", []):
            raise WorkbookExportError("工作簿含外部链接。")
    finally:
        book.close()
    return {"status": "ok", "method": "all_cells_and_types_plus_exact_value_json",
            "sheets_checked": len(model), "cells_checked": cells,
            "formula_cells": 0, "null_and_empty_string_distinguished": True}


def verify_full_workbook(path, report_data, rules, *, request, role, environment,
                         source_hashes, scope=None):
    """Independently reconstruct the expected workbook and read it back."""
    report, report_sha = _load(report_data)
    rule_data, rules_sha = _load(rules)
    model, _ = _build_model(report, rule_data, request, role, environment, source_hashes,
                            scope, report_sha, rules_sha)
    return _verify(Path(path), model)


def export_full_workbook(report_data, rules, output_path, *, request, role,
                         environment, source_hashes, scope=None, overwrite=False):
    """Write XLSX + ``<stem>.manifest.json`` after full cell-by-cell readback.

    ``report_data`` accepts report-data/native-statistics JSON path, a report
    object, or statistics list. ``rules`` accepts a JSON path or complete rule
    object. ``environment`` is the actual provider environment (testtwo/prod).
    ``source_hashes`` identifies the source manifests or archives by SHA256.
    ``scope`` is caller-supplied evidence/limitations, never a completeness proof.
    A consumer must check the manifest's artifact SHA before using the workbook.
    """
    report, report_sha = _load(report_data)
    rule_data, rules_sha = _load(rules)
    model, manifest = _build_model(report, rule_data, request, role, environment,
                                    source_hashes, scope, report_sha, rules_sha)
    output = Path(output_path)
    if output.suffix.lower() != ".xlsx":
        raise WorkbookExportError("输出文件须使用 .xlsx 扩展名。")
    sidecar = output.with_suffix(".manifest.json")
    if not overwrite and (output.exists() or sidecar.exists()):
        raise WorkbookExportError("输出已存在；请使用独立运行目录，或明确指定 overwrite=True。")
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    manifest_scratch = None
    try:
        descriptor, scratch_name = tempfile.mkstemp(prefix=".workbook-", suffix=".xlsx", dir=output.parent)
        os.close(descriptor)
        scratch = Path(scratch_name)
        _write(model, scratch)
        manifest["validation"] = _verify(scratch, model)
        manifest["artifact"] = {"name": output.name, "sha256": _file_sha(scratch), "bytes": scratch.stat().st_size}
        manifest.update({"path": str(output.resolve()), "sha256": manifest["artifact"]["sha256"],
                         "bytes": manifest["artifact"]["bytes"], "request": request,
                         "request_sha256": _digest(request),
                         "rules_sha256": rules_sha or _digest(rule_data),
                         "rules_sha256_method": "input_file_bytes" if rules_sha else "canonical_json",
                         "source_hashes": manifest["hashes"]["source_sha256"]})
        manifest["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        descriptor, manifest_name = tempfile.mkstemp(prefix=".workbook-", suffix=".json", dir=output.parent)
        os.close(descriptor)
        manifest_scratch = Path(manifest_name)
        manifest_scratch.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(scratch, output)
        os.replace(manifest_scratch, sidecar)
    finally:
        for temporary in (scratch, manifest_scratch):
            if temporary is not None and temporary.exists():
                temporary.unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--request", required=True, help="JSON file for one window")
    parser.add_argument("--role", required=True)
    parser.add_argument("--environment", required=True, choices=("testtwo", "prod"))
    parser.add_argument("--source-hashes", required=True, help="JSON file mapping source labels to SHA256")
    parser.add_argument("--scope", help="Optional JSON file with input coverage/limitations")
    args = parser.parse_args(argv)
    manifest = export_full_workbook(args.report, args.rules, args.output,
        request=_load(args.request)[0], role=args.role, environment=args.environment,
        source_hashes=_load(args.source_hashes)[0], scope=_load(args.scope)[0] if args.scope else None)
    print(json.dumps({"status": "verified", "artifact": manifest["artifact"],
                      "statistics_records": manifest["statistics_records"],
                      "statistics_fields": manifest["statistics_fields"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
