"""Portable native statistics and original no-image template rendering.

No spreadsheet, plotting, image encoding, HTTP client or analyzer constructor is
used here. Renderer code is taken from the approved external analyzer and pinned
by its complete canonical AST before it can be executed on the publisher host.
"""
from __future__ import annotations

import ast
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from analyzer_contract import CONTRACT_FINGERPRINT, ast_digest, validate_analyzer_contract

SCHEMA = 'm100-native-sheet-data-v1'
PLAN_SCHEMA = 'm100-native-sheet-plan-v1'
RENDERER_PINS = {
    'update_values': 'c6378c3e8706151fe687eaea3fa111d4a7bf24936300f5dcca98f2e632cacd53',
    'parse_sheet_coordinate': 'b49cfac9a8a19d2de7412192be76d60d7a0750ce7c25706e7681326b24eba381',
}


class NativeSheetDataError(ValueError):
    pass


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _portable(value):
    if hasattr(value, 'item'):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NativeSheetDataError('原生数据含未归一化的非有限值。')
        return value
    if isinstance(value, (list, tuple)):
        return [_portable(x) for x in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: _portable(v) for k, v in value.items()}
    raise NativeSheetDataError('原生数据包含不可移植对象：' + type(value).__name__)


def renderer_bundle(source_root):
    """Extract unchanged original methods after checking the full source family."""
    root = Path(source_root)
    contract = validate_analyzer_contract(root)
    plot_path, utils_path = root/'common/perf_plot.py', root/'common/perf_utils.py'
    plot = ast.parse(plot_path.read_text(encoding='utf-8-sig'))
    cls = next(n for n in plot.body if isinstance(n, ast.ClassDef) and n.name == 'FeishuTemplateReport')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'update_values')
    utils = ast.parse(utils_path.read_text(encoding='utf-8-sig'))
    helper = next(n for n in utils.body if isinstance(n, ast.FunctionDef) and n.name == 'parse_sheet_coordinate')
    bundle = {'profile': 'approved-original-native-template-methods-v1',
        'source_contract_fingerprint': contract['contract_fingerprint'],
        'source_files_sha256': {
            'common/perf_plot.py': hashlib.sha256(plot_path.read_bytes()).hexdigest(),
            'common/perf_utils.py': hashlib.sha256(utils_path.read_bytes()).hexdigest()},
        'methods': {n.name: {'source': ast.unparse(n), 'ast_sha256': ast_digest(n)} for n in (method, helper)}}
    _renderer_nodes(bundle)
    return bundle


def _renderer_nodes(bundle):
    if not isinstance(bundle, dict) or bundle.get('profile') != 'approved-original-native-template-methods-v1':
        raise NativeSheetDataError('原生模板渲染来源缺失或版本不支持。')
    if bundle.get('source_contract_fingerprint') != CONTRACT_FINGERPRINT:
        raise NativeSheetDataError('原生模板渲染来源不属于已批准的分析器契约。')
    methods = bundle.get('methods')
    if not isinstance(methods, dict) or set(methods) != set(RENDERER_PINS):
        raise NativeSheetDataError('原生模板渲染方法清单变化。')
    nodes = {}
    for name, pin in RENDERER_PINS.items():
        item = methods[name]
        try:
            tree = ast.parse(item['source'])
        except (KeyError, TypeError, SyntaxError) as error:
            raise NativeSheetDataError('原生模板方法不能进行 AST 核验。') from error
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != name:
            raise NativeSheetDataError('拒绝额外的模板渲染可执行代码。')
        node = tree.body[0]
        if item.get('ast_sha256') != pin or ast_digest(node) != pin:
            raise NativeSheetDataError('模板方法超出已批准的原生 AST；需要适配和回放。')
        nodes[name] = node
    return nodes


def build_native_sheet_data(rd, statistics, rules, metadata, renderer, native_template_reference):
    """Keep exact native publication data and every dynamic rule outcome."""
    result = {'schema': SCHEMA, 'no_graph': True, 'plot_generation': False,
        'excel_generation': False, 'rd': _portable(rd), 'statistics': _portable(statistics),
        'rules': _portable(rules), 'metadata': _portable(metadata),
        'renderer_bundle': _portable(renderer),
        'native_template_reference': _portable(native_template_reference)}
    for key in ('rd', 'statistics', 'rules', 'metadata', 'renderer_bundle', 'native_template_reference'):
        result[key + '_sha256'] = canonical_sha256(result[key])
    result['content_sha256'] = canonical_sha256(result)
    validate_native_sheet_data(result)
    return result


