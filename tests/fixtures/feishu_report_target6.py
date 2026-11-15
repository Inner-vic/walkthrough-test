#!/usr/bin/env python3
"""Render the compact AD comparison as Feishu Docx XML."""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import copy
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from datetime import datetime

from request_model import InputError, VIN_VALUE_PATTERN
from performance_comparison import UNPARSED_LABEL, finite_number
from report_attachments import artifact_filename


METRICS = (
    ("p99_infer_delay_ms", "AD P99 Infer Delay (ms)"),
    ("frame_rate_hz", "AD Frame Rate (Hz)"),
    ("system_cpu_percent", "Sys CPU Usage (%)"),
    ("pcm_p99_infer_delay_ms", "PCM P99 Infer Delay (ms)"),
    ("pcm_frame_rate_hz", "PCM Frame Rate (Hz)"),
    ("pcm_cpu_percent", "PCM CPU Usage (%)"),
)
SCENARIOS = ("slow_driver", "fast_driver", "parking", "CNOA")
SCENARIO_LABELS = {"slow_driver": "低速人驾", "fast_driver": "高速人驾",
                   "parking": "泊车", "CNOA": "CNOA"}
SIDE_LABELS = (("baseline", "Base"), ("replacement", "PCM更新"))
NOTIFICATION_METRICS = (('p99_infer_delay_ms', 'AD P99 Infer Delay'), ('frame_rate_hz', 'AD Frame Rate'), ('system_cpu_percent', 'Sys CPU Usage'), ('pcm_p99_infer_delay_ms', 'PCM P99 Infer Delay'), ('pcm_frame_rate_hz', 'PCM Frame Rate'), ('pcm_cpu_percent', 'PCM CPU Usage'))
PRESENTATION_V2 = 'walkthrough-presentation-v2'
SOURCE_PARAGRAPH = 'source_paragraph'


def _detail_links_style(value):
    if 'detail_links_style' not in value:
        return None
    if value['detail_links_style'] != SOURCE_PARAGRAPH or value.get('presentation_version') != PRESENTATION_V2:
        raise InputError('明细入口样式无效；不能重渲染已有报告。')
    return SOURCE_PARAGRAPH


def unparsed_metric_groups(result: dict) -> list[dict]:
    """Summarize only trusted comparison slots, never free-form diagnostics.

    A finite value (including zero) proves extraction. A missing value is only
    confirmed empty when its side explicitly says so; old caches without that
    evidence remain actionable rather than silently becoming an ASCII dash.
    """
    comparisons = result.get("comparisons")
    comparisons = comparisons if isinstance(comparisons, dict) else {}
    groups = []
    for side, side_label in SIDE_LABELS:
        for scenario in SCENARIOS:
            row = comparisons.get(scenario)
            row = row if isinstance(row, dict) else {}
            metrics = row.get("metrics")
            metrics = metrics if isinstance(metrics, dict) else {}
            missing = []
            for metric, label in NOTIFICATION_METRICS:
                pair = metrics.get(metric)
                pair = pair if isinstance(pair, dict) else {}
                if (pair.get("status", "ok") in {"ok", "missing"} and
                        (finite_number(pair.get(side)) is not None or pair.get(side + "_status") == "empty")):
                    continue
                missing.append(label)
            if missing:
                groups.append({"side": side_label, "scenario": SCENARIO_LABELS[scenario],
                               "metrics": missing})
    return groups


