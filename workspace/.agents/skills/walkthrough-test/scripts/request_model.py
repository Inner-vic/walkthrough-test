"""Request parsing reused from the provided deployment baseline; no message or Sheets IO."""
from __future__ import annotations

import hashlib

import json

import re

import unicodedata

from datetime import datetime, timezone

SCENARIOS = (
    ("slow_driver", "低速人驾", "vla", "header_delay"),
    ("fast_driver", "高速人驾", "vla", "header_delay"),
    ("CNOA", "城区智驾", "vla", "header_delay"),
    ("parking", "泊车", "vlaparking", "vla_parking_header_delay"),
)

COMPARISON_CONTEXT_FIELDS = ("vin", "start", "end")

TIME_TOKEN = (
    r"(?:\d{14}|\d{12}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[ T]"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)"
)

TIME_RANGE_PATTERN = re.compile(
    rf"(?P<start>{TIME_TOKEN})\s*(?:/|,|，|~|～|至|到|—|–|-)+\s*(?P<end>{TIME_TOKEN})"
)

LABELED_VIN_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9_])vin(?:码|号)?\s*[:：=]?\s*([A-Za-z0-9_-]+)")

UNLABELED_VIN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-HJ-NPR-Za-hj-npr-z0-9]{17}(?![A-Za-z0-9])")
VIN_CANDIDATE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{17}(?![A-Za-z0-9])")
VIN_VALUE_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.IGNORECASE)

ROLE_TOKEN = (
    r"(?:baseline|base|基线|刷包前|替换前|replacement|PCM\s*更新|替换后|刷包后|更新后|替换小包|小包后)"
)

ROLE_LABEL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<label>{ROLE_TOKEN})(?![A-Za-z0-9_])", re.IGNORECASE
)

FIRST_WINDOW = r"(?:第\s*(?:一|1)\s*(?:个|段|组|时段|时间段)?|前者|前一\s*(?:个|段|组|时段|时间段)?)"

SECOND_WINDOW = r"(?:第\s*(?:二|2)\s*(?:个|段|组|时段|时间段)?|后者|后一\s*(?:个|段|组|时段|时间段)?)"

ORDINAL_ROLE_PATTERNS = (
    re.compile(
        rf"(?P<ordinal>{FIRST_WINDOW}|{SECOND_WINDOW})\s*(?:是|为|作为|指定为|设为|=|:|：)?\s*"
        rf"(?P<role>{ROLE_TOKEN})(?![A-Za-z0-9_])",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9_])(?P<role>{ROLE_TOKEN})\s*(?:是|为|作为|指定为|设为|=|:|：)?\s*"
        rf"(?P<ordinal>{FIRST_WINDOW}|{SECOND_WINDOW})",
        re.IGNORECASE,
    ),
)

class InputError(ValueError):
    pass

def compact(value):
    """Canonicalize labels received from Sheets without changing visible text semantics."""
    normalized = unicodedata.normalize("NFKC", str(value if value is not None else ""))
    return "".join(
        char for char in normalized
        if not char.isspace() and unicodedata.category(char) not in {"Cf", "Cc"}
    ).casefold()

def comparison_context(metadata, side):
    """Validate the request context needed to compare two result windows."""
    if not isinstance(metadata, dict):
        raise InputError(f"{side} metadata 必须是 JSON 对象。")
    context = {}
    for field in COMPARISON_CONTEXT_FIELDS:
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InputError(f"{side} metadata 缺少非空 {field}；对比必须明确同一 VIN 的两个时间段。")
        context[field] = value.strip()
    return context

def parse_time_token(value):
    """Parse common copied Feishu/log time forms only for ordering two request windows."""
    if re.fullmatch(r"\d{14}", value):
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    elif re.fullmatch(r"\d{12}", value):
        parsed = datetime.strptime(value, "%Y%m%d%H%M")
    else:
        normal = value.replace("/", "-").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normal)
        except ValueError:
            raise InputError(f"无法识别时间 {value!r}；请使用 YYYY-MM-DD HH:MM:SS 或 YYYYMMDDHHMMSS。") from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None), parsed.isoformat()
    return parsed, parsed.isoformat()

def _role_name(value):
    normalized = compact(value)
    if normalized in {"baseline", "base", "基线", "刷包前", "替换前"}:
        return "baseline"
    return "replacement"

def _ordinal_index(value):
    normalized = compact(value)
    return 1 if re.search(r"(?:二|2|后者|后一)", normalized) else 0