def bind_native_sheet_metadata(data, metadata_updates, delivery_context=None, statistics=None):
    """Bind the parent window context without touching native values or rules."""
    validate_native_sheet_data(data)
    updates = _portable(metadata_updates)
    if not isinstance(updates, dict):
        raise NativeSheetDataError('原生明细交付元数据须为字典。')
    result = copy.deepcopy(data)
    if statistics is not None:
        annotated = _portable(statistics)
        if not isinstance(annotated, list) or len(annotated) != len(data['statistics']):
            raise NativeSheetDataError('最终统计诊断清单不能增删或重排原生规则。')
        allowed_annotations = {'input_diagnostic', 'source_quality', 'source_quality_diagnostic'}
        for before, after in zip(data['statistics'], annotated):
            if (not isinstance(after, dict) or set(after) - set(before) - allowed_annotations
                    or any(key not in after or after[key] != value for key, value in before.items())):
                raise NativeSheetDataError('最终统计只能追加来源诊断，不能改变原生值、规则、状态或顺序。')
        result['statistics'] = annotated
        result['statistics_sha256'] = canonical_sha256(annotated)
    result['metadata'].update(updates)
    request = result['metadata'].get('request')
    if not isinstance(request, dict) or not all(isinstance(request.get(k), str) for k in ('vin', 'start', 'end')):
        raise NativeSheetDataError('原生明细交付须绑定完整请求。')
    try:
        interval = '/'.join(datetime.fromisoformat(request[k]).strftime('%Y%m%d%H%M%S') for k in ('start', 'end'))
    except ValueError as error:
        raise NativeSheetDataError('原生明细交付请求时间无效。') from error
    if request['vin'] != data['metadata']['vin'] or interval != data['metadata']['time_range']:
        raise NativeSheetDataError('交付请求与实际原生统计车辆或时段不符。')
    request_sha = canonical_sha256(request)
    if result['metadata'].get('request_sha256', request_sha) != request_sha:
        raise NativeSheetDataError('交付请求摘要不一致。')
    result['metadata']['request_sha256'] = request_sha
    result['metadata']['rules_content_sha256'] = data['rules_sha256']
    if result['metadata'].get('environment') not in ('testtwo', 'prod'):
        raise NativeSheetDataError('原生明细交付环境未绑定。')
    if not re.fullmatch(r'[0-9a-f]{64}', str(result['metadata'].get('rules_sha256', ''))):
        raise NativeSheetDataError('原生明细交付缺少实际规则文件摘要。')
    if delivery_context is not None:
        result['delivery_context'] = _portable(delivery_context)
    result['metadata_sha256'] = canonical_sha256(result['metadata'])
    result.pop('content_sha256')
    result['content_sha256'] = canonical_sha256(result)
    return validate_native_sheet_data(result)