def build_report_notification(result: dict, *, vin: str, url: str) -> dict:
    """Put extraction warnings in the same single source-message report reply."""
    _detail_links_style(result)
    if result.get('presentation_version') == PRESENTATION_V2:
        return _compact_notification(result,vin=vin,url=url)
    content = [[{"tag": "text", "text": f"VIN：{vin}"}],
               [{"tag": "a", "text": "查看走查测试报告", "href": url}]]
    if result.get("delivery_format") == "docx_with_native_sheet_details":
        details = result.get("native_details") or {}
        if set(details) != {"baseline", "replacement"}:
            raise InputError("报告回复缺少两份原生明细。")
        urls = [_native_detail_url(details[role]) for role, _ in SIDE_LABELS]
        if urls[0] == urls[1]:
            raise InputError("报告回复的两份原生明细不能相同。")
        content.append([{"tag": "a", "text": label + " 完整原生明细", "href": link}
                        for (_, label), link in zip(SIDE_LABELS, urls)])
        content.append([{"tag": "text", "text":
            "主表单位：时延 ms、帧率 Hz、CPU %；CPU 差值为百分点。Δ = PCM更新 − Base；绿色改善，红色退化。"}])
        for role, label in SIDE_LABELS:
            source = (result.get("local_sources") or {}).get(role) or {}
            if (source.get("source_quality") or {}).get("status") == "partial":
                content.append([{"tag": "text", "text":
                    label + " 的 MCU 来源存在不完整日志，相关统计仅供参考。"}])
    version = result.get("product_version")
    if isinstance(version, dict):
        if (version.get("status") == "ok" and
                version.get("semantics") == "query_time_platform_value" and
                version.get("historical_version_verified") is False and
                __import__("re").fullmatch(r"[0-9A-F]{4}", str(version.get("code") or ""))):
            text = ("相关制品：" + version["code"] +
                    "（查询时平台返回，未验证为两个历史时段的安装版本）。")
        else:
            text = "相关制品信息未获取；历史时段的安装版本未验证。"
        content.append([{"tag": "text", "text": text}])
    groups = unparsed_metric_groups(result)
    if groups:
        content.append([{"tag": "text", "text": "以下字段未解析到，需排查（不等同于无数据）："}])
        for group in groups:
            content.append([{"tag": "text", "text":
                f"{group['side']} · {group['scenario']}：{'、'.join(group['metrics'])}"}])
    return {"zh_cn": {"title": "走查测试报告已生成", "content": content}}


def _compact_url(value, kind):
    """Expose one exact cloud URL, without hidden query data."""
    try:
        if not isinstance(value,str) or any(c.isspace() for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.feishu.cn')
                or parsed.username or parsed.password or parsed.port is not None
                or parsed.query or parsed.fragment
                or not __import__('re').fullmatch('/' + kind + '/[A-Za-z0-9_-]+',parsed.path)):
            raise ValueError
    except (TypeError,ValueError):
        raise InputError('精简报告缺少有效、无附加参数的飞书链接。') from None
    return value