def _explicit_window_roles(text, matches):
    """Return roles in source-window order when the user labels either side."""
    roles = [None, None]

    def assign(index, role):
        current = roles[index]
        if current is not None and current != role:
            raise InputError(f"第{index + 1}个时间段同时被标记为 Baseline 和替换后。")
        roles[index] = role

    # Support explicit ordinal wording such as “第一个是替换后，第二个是 base”.
    for pattern in ORDINAL_ROLE_PATTERNS:
        for marker in pattern.finditer(text):
            assign(_ordinal_index(marker.group("ordinal")), _role_name(marker.group("role")))

    # The common copied form puts a role label before each time range, possibly
    # with a VIN between the label and the range.  Limit each prefix to the text
    # following the previous range so labels cannot leak across windows.
    for index, match in enumerate(matches):
        prefix_start = matches[index - 1].end() if index else 0
        prefix = text[prefix_start:match.start()]
        labels = []
        for label in ROLE_LABEL_PATTERN.finditer(prefix):
            before = prefix[:label.start()]
            # “<previous range> 为 base” belongs to the previous range, not
            # to the next range whose prefix happens to contain that text.
            if index and re.search(r"(?:是|为|作为|指定为|设为|=)\s*$", before):
                continue
            labels.append(label)
        if len(labels) == 1:
            assign(index, _role_name(labels[0].group("label")))

        # Also accept trailing forms such as “<time range> 为 base”.  A plain
        # “替换后：<next range>” is intentionally not treated as a suffix.
        suffix_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        suffix = text[match.end():suffix_end]
        trailing = re.match(
            rf"\s*(?:(?:是|为|作为|指定为|设为|=)\s*(?P<plain>{ROLE_TOKEN})|"
            rf"[（(]\s*(?P<bracket>{ROLE_TOKEN})\s*[)）])",
            suffix,
            re.IGNORECASE,
        )
        if trailing:
            assign(index, _role_name(trailing.group("plain") or trailing.group("bracket")))

    if not any(roles):
        return None
    if roles[0] is None:
        roles[0] = "replacement" if roles[1] == "baseline" else "baseline"
    if roles[1] is None:
        roles[1] = "replacement" if roles[0] == "baseline" else "baseline"
    if roles[0] == roles[1]:
        label = "Baseline" if roles[0] == "baseline" else "替换后"
        raise InputError(f"两个时间段不能都标记为 {label}；请分别指定 Baseline 和替换后。")
    return roles

def parse_comparison_request(text):
    """Normalize a copied comparison request before any cards or sheets are read."""
    if not isinstance(text, str) or not text.strip():
        raise InputError("对比请求不能为空。")
    vin_values = [match.group(1) for match in LABELED_VIN_PATTERN.finditer(text)]
    # A labeled identifier does not make another vehicle in the same message
    # disappear. Validate every explicit value and every bare VIN candidate.
    vin_values.extend(match.group(0) for match in VIN_CANDIDATE_PATTERN.finditer(text))
    if any(not VIN_VALUE_PATTERN.fullmatch(value) for value in vin_values):
        raise InputError("VIN 必须为 17 位字母数字，且不能包含 I、O、Q 或连接符。")
    vins = {value.strip().upper() for value in vin_values if value.strip()}
    if not vins:
        raise InputError("未识别到 VIN；请写明 VIN 或 VIN 码。")
    if len(vins) != 1:
        raise InputError("对比请求包含多个 VIN，Baseline 与替换后必须是同一 VIN。")
    matches = list(TIME_RANGE_PATTERN.finditer(text))
    windows = []
    for match in matches:
        start_at, start = parse_time_token(match.group("start"))
        end_at, end = parse_time_token(match.group("end"))
        if start_at >= end_at:
            raise InputError("每个时间段的起点必须早于终点。")
        windows.append({"start_at": start_at, "end_at": end_at, "start": start, "end": end})
    if len(windows) != 2:
        raise InputError("需要恰好两个时间段；每段使用“起点/终点”或“起点 - 终点”。")
    chronological = sorted(windows, key=lambda item: (item["start_at"], item["end_at"]))
    if chronological[0]["end_at"] > chronological[1]["start_at"]:
        raise InputError("两个时间段重叠，无法执行对比。")
    explicit_roles = _explicit_window_roles(text, matches)
    if explicit_roles:
        baseline = windows[explicit_roles.index("baseline")]
        replacement = windows[explicit_roles.index("replacement")]
        input_roles = explicit_roles
        role_source = "explicit"
        role_assignment = "按用户显式指定确定 Baseline 和替换后。"
    else:
        baseline, replacement = windows
        input_roles = ["baseline", "replacement"]
        role_source = "input_order_default"
        role_assignment = "用户未指定角色；默认第一条时间段为 Base，第二条为 PCM更新，不按时间早晚重排。"
    vin = vins.pop()

    def metadata(window):
        return {"vin": vin, "start": window["start"], "end": window["end"]}

    key_material = json.dumps([vin, baseline["start"], baseline["end"],
                               replacement["start"], replacement["end"]], ensure_ascii=False)
    return {
        "schema_version": 1,
        "kind": "ad_p99_comparison_request",
        "status": "ready",
        "comparison_key": hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16],
        "vin": vin,
        "baseline_metadata": metadata(baseline),
        "replacement_metadata": metadata(replacement),
        "input_roles": input_roles,
        "role_assignment_source": role_source,
        "role_assignment": role_assignment,
    }