def validate_native_sheet_data(data):
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or data.get('no_graph') is not True:
        raise NativeSheetDataError('缺少受检的无图原生明细契约。')
    if data.get('plot_generation') is not False or data.get('excel_generation') is not False:
        raise NativeSheetDataError('原生明细契约不得依赖绘图或 Excel。')
    for key in ('rd', 'statistics', 'rules', 'metadata', 'renderer_bundle', 'native_template_reference'):
        if data.get(key + '_sha256') != canonical_sha256(data.get(key)):
            raise NativeSheetDataError('原生明细字段摘要不一致：' + key)
    unsigned = {k: v for k, v in data.items() if k != 'content_sha256'}
    if data.get('content_sha256') != canonical_sha256(unsigned):
        raise NativeSheetDataError('原生明细完整数据摘要不一致。')
    rd, rules, statistics = data['rd'], data['rules'], data['statistics']
    if not isinstance(rd, dict) or not isinstance(rd.get('title'), str) or not isinstance(rd.get('version'), str):
        raise NativeSheetDataError('缺少原生实际标题或版本字段。')
    for key, value in rd.items():
        if isinstance(value, dict) and value.get('image') is not None:
            raise NativeSheetDataError('无图明细中不允许携带图片路径或图像数据：' + key)
    if not isinstance(rules, dict) or not rules or not all(isinstance(x, dict) and x for x in rules.values()):
        raise NativeSheetDataError('原生明细必须保留完整动态规则。')
    expected = {(theme, metric) for theme, values in rules.items() for metric in values}
    if not isinstance(statistics, list):
        raise NativeSheetDataError('完整统计清单缺失。')
    actual = {}
    for row in statistics:
        if not isinstance(row, dict) or not isinstance(row.get('theme'), str) or not isinstance(row.get('metric'), str):
            raise NativeSheetDataError('完整统计行格式无效。')
        identity = (row['theme'], row['metric'])
        if identity in actual:
            raise NativeSheetDataError('完整统计中出现同主题同指标重复行。')
        actual[identity] = row
        if identity in expected and row.get('rule') != rules[identity[0]][identity[1]]:
            raise NativeSheetDataError('完整统计行绑定的规则不一致。')
        if not isinstance(row.get('statistics'), dict) or not isinstance(row.get('native_publish'), bool):
            raise NativeSheetDataError('完整统计值或原生上报标记缺失。')
    if not expected <= actual.keys():
        raise NativeSheetDataError('原生完整动态规则存在未保留的统计行。')
    if any(identity not in expected and row.get('unconfigured_output') is not True for identity, row in actual.items()):
        raise NativeSheetDataError('未配置的原生输出须明确标识。')
    template = data['native_template_reference']
    if not isinstance(template, dict) or not all(isinstance(template.get(x), str) and template[x].strip() for x in ('token', 'sheet_id')):
        raise NativeSheetDataError('缺少本次原生 generate_report 的源模板引用。')
    if not isinstance(data['metadata'], dict) or not all(isinstance(data['metadata'].get(x), str) and data['metadata'][x] for x in ('vin', 'time_range')):
        raise NativeSheetDataError('原生明细必须绑定车辆和请求时段。')
    if data['metadata'].get('rules_content_sha256', data['rules_sha256']) != data['rules_sha256']:
        raise NativeSheetDataError('元数据与实际规则内容摘要不符。')
    _renderer_nodes(data['renderer_bundle'])
    return data


def write_native_sheet_data(path, data):
    validate_native_sheet_data(data)
    path = Path(path)
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
            raise NativeSheetDataError('原生明细路径已有不同内容，拒绝覆盖。')
    else:
        with path.open('xb') as stream:
            stream.write(raw)
    return {'schema': SCHEMA, 'path': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
        'bytes': len(raw), 'content_sha256': data['content_sha256'],
        'statistics_sha256': data['statistics_sha256'], 'rules_sha256': data['rules_sha256'],
        'native_template_reference': copy.deepcopy(data['native_template_reference'])}