def _compact_context(result, vin=None):
    """Read context only from the two task-bound result windows."""
    if result.get('delivery_format') != 'docx_with_native_sheet_details':
        raise InputError('精简呈现只用于完整原生走查报告。')
    actual_vin = result.get('vin')
    if (not isinstance(actual_vin,str) or VIN_VALUE_PATTERN.fullmatch(actual_vin) is None
            or vin is not None and vin != actual_vin):
        raise InputError('精简报告 VIN 与本任务结果不一致。')
    pipeline = result.get('pipeline')
    windows = pipeline.get('windows') if isinstance(pipeline,dict) else None
    if not isinstance(windows,dict) or set(windows) != {'baseline','replacement'}:
        raise InputError('精简报告缺少两个明确角色的完整任务窗口。')
    ranges = {}
    def parse_time(value):
        if (not isinstance(value,str) or not __import__('re').fullmatch(
                r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?',value)):
            raise InputError('精简报告时间不是完整、有效的已绑定时间。')
        try:
            return datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:
            raise InputError('精简报告时间不是完整、有效的已绑定时间。') from None
    for role,_ in SIDE_LABELS:
        side = result.get(role)
        period = side.get('time_range') if isinstance(side,dict) else None
        window = windows[role]
        request = window.get('request') if isinstance(window,dict) else None
        if (not isinstance(period,dict) or not isinstance(request,dict)
                or request.get('vin') != actual_vin):
            raise InputError('精简报告角色、VIN或时间来源未绑定本任务。')
        times = []
        for key in ('start','end'):
            display,bound = parse_time(period.get(key)),parse_time(request.get(key))
            if display.isoformat() != bound.isoformat():
                raise InputError('精简报告时间与对应角色实际统计请求不一致。')
            times.append(display)
        try:
            if times[1] <= times[0]:raise ValueError
        except (TypeError,ValueError):
            raise InputError('精简报告时间范围无效或时区不一致。') from None
        ranges[role] = ' ～ '.join(value.isoformat(sep=' ') for value in times)
    return actual_vin,ranges


def _compact_details(result):
    details = result.get('native_details')
    if not isinstance(details,dict) or set(details) != {'baseline','replacement'}:
        raise InputError('精简报告必须包含两份已校验原生明细。')
    urls = {}
    for role,_ in SIDE_LABELS:
        entry = details[role]
        if not isinstance(entry,dict):
            raise InputError('精简报告的原生明细格式无效。')
        urls[role] = _compact_url(entry.get('url'),'sheets')
    if urls['baseline'] == urls['replacement']:
        raise InputError('Base 与 PCM更新明细不能指向同一份表。')
    return urls


def _compact_notification(result, *, vin, url):
    actual_vin,ranges = _compact_context(result,vin)
    _compact_details(result)
    main = _compact_url(url,'docx')
    return {'text':main + '\nVIN：' + actual_vin + '\nBase：' + ranges['baseline']
            + '\nPCM更新：' + ranges['replacement']}


def _compact_details_table(result, urls):
    sources = result.get('local_sources') or {}
    if not isinstance(sources,dict):
        raise InputError('精简报告来源质量记录格式无效。')
    rows = []
    for role,label in SIDE_LABELS:
        source = sources.get(role) or {}
        quality = (source.get('source_quality') or {}) if isinstance(source,dict) else None
        if not isinstance(source,dict) or not isinstance(quality,dict):
            raise InputError('精简报告来源质量记录格式无效。')
        note = ('<p>MCU 来源存在不完整日志，相关统计仅供参考。</p>'
                if quality.get('status') == 'partial' else '')
        rows.append('<tr><td vertical-align="top"><p>' + label
            + '</p></td><td vertical-align="top"><p><a href="'
            + escape(urls[role],quote=True) + '">查看完整原生明细</a></p>' + note + '</td></tr>')
    return ('<table><colgroup><col width="100"/><col width="690"/></colgroup><tbody>'
            + ''.join(rows) + '</tbody></table>')


def _source_quality_notes(result):
    """Describe only recorded source integrity evidence, never missing-window guesses."""
    sources = result.get('local_sources', {})
    if not isinstance(sources,dict):
        raise InputError('精简报告来源质量记录格式无效。')
    notes = []
    for role,label in SIDE_LABELS:
        source = sources.get(role, {})
        if not isinstance(source,dict):
            raise InputError('精简报告来源质量记录格式无效。')
        quality = source.get('source_quality', {})
        if not isinstance(quality,dict):
            raise InputError('精简报告来源质量记录格式无效。')
        if quality.get('status') != 'partial':
            continue
        issues = quality.get('issues', [])
        if not isinstance(issues,list) or any(not isinstance(issue,dict) for issue in issues):
            raise InputError('精简报告来源质量问题清单格式无效。')
        recovered = [issue for issue in issues if issue.get('kind') == 'incomplete_zstd_frame'
                     and issue.get('recovery') == 'complete_decoded_lines'
                     and type(issue.get('discarded_tail_bytes')) is int
                     and issue['discarded_tail_bytes'] >= 0]
        recovered_classes = {'MCU': 0, 'BZCU': 0, '来源': 0}
        for issue in recovered:
            relative = issue.get('relative_path')
            parts = relative.split('/') if isinstance(relative,str) else []
            safe = (len(parts) > 1 and '\\' not in relative and ':' not in relative
                    and not any(ord(char) < 32 for char in relative)
                    and all(part and part not in ('.','..') for part in parts))
            source_class = {'log_xcu_mcu': 'MCU', 'log_bzcu_eth': 'BZCU'}.get(parts[0], '来源') if safe else '来源'
            recovered_classes[source_class] += 1
        for source_class,count in recovered_classes.items():
            if count:
                notes.append(label + '：' + str(count) + '份' + source_class
                    + '压缩日志缺少完整结束帧，已保留完整解码行；日志完整性未确认。')
        if not issues or len(recovered) != len(issues):
            notes.append(label + '：来源日志完整性未确认，相关统计需结合来源审计核查。')
    return notes


def _source_details_paragraph(result, urls):
    text = ('<p><b>数据来源：</b><a href="' + escape(urls['baseline'],quote=True)
            + '">Baseline 表格</a>　<a href="' + escape(urls['replacement'],quote=True)
            + '">PCM更新表格</a>')
    return text + ''.join('<br/>' + escape(note) for note in _source_quality_notes(result)) + '</p>'


def _missing_cell(pair: dict, side: str) -> str:
    if pair.get("status", "ok") in {"ok", "missing"} and pair.get(side + "_status") == "empty":
        return "<p align=\"center\">-</p>"
    return f'<p align="center"><span text-color="red">{UNPARSED_LABEL}</span></p>'


def _number(value) -> str:
    try:
        number = round(float(value), 2)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(number):
        return "-"
    if number == 0:
        number = 0.0
    return f"{number:.2f}".rstrip("0").rstrip(".")




def _side_number(pair: dict, side: str) -> str:
    """Expose known sides of a missing pair, but never error/stale values."""
    if pair.get("status") not in {"ok", "missing"}:
        return "-"
    value = pair.get(side)
    if isinstance(value, bool):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return _number(number) if math.isfinite(number) and number >= 0 else "-"


def _replacement_cell(pair: dict) -> str:
    if pair.get("status", "ok") not in {"ok", "missing"} or finite_number(pair.get("replacement")) is None:
        return _missing_cell(pair, "replacement")
    value = escape(_number(pair.get("replacement")))
    if (pair.get("status") != "ok" or finite_number(pair.get("baseline")) is None
            or not isinstance(pair.get("delta"), (int, float)) or isinstance(pair.get("delta"), bool)
            or not math.isfinite(pair["delta"])):
        return f'<p align="center">{value}</p>'
    delta = _number(pair.get("delta"))
    if delta != "-" and not delta.startswith("-") and delta != "0":
        delta = "+" + delta
    # Tone accounts for each metric's direction: improved green, regressed red.
    tone = pair.get("tone")
    if delta not in {"-", "0"} and tone in {"improved", "regressed"}:
        color = "green" if tone == "improved" else "red"
        delta_xml = f'<span text-color="{color}">({escape(delta)})</span>'
    else:
        delta_xml = f"({escape(delta)})"
    return f'<p align="center">{value} {delta_xml}</p>'


def _baseline_cell(pair: dict) -> str:
    if pair.get("status", "ok") not in {"ok", "missing"} or finite_number(pair.get("baseline")) is None:
        return _missing_cell(pair, "baseline")
    value = _number(pair.get("baseline"))
    return f'<p align="center">{escape(value)}</p>'


def _environment_evidence(pipeline: dict, role: str, source: dict) -> tuple[str, str]:
    """Render only known environment names and the validated selection facts."""
    window = pipeline.get('windows',{}).get(role,{})
    actual = window.get('environment',source.get('environment'))
    environment = actual if actual in ('testtwo','prod') else '未记录'
    decision = window.get('environment_selection') or {}
    attempts = decision.get('attempts') or []
    first = attempts[0] if attempts and isinstance(attempts[0],dict) else {}
    counts = first.get('counts') or {}
    empty_test = (first.get('environment') == 'testtwo' and first.get('pcm_status') == 'ok'
                  and set(counts) == {'log_fsdA','log_fsdB','log_xcu_mcu','log_bzcu_eth','log_fsdA_service'}
                  and all(type(value) is int and value == 0 for value in counts.values()))
    if (environment == 'prod' and decision.get('policy') == 'test_then_prod'
            and decision.get('environment') == 'prod' and empty_test):
        return environment, '回退原因：testtwo 的 fsdA、fsdB、XCU、BZCU 与 AVM 所需候选均为空，本时段改用 prod。'
    if environment == 'prod' and decision.get('policy') == 'prod':
        return environment, '按配置直接使用 prod，本时段未执行空结果回退。'
    if environment == 'testtwo':
        return environment, '本时段使用 testtwo，未发生环境回退。'
    return environment, ''


def build_legacy_report_document(result: dict) -> dict:
    """Render only the comparison table; extraction warnings stay in cells."""
    title = str(result.get("title") or "走查测试")
    comparisons = result.get("comparisons") or {}

    first_header = ['<th rowspan="2" background-color="light-gray" vertical-align="middle"><p align="center"><b>车辆模式</b></p></th>']
    second_header = []
    for _, label in NOTIFICATION_METRICS:
        first_header.append(
            f'<th colspan="2" background-color="light-gray" vertical-align="middle">'
            f'<p align="center"><b>{escape(label)}</b></p></th>'
        )
        second_header.extend((
            '<th background-color="light-gray"><p align="center"><b>base</b></p></th>',
            '<th background-color="light-gray"><p align="center"><b>PCM更新</b></p></th>',
        ))

    body = []
    for scenario in SCENARIOS:
        row = comparisons.get(scenario) or {}
        label = escape(str(row.get("label") or scenario))
        cells = [f'<td vertical-align="middle"><p align="center"><b>{label}</b></p></td>']
        metrics = row.get("metrics") or {}
        for metric, _ in METRICS:
            pair = metrics.get(metric) or {}
            cells.append(f'<td vertical-align="middle">{_baseline_cell(pair)}</td>')
            cells.append(f'<td vertical-align="middle">{_replacement_cell(pair)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    xml = "".join((
        f"<title>{escape(title)}</title>",
        "<table>",
        '<colgroup><col width="100"/><col span="12" width="115"/></colgroup>',
        "<thead><tr>", "".join(first_header), "</tr><tr>", "".join(second_header), "</tr></thead>",
        "<tbody>", "".join(body), "</tbody></table>",
    ))
    return {
        "format": "lark_docx_xml",
        "title": title,
        "xml": xml,
        "source_links": [],
    }


def _native_detail_url(entry: dict) -> str:
    value = str(entry.get("url") or "")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or
            not parsed.hostname.endswith(".feishu.cn") or
            not __import__("re").fullmatch(r"/sheets/[A-Za-z0-9_-]+", parsed.path) or
            parsed.username or parsed.password):
        raise InputError("主报告缺少有效的原生飞书明细链接。")
    return value


