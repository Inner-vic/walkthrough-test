"""Checkpointed native-template Sheets delivery, without Excel or pictures.

Call under the worker's per-record exclusive lock. The optional fixed User
reader can only read the original template and check its copy permission. Relay
and final report operations use the dedicated Bot profile. The analyzer's portable
renderer supplies the values; this module never recomputes statistics. A lost
copy response is reconciled only from a saved receipt, never by copying again.
No message is sent here. A returned link has passed complete value/formula/blank,
image, cell-style and sheet-layout readback, followed by report governance.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import stat
from time import perf_counter
from urllib.parse import urlsplit
import uuid

import report_policy as policy
from request_model import InputError

VERSION = 'native-sheets-no-images-v1'
FIXED_USER_VERSION = 'native-sheets-fixed-user-reference-v2'
ROLES = ('baseline', 'replacement')
DATA_SCHEMA = 'm100-native-sheet-data-v1'
LAYOUT_INCLUDE = 'merges,row_heights,col_widths,hidden_rows,hidden_cols,groups,frozen'
WORKBOOK_FIELDS = ('sheet_name', 'index', 'row_count', 'column_count',
                   'merged_cells_count', 'is_hidden', 'chart_count',
                   'pivot_table_count', 'float_image_count')


def request_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def _token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _safe_path(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        path.relative_to(root)
    except ValueError:
        raise InputError('原生结果表文件不在本任务目录中。') from None
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InputError('原生结果表文件禁止符号链接或目录联接。')
        if current == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise InputError('原生结果表文件解析后超出本任务目录。')
    return path.resolve()


def _sheet_token(url):
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r'/sheets/([A-Za-z0-9_-]{1,128})/?', parsed.path)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port is not None or parsed.query or parsed.fragment
                or not host.endswith(('.feishu.cn', '.larksuite.com')) or not match):
            raise ValueError
        return match.group(1)
    except (ValueError, TypeError):
        raise InputError('原生结果表响应缺少有效飞书 Sheets 链接。') from None


def _data(payload):
    if not isinstance(payload, dict) or payload.get('ok') is not True or payload.get('identity') != 'bot':
        raise InputError('原生结果表接口未返回成功的固定 Bot 身份。')
    value = payload.get('data')
    if isinstance(value, dict) and 'code' in value:
        if value['code'] != 0:
            raise InputError('原生结果表接口返回业务错误。')
        value = value.get('data')
    if not isinstance(value, dict):
        raise InputError('原生结果表接口缺少可校验数据。')
    return value


def _call(settings, binding, args, timeout=150):
    return _data(policy.run_lark(settings, [*args, '--as', 'bot'], timeout=timeout,
                                 profile=binding['profile']))


def _cli_path(settings, path):
    root = Path(settings['workspace_root']).resolve()
    if Path(policy.WORKSPACE_ROOT).resolve() != root:
        raise InputError('原生结果表 CLI 工作区已变化。')
    return './' + _safe_path(path, root).relative_to(root).as_posix()


def _load(path, expected_sha256=None):
    if expected_sha256 and _digest(path) != expected_sha256:
        raise InputError('原生结果表断点文件指纹已变化。')
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise InputError('原生结果表断点文件不可读取。') from None
    if not isinstance(value, dict):
        raise InputError('原生结果表断点文件格式无效。')
    return value


def _save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    policy.atomic_json(Path(path), value)


@contextmanager
def _delivery_stage(entry, persist, stage):
    """Add bounded timing evidence without changing any publication checkpoint."""
    metric = {'stage': stage, 'started_at': datetime.now(timezone.utc).isoformat(),
              'outcome': 'running', 'api_call_count': 0}
    entry.setdefault('phase_timings', []).append(metric)
    persist()
    started = perf_counter()
    try:
        yield metric
    except BaseException:
        metric['outcome'] = 'failed'
        raise
    else:
        metric['outcome'] = 'ok'
    finally:
        metric['elapsed_seconds'] = round(perf_counter() - started, 6)
        persist()


def _fixed_checkpoint(entry, name, directory):
    descriptor = entry.get(name)
    if (not isinstance(descriptor, dict) or set(descriptor) != {'path', 'sha256'}
            or not isinstance(descriptor.get('path'), str) or not _hash(descriptor.get('sha256'))):
        raise InputError('fixed_user 发布缺少完整的参考快照或来源复制权限证据。')
    return _load(_safe_path(descriptor['path'], directory), descriptor['sha256'])


def _check_source_authorization(evidence, access):
    expected = {'schema': 'template-source-copy-check-v1', 'identity': 'user', 'auth_result': True,
        'source_token': access['source_token'], 'source_sheet_id': access['source_sheet_id'],
        'operator_open_id': access['operator_open_id'], 'app_id': access['app_id'],
        'profile': access['user_profile'], 'lark_home_sha256': access['lark_home_sha256'],
        'manifest_sha256': access['manifest_sha256']}
    if (not isinstance(evidence, dict) or set(evidence) != set(expected) | {'checked_at', 'response_sha256'}
            or any(evidence.get(key) != value for key, value in expected.items())
            or evidence.get('auth_result') is not True or not _hash(evidence.get('response_sha256'))
            or not isinstance(evidence.get('checked_at'), str)):
        raise InputError('固定User来源复制权限证据与本次身份、原模板或manifest不一致。')
    try:
        checked = datetime.fromisoformat(evidence['checked_at'].replace('Z', '+00:00'))
        if checked.tzinfo is None:
            raise ValueError
    except ValueError:
        raise InputError('固定User来源复制权限证据缺少有效时间。') from None


def _source_authorization(settings, access, directory, label):
    from template_reference_access import assert_source_copy_allowed
    evidence = assert_source_copy_allowed(settings, access)
    _check_source_authorization(evidence, access)
    path = directory / (label + '-' + uuid.uuid4().hex[:8] + '.json')
    _save(path, {'source_access': access, 'authorization': evidence})
    return {'path': str(path), 'sha256': _digest(path)}


def _fixed_reference(entry, access, reference, directory):
    proof = _fixed_checkpoint(entry, 'reference_copy_authorization', directory)
    if (set(proof) != {'source_access', 'authorization'} or proof['source_access'] != access):
        raise InputError('固定User参考快照的复制权限证据绑定已变化。')
    _check_source_authorization(proof['authorization'], access)
    original = _fixed_checkpoint(entry, 'reference_snapshot', directory)
    sheets = original.get('sheets')
    if (original.get('token') != reference or not isinstance(sheets, list) or len(sheets) != 7
            or any(not isinstance(x, dict) or not isinstance(x.get('info'), dict)
                   or not _token(x['info'].get('sheet_id')) for x in sheets)
            or len({x['info']['sheet_id'] for x in sheets}) != 7
            or access['source_sheet_id'] not in {x['info']['sheet_id'] for x in sheets}):
        raise InputError('固定User参考快照的原模板token或sheet_id不一致。')
    if (entry.get('token') or entry.get('copy_pending') or entry.get('plan')
            or entry.get('status') in ('copied', 'write_pending', 'values_verified', 'ready')):
        recheck = _fixed_checkpoint(entry, 'reference_copy_recheck', directory)
        if set(recheck) != {'source_access', 'authorization'} or recheck['source_access'] != access:
            raise InputError('固定User发布前复制权限证据绑定已变化。')
        _check_source_authorization(recheck['authorization'], access)
        before_copy = _fixed_checkpoint(entry, 'reference_before_copy', directory)
        if before_copy.get('token') != reference or not _equivalent_templates(original, before_copy):
            raise InputError('固定User发布前原reference快照与保存来源不一致。')
    return original


def _inputs(settings, record_path, record, result):
    import native_sheet_data as renderer
    workspace = Path(settings.get('workspace_root', '')).resolve()
    runtime = Path(settings.get('runtime_state_dir', '')).resolve()
    if Path(policy.WORKSPACE_ROOT).resolve() != workspace:
        raise InputError('原生结果表 CLI 工作目录与任务工作区不一致。')
    record_path = _safe_path(record_path, workspace)
    if record_path.parent != runtime or runtime == workspace:
        raise InputError('原生结果表发布记录不属于本 Skill runtime。')
    profile = str(settings.get('report_lark_profile') or '').strip()
    app_id = str(settings.get('expected_lark_app_id') or '').strip()
    home = str(settings.get('lark_home') or '').strip()
    if (not profile or profile == settings.get('lark_profile') or not app_id
            or not home or not Path(home).is_absolute()):
        raise InputError('原生结果表必须固定独立 Bot profile、App ID 和飞书身份目录。')
    fingerprint = record.get('request_sha256')
    if not _hash(fingerprint) or result.get('request_sha256') != fingerprint:
        raise InputError('原生结果表结果与任务请求指纹不一致。')
    if not record.get('vin') or result.get('vin') != record['vin']:
        raise InputError('原生结果表结果车辆与任务不一致。')
    if any(not isinstance(record.get(key), str) or not record[key]
           for key in ('job_key', 'sender_id', 'source_chat_id', 'source_message_id')):
        raise InputError('原生结果表任务缺少请求人、来源群或消息身份。')
    refs = result.get('native_sheet_data')
    if not isinstance(refs, dict) or set(refs) != set(ROLES):
        raise InputError('必须提供 Base 与 PCM更新两份完整原生结果数据。')
    windows = result.get('pipeline', {}).get('windows', {})
    artifacts, loaded = {}, {}
    for role in ROLES:
        item = refs[role];window = windows.get(role)
        metadata = record.get('request', {}).get(role + '_metadata')
        if (not isinstance(item, dict) or not isinstance(window, dict) or not isinstance(metadata, dict)
                or metadata.get('vin') != record['vin'] or window.get('request') != metadata
                or item.get('role') != role or item.get('request') != metadata
                or item.get('request_sha256') != request_digest(metadata)
                or item.get('schema') != DATA_SCHEMA):
            raise InputError(f'{role} 原生结果数据的角色、请求或格式不一致。')
        if (window.get('environment') not in ('testtwo', 'prod')
                or item.get('environment') != window.get('environment')
                or not _hash(window.get('rules_sha256'))
                or item.get('rules_sha256') != window.get('rules_sha256')
                or not _hash(item.get('sha256')) or type(item.get('bytes')) is not int
                or item['bytes'] <= 0 or not isinstance(item.get('path'), str)
                or not Path(item['path']).is_absolute()):
            raise InputError(f'{role} 原生结果数据的环境、规则或内容指纹无效。')
        role_root = _safe_path(record_path.with_suffix('.work') / role, workspace)
        path = _safe_path(item['path'], role_root)
        if not path.is_file() or path.stat().st_size != item['bytes'] or _digest(path) != item['sha256']:
            raise InputError(f'{role} 原生结果数据文件已变化或不存在。')
        data = renderer.load_native_sheet_data(path, expected_sha256=item['sha256'])
        if not _hash(data.get('content_sha256')) or not isinstance(data.get('statistics'), list):
            raise InputError('完整原生结果缺少有效内容指纹或完整规则清单。')
        meta = data.get('metadata', {})
        if (meta.get('request') != metadata or meta.get('environment') != item['environment']
                or meta.get('rules_sha256') != item['rules_sha256']
                or meta.get('role', role) != role):
            raise InputError(f'{role} 原生结果数据内部来源绑定不一致。')
        template = data.get('native_template_reference', {})
        if not _token(template.get('token')) or not _token(template.get('sheet_id')):
            raise InputError('缺少批准分析器实际使用的原生模板引用。')
        artifacts[role] = {key: item[key] for key in ('role', 'request', 'request_sha256',
            'environment', 'rules_sha256', 'path', 'sha256', 'bytes', 'schema')}
        artifacts[role]['native_template_reference'] = copy.deepcopy(template)
        loaded[role] = data
    options = settings.get('report_delivery') or {}
    if options.get('format', 'native_sheets') != 'native_sheets':
        raise InputError('本入口只发布原生 Sheets 完整明细。')
    if options.get('include_images', False) is not False:
        raise InputError('本版本原生结果表只交付完整统计，不生成或上传图片。')
    override = str(options.get('native_template_spreadsheet_token') or '').strip()
    if override and not _token(override):
        raise InputError('原生结果表模板 override 必须是有效 spreadsheet token。')
    parent = str(options.get('native_parent_token') or settings.get('report_parent_token') or 'my_space').strip()
    if parent != 'my_space' and not _token(parent):
        raise InputError('原生结果表目标目录 token 无效。')
    binding = {'version': VERSION, 'profile': profile, 'app_id': app_id,
        'lark_home': str(Path(home).resolve()), 'request_sha256': fingerprint,
        'template_override': override, 'parent_token': parent,
        'data_renderer_sha256': _digest(Path(renderer.__file__)),
        **{key: record[key] for key in ('job_key', 'sender_id', 'source_chat_id', 'source_message_id')}}
    from template_reference_access import build_source_binding
    access = build_source_binding(settings,
        {role: artifacts[role]['native_template_reference'] for role in ROLES}, override)
    if access is not None:
        binding.update(version=FIXED_USER_VERSION, source_access=access)
    return record_path, binding, artifacts, loaded


def _workbook(settings, binding, token, *, call=None):
    invoke = _call if call is None else call
    data = invoke(settings, binding, ['sheets', '+workbook-info', '--spreadsheet-token', token])
    sheets = data.get('sheets')
    if (data.get('token') != token or not isinstance(sheets, list) or len(sheets) != 7
            or len({s.get('sheet_id') for s in sheets}) != 7
            or len({s.get('sheet_name') for s in sheets}) != 7):
        raise InputError('原生模板必须完整返回七个唯一子表。')
    for sheet in sheets:
        if (not _token(sheet.get('sheet_id')) or not isinstance(sheet.get('sheet_name'), str)
                or type(sheet.get('row_count')) is not int or not 1 <= sheet['row_count'] <= 10000
                or type(sheet.get('column_count')) is not int or sheet['column_count'] < 15
                or sheet.get('float_image_count') != 0):
            raise InputError('原生模板子表结构无效或包含未获支持的浮动图片。')
    return data


def _cell_value(cell):
    if cell.get('formula'):
        return cell['formula']
    if 'value' in cell:
        return cell['value']
    text = cell.get('rich_text', [])
    if text and all(x.get('type') != 'embed-image' for x in text):
        return ''.join(str(x.get('text', '')) for x in text)
    return None


def _has_image(cell):
    return any(x.get('type') == 'embed-image' for x in cell.get('rich_text', []))


def _style(cell):
    return {key: value for key, value in cell.items() if key in ('style', 'cell_styles', 'border_styles')}


def _read_cells(settings, binding, token, sheet, directory, label, *, call=None):
    area = f"B1:O{sheet['row_count']}"
    path = directory / (label + '-' + sheet['sheet_id'] + '-' + uuid.uuid4().hex[:8] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    invoke = _call if call is None else call
    receipt = invoke(settings, binding, ['sheets', '+cells-get', '--spreadsheet-token', token,
        '--sheet-id', sheet['sheet_id'], '--range', area, '--include', 'value,formula,style',
        '--output-path', _cli_path(settings, path)])
    if receipt.get('complete') is not True:
        raise InputError('原生结果表读取未完成，禁止基于截断数据填表或验收。')
    value = _load(path)
    ranges = value.get('ranges')
    if value.get('has_more') is not False or not isinstance(ranges, list) or len(ranges) != 1:
        raise InputError('原生结果表读取缺少完整范围。')
    result = ranges[0]
    if (result.get('truncated') or result.get('actual_range') != area
            or result.get('row_indices') != list(range(1, sheet['row_count'] + 1))
            or result.get('col_indices') != list('BCDEFGHIJKLMNO')
            or not isinstance(result.get('cells'), list) or len(result['cells']) != sheet['row_count']
            or any(not isinstance(row, list) or len(row) != 14 or any(not isinstance(cell, dict) for cell in row) for row in result['cells'])):
        raise InputError('原生结果表读取的首尾行列、范围或数据维度不一致。')
    return result


def _layout(settings, binding, token, sheet, *, call=None):
    invoke = _call if call is None else call
    value = invoke(settings, binding, ['sheets', '+sheet-info', '--spreadsheet-token', token,
        '--sheet-id', sheet['sheet_id'], '--include', LAYOUT_INCLUDE])
    def normalized(node):
        if isinstance(node, dict):
            return {k: normalized(v) for k, v in node.items() if k not in
                    ('token', 'spreadsheet_token', 'sheet_id', 'revision', 'modified_time', 'url')}
        if isinstance(node, list):return [normalized(x) for x in node]
        if isinstance(node, str):return node.replace(sheet['sheet_id'] + '!', '@sheet!')
        return node
    return normalized(value)


def _revision(settings, binding, token, *, call=None):
    invoke = _call if call is None else call
    try:
        value = invoke(settings, binding, ['sheets', '+revision-get', '--spreadsheet-token', token])
    except InputError:
        return None  # Unsupported revision reads retain the full-read path.
    revision = value.get('revision') if isinstance(value, dict) else None
    return revision if type(revision) is int and 0 <= revision <= 2**53 else None


def _stable_revision(snapshot, token):
    guard = snapshot.get('revision_guard') if isinstance(snapshot, dict) else None
    if (snapshot.get('token') != token or not isinstance(guard, dict)
            or set(guard) != {'schema', 'token', 'before', 'after'}
            or guard['schema'] != 'native-snapshot-revision-v1' or guard['token'] != token
            or type(guard['before']) is not int or type(guard['after']) is not int
            or not 0 <= guard['before'] == guard['after'] <= 2**53):
        return None
    return guard['after']


def _snapshot(settings, binding, token, directory, label, *, call=None, guard_revision=False, prior=None):
    if guard_revision and prior is not None and _stable_revision(prior, token) is not None:
        current = _revision(settings, binding, token, call=call)
        if current is not None and current == _stable_revision(prior, token):
            return copy.deepcopy(prior)
    before = _revision(settings, binding, token, call=call) if guard_revision else None
    info = _workbook(settings, binding, token, call=call)
    result = {'token': token, 'sheets': []}
    for sheet in info['sheets']:
        result['sheets'].append({'info': sheet, 'layout': _layout(settings, binding, token, sheet, call=call),
            'area': _read_cells(settings, binding, token, sheet, directory, label, call=call)})
    if before is not None:
        after = _revision(settings, binding, token, call=call)
        if after == before:
            result['revision_guard'] = {'schema': 'native-snapshot-revision-v1',
                'token': token, 'before': before, 'after': after}
    return result


def _equivalent_templates(left, right):
    if len(left['sheets']) != len(right['sheets']):return False
    for a, b in zip(left['sheets'], right['sheets']):
        if ({k: a['info'].get(k) for k in WORKBOOK_FIELDS} != {k: b['info'].get(k) for k in WORKBOOK_FIELDS}
                or a['layout'] != b['layout']):return False
        for row_a, row_b in zip(a['area']['cells'], b['area']['cells']):
            if len(row_a) != len(row_b):return False
            for ca, cb in zip(row_a, row_b):
                if _cell_value(ca) != _cell_value(cb) or _style(ca) != _style(cb):return False
    return True


def _copy_result(data, template, title):
    if (data.get('copied') is not True or data.get('file_type') != 'sheet'
            or data.get('source_type') != 'sheet' or data.get('source_file_token') != template
            or data.get('name') != title or not _token(data.get('file_token'))
            or data.get('file_token') == template
            or _sheet_token(data.get('url')) != data['file_token']):
        raise InputError('模板复制回执不完整；保留未确认状态，禁止自动再次复制。')
    if isinstance(data.get('permission_grant'), dict) and data['permission_grant'].get('status') == 'granted':
        raise InputError('模板复制发生了固定 Bot-only 身份之外的隐式用户授权，需先核查权限。')
    return {'token': data['file_token'], 'url': data['url']}


def _template_inputs(snapshot):
    result = []
    for sheet in snapshot['sheets']:
        info, area = sheet['info'], sheet['area']
        result.append({**{key: info[key] for key in ('sheet_id', 'sheet_name', 'row_count', 'column_count')},
            'keymap': {'valueRanges': [{'range': info['sheet_id'] + '!' + area['actual_range'],
                'values': [[_cell_value(cell) for cell in row] for row in area['cells']]}]}})
    return result


def _expected_formula(value):
    if isinstance(value, dict):
        if value.get('type') == 'formula' and isinstance(value.get('text'), str):return value['text']
        raise InputError('原生模板渲染结果包含未知单元格对象。')
    return None


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    text = str(value)
    if not re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?', text):
        return None
    try:
        number = Decimal(text)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _matches(cell, expected, *, numeric_statistic=False):
    if _has_image(cell):return False
    formula = _expected_formula(expected)
    if formula is not None:return cell.get('formula') == formula
    if cell.get('formula'):return False
    actual = _cell_value(cell)
    if expected is None:return actual in (None, '')
    if isinstance(expected, bool):return isinstance(actual, bool) and actual == expected
    if numeric_statistic or isinstance(expected, (int, float)) and not isinstance(expected, bool):
        # The original renderer formats numeric statistics as decimal strings.
        # Sheets may store them as JSON numbers without changing their value.
        # Decimal equality is exact; labels, diagnostics and formulas never use
        # this branch, and no float tolerance may conceal a changed statistic.
        if numeric_statistic:
            left, right = _decimal(expected), _decimal(actual)
            return left is not None and right is not None and left == right
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return type(actual) is type(expected) and actual == expected
        left, right = _decimal(expected), _decimal(actual)
        return left is not None and right is not None and left == right
    return type(actual) is type(expected) and actual == expected


def _numeric_stat_cells(data, plan):
    result = set()
    for sheet in plan['sheets']:
        for item in sheet.get('placeholders', []):
            paths = item.get('paths', [])
            if (not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], list)
                    or len(paths[0]) != 2 or paths[0][0] in ('title', 'version')
                    or item.get('placeholder') != '&' + '@'.join(paths[0])):
                continue
            path = paths[0]
            if not re.fullmatch(r'(?:mean|max|min|P90|P99|HGR)(?:_[A-Za-z0-9_]+)?', path[-1]):continue
            rules = [values[path[0]] for values in data.get('rules', {}).values()
                     if isinstance(values, dict) and path[0] in values]
            if not rules or any(not isinstance(rule, dict) or not rule.get('ps_type')
                                or rule['ps_type'] == 'ps_string' for rule in rules):continue
            value = data.get('rd', {})
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    value = None;break
                value = value[key]
            if (isinstance(value, bool) or not isinstance(value, (str, int, float))
                    or not re.fullmatch(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?', str(value))
                    or _decimal(value) is None):
                continue
            match = re.fullmatch(re.escape(sheet['sheet_id']) + r'!([B-O])([1-9]\d*)(?::\1\2)?', str(item.get('range', '')))
            if not match:continue
            ri, ci = int(match.group(2)) - 1, ord(match.group(1)) - ord('B')
            if 0 <= ri < len(sheet['values']) and _decimal(sheet['values'][ri][ci]) is not None:
                result.add((sheet['sheet_id'], ri, ci))
    return result


def _validate_plan(plan, snapshot, data_sha, statistics_count):
    if not isinstance(plan, dict) or not isinstance(plan.get('sheets'), list) or len(plan['sheets']) != 7:
        raise InputError('原生模板渲染没有返回完整七页。')
    if plan.get('native_data_sha256') != data_sha:
        raise InputError('原生模板渲染计划未绑定完整原生数据指纹。')
    if (type(plan.get('complete_statistics_count')) is not int
            or plan['complete_statistics_count'] != statistics_count
            or not isinstance(plan.get('unrepresented_metrics'), list)
            or len(plan['unrepresented_metrics']) > statistics_count):
        raise InputError('原生模板计划丢失完整统计数量或未映射规则审计。')
    for planned, original in zip(plan['sheets'], snapshot['sheets']):
        info = original['info']
        if (planned.get('sheet_id') != info['sheet_id'] or planned.get('sheet_name') != info['sheet_name']
                or planned.get('range', '').split('!')[-1] != original['area']['actual_range']
                or not isinstance(planned.get('values'), list) or len(planned['values']) != info['row_count']
                or any(not isinstance(row, list) or len(row) != 14 for row in planned['values'])):
            raise InputError('原生模板渲染发生页、范围或行列错位。')
        for row in planned['values']:
            for value in row:
                if isinstance(value, dict):_expected_formula(value)
                elif value is not None and (not isinstance(value, (str, int, float, bool))
                        or isinstance(value, float) and not math.isfinite(value)):
                    raise InputError('原生模板渲染包含无效数值。')
                if isinstance(value, str) and value.lstrip().startswith('&'):
                    raise InputError('原生模板中仍有未替换占位符，不能交付。')


def _verify(settings, binding, token, snapshot, plan, directory, *, allow_original=False, numeric_cells=frozenset(), call=None):
    revision = _stable_revision(snapshot, token)
    reused = (allow_original and settings.get('native_sheet_revision_guard') is True
              and revision is not None and _revision(settings, binding, token, call=call) == revision)
    current = ({'sheets': [sheet['info'] for sheet in snapshot['sheets']]} if reused
               else _workbook(settings, binding, token, call=call))
    if [{k:s.get(k) for k in ('sheet_id', *WORKBOOK_FIELDS)} for s in current['sheets']] != [
            {k:s['info'].get(k) for k in ('sheet_id', *WORKBOOK_FIELDS)} for s in snapshot['sheets']]:
        raise InputError('原生结果表布局、顺序或子表清单发生变化。')
    mismatches = [];verified_cells = 0;cleared_image_cells = 0
    for planned, original in zip(plan['sheets'], snapshot['sheets']):
        sheet = original['info']
        if not reused and _layout(settings, binding, token, sheet, call=call) != original['layout']:
            raise InputError('原生结果表合并、行高列宽、隐藏或冻结结构发生变化。')
        area = (original['area'] if reused else
                _read_cells(settings, binding, token, sheet, directory, 'readback', call=call))
        for ri, row in enumerate(area['cells']):
            for ci, cell in enumerate(row):
                source = original['area']['cells'][ri][ci];expected = planned['values'][ri][ci]
                if _style(cell) != _style(source):
                    raise InputError('原生结果表单元格格式发生变化。')
                verified_cells += 1
                if _matches(cell, expected, numeric_statistic=(sheet['sheet_id'], ri, ci) in numeric_cells):continue
                if allow_original:
                    keys = ('value', 'formula', 'rich_text')
                    if {k:cell.get(k) for k in keys} != {k:source.get(k) for k in keys}:
                        raise InputError('原生结果表出现任务原模板和计划之外的内容，拒绝覆盖人工改动。')
                mismatches.append({'sheet_id':sheet['sheet_id'], 'row':ri + 1, 'column':ci + 2})
        cleared_image_cells += len(planned.get('image_cells_to_clear', []))
    return {'complete': not mismatches, 'mismatches': mismatches, 'verified_cells': verified_cells,
        'images_uploaded': 0, 'image_cells_cleared': cleared_image_cells,
        'all_values_formulas_blanks_styles_and_layout_verified': not mismatches,
        'revision_snapshot_reused': bool(reused)}


def _writes(snapshot, plan):
    result = []
    for planned, original in zip(plan['sheets'], snapshot['sheets']):
        matrix = []
        for ri, row in enumerate(planned['values']):
            cells = []
            for ci, value in enumerate(row):
                source = original['area']['cells'][ri][ci]
                if _matches(source, value):cells.append({})
                elif (formula := _expected_formula(value)) is not None:cells.append({'formula':formula})
                else:cells.append({'value':'' if value is None else value})
            matrix.append(cells)
        result.append({'sheet_id':planned['sheet_id'], 'range':planned['range'].split('!')[-1], 'cells':matrix})
    return result


def _govern(settings, record_path, record, role, entry, persist):
    permissions = entry.setdefault('permissions', {})
    url = entry['url']
    for name, function, extra in (
        ('collaborator_management', policy.restrict_collaborator_management, ()),
        ('source_chat_view', policy.grant_source_chat_view, (record['source_chat_id'], record['sender_id']))):
        if not permissions.get(name):
            audit = function(settings, url, *extra)
            if name == "source_chat_view":
                permissions["source_chat_access"] = audit
            permissions[name] = True;persist()
    if not permissions.get('owner_transfer'):
        if permissions.get('owner_transfer_pending'):
            raise InputError(f'{role} 原生表所有权交接回执未知，需先核查，不会自动重复转移。')
        permissions['owner_transfer_pending'] = True;persist()
        policy.transfer_report_owner(settings, url, record['sender_id'])
        permissions['owner_transfer'] = True;permissions.pop('owner_transfer_pending', None);persist()
    policy.configure_report_secure_label(settings, record_path, record, url, permissions, persist_callback=persist)


def ensure_native_sheets(settings, record_path, record, result, report_url=None):
    """Return validated Base/PCM Sheets manifests and persist independent checkpoints.

    Requires native_sheet_data manifests for both roles in ``result``. Source
    data lives under this record's role work directories and remains available
    for delivery-only retries. The main Docx may be created after this returns.
    Copy outcomes without a saved valid response require manual reconciliation;
    a known copy is never recreated. Pending value writes are first read back
    and may only replay the same plan over original-or-already-planned cells.
    """
    import native_sheet_data as renderer
    record_path, binding, artifacts, loaded = _inputs(settings, record_path, record, result)
    access = binding.get('source_access')
    if report_url is not None and record.get('report_url') != report_url:
        raise InputError('主报告链接与当前任务记录不一致。')
    policy.ensure_report_bot_authorization(settings)
    state = record.get('native_sheets')
    identity = {'binding':binding, 'artifacts':artifacts}
    if state is None:
        state = {'version':binding['version'], 'identity':identity, 'roles':{}}
        record['native_sheets'] = state
    elif state.get('version') != binding['version'] or state.get('identity') != identity:
        raise InputError('原生结果表断点与角色、来源、模板或发布身份不一致。')
    if not isinstance(state.get('roles'), dict) or set(state['roles']) - set(ROLES):
        raise InputError('原生结果表断点角色清单无效。')
    existing_tokens = [value.get('token') for value in state['roles'].values() if isinstance(value, dict) and value.get('token')]
    if len(existing_tokens) != len(set(existing_tokens)):
        raise InputError('Base 与 PCM更新断点共用了同一份结果表。')
    def persist():policy.atomic_json(record_path, record)
    persist()
    output = {};work = _safe_path(record_path.with_suffix('.native-sheets'), Path(settings['workspace_root']))
    for role in ROLES:
        data, artifact = loaded[role], artifacts[role]
        entry = state['roles'].setdefault(role, {'status':'prepared'})
        directory = _safe_path(work / role, work);directory.mkdir(parents=True, exist_ok=True)
        def tracked_call(metric):
            def invoke(settings, binding, args, timeout=150):
                metric['api_call_count'] += 1
                return _call(settings, binding, args, timeout=timeout)
            return invoke
        def snapshot(token, label, *, fixed_user=False, prior=None):
            with _delivery_stage(entry, persist, 'snapshot_' + label) as metric:
                if not fixed_user:
                    guard = settings.get('native_sheet_revision_guard') is True and label in (
                        'source', 'override', 'copy', 'source-before-copy')
                    return _snapshot(settings, binding, token, directory, label,
                        call=tracked_call(metric), guard_revision=guard, prior=prior)
                from template_reference_access import user_reference_read_session
                scope = {'job_key': record['job_key'], 'role': role, 'purpose': label}
                session = user_reference_read_session(settings, access, scope=scope, directory=directory)
                def invoke(settings, unused_binding, args, timeout=150):
                    return session.call(settings, access, args, scope=scope, timeout=timeout)
                try:
                    with session:
                        return _snapshot(settings, binding, token, directory, label, call=invoke)
                finally:
                    metric['reference_read_session'] = copy.deepcopy(session.audit)
                    metric['api_call_count'] = session.audit.get('api_call_count', 0)
        def verify(stage, **options):
            with _delivery_stage(entry, persist, stage) as metric:
                return _verify(settings, binding, entry['token'], snapshot_data, plan, directory,
                    call=tracked_call(metric), **options)
        reference = artifact['native_template_reference']['token']
        template = binding['template_override'] or reference
        if access is not None and 'source_snapshot' not in entry and (
                entry.get('token') or entry.get('copy_pending') or entry.get('plan')
                or entry.get('status') in ('copied', 'write_pending', 'values_verified', 'ready')):
            raise InputError('已有fixed_user发布资产缺少来源快照；必须恢复原证据，不能读取新来源替代。')
        if 'source_snapshot' not in entry:
            if access is None:
                original = snapshot(reference, 'source')
            else:
                if 'reference_snapshot' not in entry:
                    entry['reference_copy_authorization'] = _source_authorization(
                        settings, access, directory, 'reference-copy-authorization')
                    persist()
                    original = snapshot(reference, 'reference', fixed_user=True)
                    if access['source_sheet_id'] not in {x['info']['sheet_id'] for x in original['sheets']}:
                        raise InputError('批准分析器的原reference sheet_id不在固定User完整快照中。')
                    path = directory / 'reference-snapshot.json';_save(path, original)
                    entry['reference_snapshot'] = {'path':str(path), 'sha256':_digest(path)};persist()
                original = _fixed_reference(entry, access, reference, directory)
            if template != reference:
                override = snapshot(template, 'override')
                if not _equivalent_templates(original, override):
                    raise InputError('配置模板与分析器原生模板的布局、格式或占位内容不兼容。')
                original = override
            path = directory / 'source-snapshot.json';_save(path, original)
            entry['source_snapshot'] = {'path':str(path), 'sha256':_digest(path)};persist()
        source = _load(_safe_path(entry['source_snapshot']['path'], directory), entry['source_snapshot']['sha256'])
        fixed_reference = _fixed_reference(entry, access, reference, directory) if access is not None else None
        if fixed_reference is not None and not _equivalent_templates(fixed_reference, source):
            raise InputError('保存的固定User原reference与Bot中继快照不等价。')
        if source['token'] != template:
            raise InputError('原生结果表模板断点 token 不一致。')
        if template == reference and artifact['native_template_reference']['sheet_id'] not in {x['info']['sheet_id'] for x in source['sheets']}:
            raise InputError('批准分析器的源模板 sheet_id 不在实际模板中。')
        preview = renderer.build_native_sheet_plan(data, _template_inputs(source))
        _validate_plan(preview, source, data['content_sha256'], len(data['statistics']))
        label = 'Base' if role == 'baseline' else 'PCM更新'
        title = f"走查测试_{record['vin']}_{label}_{artifact['request_sha256'][:12]}"
        if not entry.get('token'):
            if entry.get('copy_pending'):
                receipt = entry.get('copy_receipt')
                if not receipt:
                    raise InputError(f'{role} 模板复制结果未知；请先核查已有副本，禁止自动重复创建。')
                copied = _copy_result(_load(_safe_path(receipt['path'], directory), receipt['sha256']), template, title)
            else:
                if access is not None:
                    entry['reference_copy_recheck'] = _source_authorization(
                        settings, access, directory, 'reference-copy-before-publish')
                    persist()
                    current_reference = snapshot(reference, 'reference-before-copy', fixed_user=True)
                    if not _equivalent_templates(fixed_reference, current_reference):
                        raise InputError('固定User原reference在准备后已更新，禁止从旧中继复制报告。')
                    path = directory / ('reference-before-copy-' + uuid.uuid4().hex[:8] + '.json')
                    _save(path, current_reference)
                    entry['reference_before_copy'] = {'path':str(path), 'sha256':_digest(path)};persist()
                current = snapshot(template, 'source-before-copy', prior=source)
                if not _equivalent_templates(source, current):
                    raise InputError('原生模板在准备后已更新，禁止复制与已审计划不一致的模板。')
                entry['copy_pending'] = True;persist()
                with _delivery_stage(entry, persist, 'copy_result_sheet') as metric:
                    response = tracked_call(metric)(settings, binding, ['drive', '+copy', '--token', template, '--type', 'sheet',
                        '--name', title, '--folder-token', binding['parent_token']])
                path = directory / 'copy-receipt.json';_save(path, response)
                entry['copy_receipt'] = {'path':str(path), 'sha256':_digest(path)};persist()
                copied = _copy_result(response, template, title)
            entry.update(copied);entry.update(status='copied', template_token=template, title=title)
            entry.pop('copy_pending', None);persist()
        if any(other != role and value.get('token') == entry.get('token') for other, value in state['roles'].items()):
            raise InputError('Base 与 PCM更新不能共用同一份结果表。')
        if _sheet_token(entry.get('url')) != entry.get('token') or entry.get('template_token') != template:
            raise InputError('原生结果表目标 token、链接或模板绑定不一致。')
        if entry['token'] in (template, reference):
            raise InputError('原生模板本身不能作为本任务的发布目标。')
        if 'copied_snapshot' not in entry:
            copied = snapshot(entry['token'], 'copy')
            if not _equivalent_templates(source, copied):
                raise InputError('模板副本没有保留完整原生布局、格式和占位内容。')
            path = directory / 'copied-snapshot.json';_save(path, copied)
            entry['copied_snapshot'] = {'path':str(path), 'sha256':_digest(path)};persist()
        snapshot_data = _load(_safe_path(entry['copied_snapshot']['path'], directory), entry['copied_snapshot']['sha256'])
        if snapshot_data['token'] != entry['token']:
            raise InputError('模板副本断点指向了其他结果表。')
        if 'plan' not in entry:
            plan = renderer.build_native_sheet_plan(data, _template_inputs(snapshot_data))
            _validate_plan(plan, snapshot_data, data['content_sha256'], len(data['statistics']))
            path = directory / 'native-plan.json';_save(path, plan)
            entry['plan'] = {'path':str(path), 'sha256':_digest(path)};persist()
        plan = _load(_safe_path(entry['plan']['path'], directory), entry['plan']['sha256'])
        _validate_plan(plan, snapshot_data, data['content_sha256'], len(data['statistics']))
        numeric_cells = _numeric_stat_cells(data, plan)
        ready = entry.get('status') in ('ready', 'values_verified')
        verification = verify('verify_before_write', allow_original=not ready, numeric_cells=numeric_cells)
        if not verification['complete']:
            if ready:
                raise InputError('已交付原生结果表发生内容变化；保留现状，禁止自动覆盖。')
            payload = directory / 'native-writes.json';_save(payload, _writes(snapshot_data, plan))
            entry.update(status='write_pending', write_pending=True, writes_sha256=_digest(payload));persist()
            with _delivery_stage(entry, persist, 'write_result_cells') as metric:
                response = tracked_call(metric)(settings, binding, ['sheets', '+cells-set', '--spreadsheet-token', entry['token'],
                    '--writes', '@' + _cli_path(settings, payload)])
            if response.get('formula_errors'):
                raise InputError('原生结果表公式写入报告错误，尚未验收。')
            verification = verify('verify_after_write', numeric_cells=numeric_cells)
            if not verification['complete']:
                raise InputError('原生结果表完整读回与计划不一致，尚未完成交付。')
        verification.update(complete_statistics_count=plan['complete_statistics_count'],
            numeric_statistic_cells=len(numeric_cells),
            numeric_readback_policy='只有非ps_string规则的数值统计字段、单一纯占位与规范十进制原值允许字符串与number按Decimal严格等值；标签/诊断/拼接/公式不转换，不使用浮点容差。',
            template_mapped_rule_count=plan['complete_statistics_count']-len(plan['unrepresented_metrics']),
            unrepresented_metrics=copy.deepcopy(plan['unrepresented_metrics']),
            template_represents_every_rule=not plan['unrepresented_metrics'],
            complete_local_statistics_retained=True,
            publication_semantics='完整规则已执行；按原生七子表的现有占位映射发布，未映射规则保留于本地完整结果和发布审计。')
        path = directory / 'readback-validation.json';_save(path, verification)
        entry.update(status='values_verified', validation_sha256=_digest(path));entry.pop('write_pending', None);persist()
        with _delivery_stage(entry, persist, 'govern_result_sheet') as metric:
            # Governance helpers contain their own calls/checkpoints. Do not
            # misreport an unknown number of remote calls as zero.
            metric['api_call_count'] = None
            _govern(settings, record_path, record, role, entry, persist)
        entry['status'] = 'ready';persist()
        output[role] = {**{key:artifact[key] for key in ('role','request','request_sha256','environment','rules_sha256','schema')},
            'url':entry['url'], 'token':entry['token'], 'template_token':template,
            'native_data_sha256':data['content_sha256'], 'native_data_file_sha256':artifact['sha256'],
            'plan_sha256':entry['plan']['sha256'], 'renderer_plan_sha256':plan.get('plan_sha256'),
            'validation':{'status':'ok', **verification}, 'permissions':copy.deepcopy(entry['permissions']),
            'include_images':False}
    if output['baseline']['token'] == output['replacement']['token']:
        raise InputError('Base 与 PCM更新不能共用同一份结果表。')
    return output