def load_native_sheet_data(path, expected_sha256=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise NativeSheetDataError('原生明细文件须为普通文件。')
    raw = path.read_bytes()
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise NativeSheetDataError('原生明细文件 SHA 不一致。')
    return validate_native_sheet_data(json.loads(raw.decode('utf-8')))


def _original_renderer(bundle):
    nodes = _renderer_nodes(bundle)
    def image_forbidden(*args, **kwargs):
        raise NativeSheetDataError('无图模板填充不允许读取、编码或上传图片。')
    namespace = {'Path': Path, 'img_to_stream': image_forbidden}
    helper = ast.Module(body=[copy.deepcopy(nodes['parse_sheet_coordinate'])], type_ignores=[])
    exec(compile(helper, '<approved-native-coordinate>', 'exec'), namespace)
    wrapper = ast.ClassDef(name='NativeTemplateOnly', bases=[], keywords=[],
        body=[copy.deepcopy(nodes['update_values'])], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(module, '<approved-native-update-values>', 'exec'), namespace)
    renderer = namespace['NativeTemplateOnly']()
    renderer.key_char, renderer.split_char = '&', '@'
    return renderer, namespace['parse_sheet_coordinate']


def build_native_sheet_plan(data, template_sheets):
    """Use the exact original placeholder method on independent template copies."""
    validate_native_sheet_data(data)
    if not isinstance(template_sheets, list) or len(template_sheets) != 7:
        raise NativeSheetDataError('须提供七张完整原生模板页。')
    ids, names = [x.get('sheet_id') for x in template_sheets], [x.get('sheet_name') for x in template_sheets]
    if len(set(ids)) != 7 or len(set(names)) != 7 or not all(isinstance(x, str) and x for x in ids + names):
        raise NativeSheetDataError('原生模板页 ID 或名称不唯一。')
    renderer, coordinate = _original_renderer(data['renderer_bundle'])
    represented, sheets = set(), []
    for sheet in template_sheets:
        rows, cols = sheet.get('row_count'), sheet.get('column_count')
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1 or not isinstance(cols, int) or cols < 15:
            raise NativeSheetDataError('原生模板页的完整行列范围缺失。')
        keymap = copy.deepcopy(sheet.get('keymap'))
        areas = keymap.get('valueRanges') if isinstance(keymap, dict) else None
        if not isinstance(areas, list) or len(areas) != 1:
            raise NativeSheetDataError('每张原生模板页须提供完整 B:O 单一区间。')
        area = areas[0]
        if (not isinstance(area, dict) or not isinstance(area.get('range'), str)
                or not re.fullmatch(r'[^!]+!B1:O' + str(rows), area['range'])):
            raise NativeSheetDataError('原生模板页不是完整 B1:O末行范围。')
        values = area.get('values')
        if not isinstance(values, list) or len(values) != rows or any(not isinstance(r, list) or len(r) != 14 for r in values):
            raise NativeSheetDataError('原生模板页存在缺失行列。')
        images, placeholders = [], []
        for r, line in enumerate(values, 1):
            for c, cell in enumerate(line, 1):
                if isinstance(cell, str) and not cell.strip():
                    line[c-1] = None  # original method indexes j[0]; native blank is None
                    continue
                if cell is not None and not isinstance(cell, (str, int, float, bool)):
                    raise NativeSheetDataError('模板输入须保持原 get_values_batch 的值/公式/None 形式。')
                if isinstance(cell, str) and cell.startswith('&'):
                    paths = [part.split('@') for part in cell.split('&')[1:] if part and not part.startswith('=')]
                    represented.update(path[0] for path in paths)
                    anchor = coordinate((sheet['sheet_id'], (c, r), (c, r)))
                    placeholders.append({'range': anchor, 'placeholder': cell, 'paths': paths})
                    if '@image' in cell:
                        images.append(anchor)
        text, image_payloads = renderer.update_values(copy.deepcopy(data['rd']), keymap, sheet['sheet_id'])
        if image_payloads:
            raise NativeSheetDataError('无图原生填充意外产生图像上传内容。')
        if len(text) != 1:
            raise NativeSheetDataError('原生填充返回区间结构变化。')
        target_range, target_values = next(iter(text.items()))
        sheets.append({'sheet_id': sheet['sheet_id'], 'sheet_name': sheet['sheet_name'],
            'range': target_range, 'values': target_values, 'image_cells_to_clear': images,
            'placeholders': placeholders, 'values_sha256': canonical_sha256(target_values),
            'number_format': '@', 'image_uploads': []})
    unrepresented = [{'theme': row['theme'], 'metric': row['metric'],
        'native_publish': row['native_publish'], 'status': row.get('status'),
        'diagnostic': row.get('diagnostic', ''), 'skip_reason': row.get('skip_reason', '')}
        for row in data['statistics'] if row['metric'] not in represented]
    result = {'schema': PLAN_SCHEMA, 'native_data_sha256': data['content_sha256'],
        'statistics_sha256': data['statistics_sha256'], 'rules_sha256': data['rules_sha256'],
        'native_template_reference': copy.deepcopy(data['native_template_reference']),
        'no_graph': True, 'sheets': sheets, 'complete_statistics_count': len(data['statistics']),
        'represented_metrics': sorted(represented - {'title', 'version'}),
        'unrepresented_metrics': unrepresented,
        'template_represents_every_rule': not unrepresented}
    result['plan_sha256'] = canonical_sha256(result)
    return result