def _template_table(result: dict) -> str:
    """Fill the supplied report's structure; keep widths, headers and merges."""
    template = Path(__file__).resolve().parents[1] / "assets" / "report-table.xml"
    table = ET.fromstring(template.read_text(encoding="utf-8"))
    rows = table.findall(".//tr")
    labels = ["低速人驾", "高速人驾", "泊车", "CNOA"]
    if (len(rows) != 6 or
            ["".join(row[0].itertext()) for row in rows[-4:]] != labels or
            any(len(row) != 13 for row in rows[-4:])):
        raise InputError("走查主报告模板结构变化，禁止生成错位的统计表。")
    for row_index, scenario in enumerate(SCENARIOS):
        metrics = (result.get("comparisons", {}).get(scenario) or {}).get("metrics", {})
        for metric_index, (metric, _) in enumerate(METRICS):
            pair = metrics.get(metric) or {}
            for offset, render in ((1, _baseline_cell), (2, _replacement_cell)):
                column = metric_index * 2 + offset
                cell = rows[-4 + row_index][column]
                placeholder = "{{cell_" + str(row_index) + "_" + str(column) + "}}"
                if "".join(cell.itertext()) != placeholder:
                    raise InputError("走查模板存在旧值或占位错位。")
                old = list(cell)
                rendered = ET.fromstring(render(pair))
                if len(old) == 1:
                    rendered.attrib.update(old[0].attrib)
                for child in old:
                    cell.remove(child)
                cell.append(rendered)
    return ET.tostring(table, encoding="unicode")


def build_report_document(result: dict) -> dict:
    """Preserve the target's table-only body; native links travel in one reply."""
    style = _detail_links_style(result)
    if result.get('presentation_version') == PRESENTATION_V2:
        _compact_context(result)
        urls = _compact_details(result)
        title = str(result.get('title') or '走查测试')
        xml = ('<title>' + escape(title) + '</title>' + _template_table(result)
               + (_source_details_paragraph(result,urls) if style == SOURCE_PARAGRAPH
                  else _compact_details_table(result,urls)))
        return {'format':'lark_docx_xml','title':title,'xml':xml,
                'source_links':[urls['baseline'],urls['replacement']],
                **({'detail_links_style':SOURCE_PARAGRAPH} if style == SOURCE_PARAGRAPH else {}),
                'presentation_version':PRESENTATION_V2}
    if result.get("delivery_format") != "docx_with_native_sheet_details":
        return build_legacy_report_document(result)
    details = result.get("native_details") or {}
    if set(details) != {"baseline", "replacement"}:
        raise InputError("两份原生飞书明细尚未校验完成，不能创建主报告。")
    urls = {role: _native_detail_url(details[role]) for role in details}
    if urls["baseline"] == urls["replacement"]:
        raise InputError("Base 与 PCM更新明细不能指向同一份表。")
    title = str(result.get("title") or "走查测试")
    xml = "<title>" + escape(title) + "</title>" + _template_table(result)
    return {"format": "lark_docx_xml", "title": title, "xml": xml,
            "source_links": [urls["baseline"], urls["replacement"]]}


def verify_main_report_document(actual_xml: str, expected_document: dict) -> dict:
    """Read back all 48 main cells, template structure, title, deltas and colors.

    Target policy keeps the body table-only. The independently governed Sheets
    links remain document metadata and are sent in the single source reply.
    """
    import hashlib
    def parse(text):
        try:
            return ET.fromstring("<root>" + text + "</root>")
        except (ET.ParseError, TypeError):
            raise InputError("主报告回读内容不是有效 DocxXML。") from None
    actual, expected = parse(actual_xml), parse(expected_document["xml"])
    def canonical(node):
        node = copy.deepcopy(node)
        for item in node.iter():
            item.attrib.pop("id", None)
            if "text-color" in item.attrib:
                item.set("text-color", {"rgb(46,161,33)":"green", "rgb(216,57,49)":"red"}.get(
                    item.get("text-color"), item.get("text-color")))
        return ET.tostring(node, encoding="unicode")
    compact = expected_document.get('presentation_version') == PRESENTATION_V2
    paragraph = _detail_links_style(expected_document) == SOURCE_PARAGRAPH
    tags = (['title','table','p'] if paragraph else ['title','table','table']) if compact else ['title','table']
    if ([item.tag for item in actual] != tags or
            [item.tag for item in expected] != tags or
            canonical(actual.find("title")) != canonical(expected.find("title")) or
            canonical(actual.find("table")) != canonical(expected.find("table"))):
        raise InputError("主报告标题、48项数值/差值/颜色或模板结构与本次结果不一致。")
    table = actual.find("table")
    rows = table.findall(".//tr")[-4:]
    if (len(rows) != 4 or any(len(row) != 13 for row in rows) or
            actual.findall(".//img") or table.findall('.//a') or
            not compact and actual.findall(".//a")):
        raise InputError("主报告未满足纯表格、13列48格及无图要求。")
    source_links = expected_document.get("source_links") or []
    links_verified = 0
    if compact:
        if (len(source_links) != 2 or source_links[0] == source_links[1]
                or canonical(actual[2]) != canonical(expected[2])):
            raise InputError('精简报告明细入口表或来源链接与本次结果不一致。')
        anchors = actual[2].findall('.//a')
        if (len(anchors) != 2 or len(actual.findall('.//a')) != 2
                or [anchor.get('href') for anchor in anchors] != source_links):
            raise InputError('精简报告必须保留两个角色明确且可进入的真实明细链接。')
        if paragraph:
            if [''.join(a.itertext()) for a in anchors] != ['Baseline 表格','PCM更新表格']:
                raise InputError('数据来源段落的角色标签与本次结果不一致。')
        else:
            detail_rows = actual[2].findall('.//tr')
            if (len(detail_rows) != 2 or any(len(row) != 2 for row in detail_rows)
                    or [''.join(row[0].itertext()) for row in detail_rows] != ['Base','PCM更新']):
                raise InputError('精简报告必须保留两个角色明确且可进入的真实明细链接。')
        for link in source_links:_compact_url(link,'sheets')
        links_verified = 2
    if source_links:
        if len(source_links) != 2 or source_links[0] == source_links[1]:
            raise InputError("主报告元数据缺少两份独立明细链接。")
        for url in source_links:
            _native_detail_url({"url": url})
    return {"status":"ok", "data_cells_verified":48, "native_detail_links_verified":links_verified,
            "native_detail_metadata_links_validated":len(source_links), "images":0,
            "all_values_delta_colors_template_and_sources_verified":True,
            "expected_xml_sha256":hashlib.sha256(expected_document["xml"].encode()).hexdigest()}

