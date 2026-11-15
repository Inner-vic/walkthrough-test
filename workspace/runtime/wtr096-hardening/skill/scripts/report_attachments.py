"""Publish complete XLSX artifacts as media inside the existing report Docx.

Call under the worker's per-record exclusive lock. This module never runs an
analyzer, creates an independent Drive file, changes ACLs, or sends a message.
The bot needs Docx read/write plus document-media upload/download scopes (or
the documented broader alternatives). API denial is propagated, never repaired
by granting scopes/permissions or changing identity. Main-report governance is
still the worker's responsibility; document view does not imply download access.

API references: docx-v1/document-block/create, /batch_update, /get and
drive-v1/media/upload_all, /download. Create/bind client_token values are saved
before mutation. Unknown outcomes are reconciled by reading, not blind replay.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
import uuid

import report_policy as policy
from request_model import InputError

VERSION = 'docx-xlsx-attachments-v1'
ROLES = ('baseline', 'replacement')
STATES = {'prepared', 'create_pending', 'created', 'upload_pending',
          'uploaded', 'bind_pending', 'ready'}
REQUIRED_SCOPE_GROUPS = (
    frozenset({'docx:document'}),
    frozenset({'docs:document.media:upload', 'docs:doc', 'drive:drive'}),
    frozenset({'docs:document.media:download', 'docs:doc', 'docs:doc:readonly',
               'drive:drive', 'drive:drive:readonly'}),
)


def request_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def _token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def artifact_filename(role, request_sha256, sha256):
    """Return the exact card name shared by report copy and uploaded media."""
    if role not in ROLES or not _hash(request_sha256) or not _hash(sha256):
        raise InputError('完整 Excel 附件缺少有效角色、请求或内容指纹。')
    return ('Base' if role == 'baseline' else 'PCM_update') + '_' + request_sha256[:12] + '_' + sha256[:12] + '.xlsx'


def _safe_path(path, root):
    """Reject symlinks/junctions as well as resolved paths outside the root."""
    path, root = Path(path).absolute(), Path(root).absolute()
    try:
        path.relative_to(root)
    except ValueError:
        raise InputError('附件路径不在本任务允许的目录中。') from None
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InputError('附件路径禁止符号链接或目录联接。')
        if current == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise InputError('附件解析后的路径超出本任务目录。')
    return path.resolve()


def _document_id(url):
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r'/docx/([A-Za-z0-9_-]{1,128})/?', parsed.path)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port is not None or parsed.query or parsed.fragment
                or not host.endswith('.feishu.cn') or not match):
            raise ValueError
        return match.group(1)
    except (TypeError, ValueError):
        raise InputError('完整 Excel 附件只允许写入已确认的飞书 Docx 报告。') from None


def _inputs(settings, record_path, record, result, report_url):
    workspace = Path(settings.get('workspace_root', '')).resolve()
    runtime = Path(settings.get('runtime_state_dir', '')).resolve()
    if Path(policy.WORKSPACE_ROOT).resolve() != workspace:
        raise InputError('附件发布 CLI 工作目录与任务工作区不一致。')
    record_path = _safe_path(record_path, workspace)
    if record_path.parent != runtime or runtime == workspace:
        raise InputError('附件发布记录不属于走查测试 runtime。')
    profile = str(settings.get('report_lark_profile') or '').strip()
    app_id = str(settings.get('expected_lark_app_id') or '').strip()
    home = str(settings.get('lark_home') or '').strip()
    if (not profile or profile == settings.get('lark_profile') or not app_id or not home
            or not Path(home).is_absolute()):
        raise InputError('附件发布必须固定独立 Bot profile、App ID 和飞书身份目录。')
    document_id = _document_id(report_url)
    if record.get('report_url') != report_url:
        raise InputError('附件目标报告与任务记录不一致。')
    fingerprint = record.get('request_sha256')
    if not _hash(fingerprint) or result.get('request_sha256') != fingerprint:
        raise InputError('附件结果与任务请求指纹不一致。')
    if not record.get('vin') or result.get('vin') != record['vin']:
        raise InputError('附件结果车辆与任务不一致。')
    if any(not isinstance(record.get(key), str) or not record[key]
           for key in ('job_key', 'sender_id', 'source_chat_id', 'source_message_id')):
        raise InputError('附件任务缺少来源群、请求人或消息身份。')
    manifests = result.get('full_workbooks')
    if not isinstance(manifests, dict) or set(manifests) != set(ROLES):
        raise InputError('报告必须包含 Base 和 PCM 更新两份完整 Excel 清单。')
    windows = result.get('pipeline', {}).get('windows', {})
    request = record.get('request', {})
    artifacts = {}
    for role in ROLES:
        item, window = manifests[role], windows.get(role)
        metadata = request.get(role + '_metadata')
        if (not isinstance(item, dict) or not isinstance(window, dict)
                or not isinstance(metadata, dict) or metadata.get('vin') != record['vin']
                or window.get('request') != metadata or item.get('request') != metadata
                or item.get('role') != role
                or item.get('request_sha256') != request_digest(metadata)):
            raise InputError(f'{role} 完整 Excel 的角色或时段指纹不一致。')
        environment = window.get('environment')
        rules = window.get('rules_sha256')
        if (environment not in ('testtwo', 'prod') or item.get('environment') != environment
                or not _hash(rules) or item.get('rules_sha256') != rules):
            raise InputError(f'{role} 完整 Excel 的环境或规则指纹不一致。')
        if (not isinstance(item.get('validation'), dict)
                or item['validation'].get('status') != 'ok'
                or not _hash(item.get('sha256')) or type(item.get('bytes')) is not int
                or item['bytes'] <= 0 or not isinstance(item.get('path'), str)
                or not Path(item['path']).is_absolute()):
            raise InputError(f'{role} 完整 Excel 尚未通过校验或缺少有效文件清单。')
        role_root = _safe_path(record_path.with_suffix('.work') / role, workspace)
        path = _safe_path(item['path'], role_root)
        if path.suffix.lower() != '.xlsx' or path == role_root:
            raise InputError('附件必须是本任务角色目录中的 XLSX。')
        name = artifact_filename(role, item['request_sha256'], item['sha256'])
        artifacts[role] = {key: item[key] for key in ('role', 'request', 'environment',
            'request_sha256', 'rules_sha256', 'sha256', 'bytes')}
        artifacts[role].update(path=str(path), file_name=name)
    binding = {'document_id': document_id, 'report_url': report_url, 'profile': profile,
               'app_id': app_id, 'lark_home': str(Path(home).resolve()),
               'request_sha256': fingerprint,
               **{key: record[key] for key in ('job_key', 'sender_id', 'source_chat_id', 'source_message_id')}}
    return record_path, binding, artifacts


def _data(payload):
    if not isinstance(payload, dict) or payload.get('ok') is not True or payload.get('identity') != 'bot':
        raise InputError('附件接口没有返回成功的 Bot 身份结果。')
    data = payload.get('data')
    # Raw API may retain the upstream envelope; reject nonzero business codes.
    if isinstance(data, dict) and 'code' in data:
        if data['code'] != 0:
            raise InputError('附件接口返回非零业务错误。')
        data = data.get('data')
    if not isinstance(data, dict):
        raise InputError('附件接口缺少可校验的数据。')
    return data


def _call(settings, binding, args, timeout=120):
    return _data(policy.run_lark(settings, [*args, '--as', 'bot'], timeout=timeout,
                                 profile=binding['profile']))


def _cli_path(settings, path):
    workspace = Path(settings['workspace_root']).resolve()
    if Path(policy.WORKSPACE_ROOT).resolve() != workspace:
        raise InputError('附件发布 CLI 工作目录已变化。')
    path = _safe_path(path, workspace)
    return './' + path.relative_to(workspace).as_posix()


def _api(settings, binding, method, suffix, *, body=None, client_token=None):
    args = ['api', method, '/open-apis/docx/v1/documents/' + binding['document_id'] + suffix]
    params = {'document_revision_id': -1}
    if client_token:
        params['client_token'] = client_token
    args += ['--params', json.dumps(params)]
    if body is not None:
        args += ['--data', json.dumps(body, ensure_ascii=False)]
    return _call(settings, binding, args)


def _get_block(settings, binding, block_id):
    if not _token(block_id):
        raise InputError('附件块 ID 无效。')
    block = _api(settings, binding, 'GET', '/blocks/' + block_id).get('block')
    if not isinstance(block, dict) or block.get('block_id') != block_id:
        raise InputError('附件块读取结果不一致。')
    return block


def _all_blocks(settings, binding):
    blocks, seen, cursor = [], set(), None
    for _ in range(100):
        params = {'document_revision_id': -1, 'page_size': 500}
        if cursor:
            params['page_token'] = cursor
        data = _call(settings, binding, ['api', 'GET', '/open-apis/docx/v1/documents/'
                     + binding['document_id'] + '/blocks', '--params', json.dumps(params)])
        if not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
            raise InputError('附件认领所需的完整文档分页响应无效。')
        for block in data['items']:
            if not isinstance(block, dict) or not _token(block.get('block_id')):
                raise InputError('附件文档块清单无效。')
            blocks.append(block)
        if not data['has_more']:
            if len({b['block_id'] for b in blocks}) != len(blocks):
                raise InputError('附件文档块分页出现重复，拒绝认领。')
            return blocks
        cursor = data.get('page_token')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise InputError('附件文档块分页不完整。')
        seen.add(cursor)
    raise InputError('附件文档块分页超出上限，拒绝不完整认领。')


def _local_file(artifact, *, required):
    path = Path(artifact['path'])
    if not path.exists() and not required:
        return
    if not path.is_file() or path.stat().st_size != artifact['bytes'] or _digest(path) != artifact['sha256']:
        raise InputError('完整 Excel 文件缺失、字节数或摘要变化；禁止发布。')


def _stage(artifact, record_path):
    role_root = record_path.with_suffix('.work') / artifact['role']
    source = _safe_path(artifact['path'], role_root)
    _local_file(artifact, required=True)
    target = _safe_path(role_root / '.report-upload' / artifact['file_name'], role_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with source.open('rb') as incoming, target.open('xb') as outgoing:
            for part in iter(lambda: incoming.read(1024 * 1024), b''):
                outgoing.write(part)
    if not target.is_file() or target.stat().st_size != artifact['bytes'] or _digest(target) != artifact['sha256']:
        raise InputError('附件上传暂存文件与完整 Excel 不一致。')
    return target


def _download_verify(settings, binding, artifact, token, record_path):
    if not _token(token):
        raise InputError('附件素材 token 无效。')
    work = record_path.with_suffix('.work')
    directory = _safe_path(work / '.attachment-verification', Path(settings['workspace_root']).resolve())
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (uuid.uuid4().hex + '.xlsx')
    try:
        _call(settings, binding, ['docs', '+media-download', '--token', token,
              '--output', _cli_path(settings, destination)], timeout=300)
        _safe_path(destination, directory)
        if (not destination.is_file() or destination.stat().st_size != artifact['bytes']
                or _digest(destination) != artifact['sha256']):
            raise InputError('飞书附件全文件回读与本地 Excel 字节不一致。')
    finally:
        # Only unlink this generated verification path; never recursively clean work.
        destination.unlink(missing_ok=True)


def _verify(settings, binding, artifact, entry, record_path):
    block = _get_block(settings, binding, entry.get('file_block_id'))
    file = block.get('file')
    if (block.get('block_type') != 23 or not isinstance(file, dict)
            or file.get('name') != artifact['file_name'] or not _token(file.get('token'))):
        raise InputError('报告内附件块的类型、名称或素材不一致。')
    token = file['token']
    if entry.get('file_token') and entry['file_token'] != token:
        raise InputError('报告附件素材已变化，禁止覆盖或认领其他文件。')
    parent_id = entry.get('view_block_id') or binding['document_id']
    if block.get('parent_id') != parent_id:
        raise InputError('附件 File 块不属于预期父块。')
    if entry.get('view_block_id'):
        view = _get_block(settings, binding, entry['view_block_id'])
        if (view.get('block_type') != 33 or view.get('parent_id') != binding['document_id']
                or entry['file_block_id'] not in view.get('children', [])):
            raise InputError('附件 View/File 层级与报告不一致。')
    _download_verify(settings, binding, artifact, token, record_path)
    entry.update(file_token=token, verified_sha256=artifact['sha256'],
                 verified_bytes=artifact['bytes'], state='ready')


def _created_ids(settings, binding, response):
    children = response.get('children') if isinstance(response, dict) else None
    if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict):
        raise InputError('创建附件块的返回结果不完整。')
    child = children[0]
    if child.get('block_type') == 33:
        ids = child.get('children')
        if not _token(child.get('block_id')) or not isinstance(ids, list) or len(ids) != 1 or not _token(ids[0]):
            raise InputError('附件 View/File 返回结构不完整。')
        file_id, view_id = ids[0], child['block_id']
    elif child.get('block_type') == 23 and _token(child.get('block_id')):
        file_id, view_id = child['block_id'], None
    else:
        raise InputError('创建附件返回了其他类型块。')
    file = _get_block(settings, binding, file_id)
    if (file.get('block_type') != 23 or not isinstance(file.get('file'), dict)
            or file.get('parent_id') != (view_id or binding['document_id'])):
        raise InputError('新建附件 File 块与报告不一致。')
    return file_id, view_id


def _reconcile(settings, binding, artifact, entry, record_path):
    """Unknown outcome: only read and recognize a unique, byte-identical file."""
    if not entry.get('file_block_id'):
        matches = [block for block in _all_blocks(settings, binding)
                   if block.get('block_type') == 23
                   and isinstance(block.get('file'), dict)
                   and block['file'].get('name') == artifact['file_name']]
        if len(matches) != 1:
            raise InputError('附件写入结果未知且无法唯一认领；保留 pending，禁止重复插入。')
        block = matches[0]
        entry['file_block_id'] = block['block_id']
        entry['view_block_id'] = None if block.get('parent_id') == binding['document_id'] else block.get('parent_id')
    try:
        _verify(settings, binding, artifact, entry, record_path)
    except InputError as exc:
        raise InputError('附件写入结果尚不能确认；仅允许核查恢复，禁止重复上传或插入。' + str(exc)) from None


def _publish_one(settings, binding, artifact, entry, record_path, save):
    state = entry['state']
    if state == 'ready':
        _verify(settings, binding, artifact, entry, record_path)
        save()
        return
    if state.endswith('_pending'):
        # A saved response makes the completed step known; no write is repeated.
        if state == 'create_pending' and isinstance(entry.get('create_response'), dict):
            file_id, view_id = _created_ids(settings, binding, entry['create_response'])
            entry.update(file_block_id=file_id, view_block_id=view_id, state='created')
            save()
        elif state == 'upload_pending' and _token(entry.get('upload_response', {}).get('file_token')):
            entry.update(file_token=entry['upload_response']['file_token'], state='uploaded')
            save()
        else:
            _reconcile(settings, binding, artifact, entry, record_path)
            save()
            return
    if entry['state'] == 'prepared':
        _stage(artifact, record_path)
        entry['state'] = 'create_pending'
        save()
        response = _api(settings, binding, 'POST', '/blocks/' + binding['document_id'] + '/children',
            body={'children': [{'block_type': 23, 'file': {'view_type': 1}}], 'index': -1},
            client_token=entry['create_client_token'])
        entry['create_response'] = response
        save()
        file_id, view_id = _created_ids(settings, binding, response)
        entry.update(file_block_id=file_id, view_block_id=view_id, state='created')
        save()
    if entry['state'] == 'created':
        block = _get_block(settings, binding, entry['file_block_id'])
        if block.get('file', {}).get('token'):
            _verify(settings, binding, artifact, entry, record_path)
            save()
            return
        source = _stage(artifact, record_path)
        entry['state'] = 'upload_pending'
        save()
        response = _call(settings, binding, ['docs', '+media-upload', '--doc-id', binding['document_id'],
            '--parent-type', 'docx_file', '--parent-node', entry['file_block_id'], '--file', _cli_path(settings, source)], timeout=300)
        entry['upload_response'] = response
        save()
        if not _token(response.get('file_token')):
            raise InputError('附件上传返回缺少有效 token；保留 pending，不能自动重传。')
        entry.update(file_token=response['file_token'], state='uploaded')
        save()
    if entry['state'] == 'uploaded':
        block = _get_block(settings, binding, entry['file_block_id'])
        remote_token = block.get('file', {}).get('token')
        if remote_token and remote_token != entry['file_token']:
            raise InputError('附件占位块已有其他素材，禁止覆盖。')
        if not remote_token:
            entry['state'] = 'bind_pending'
            save()
            response = _api(settings, binding, 'PATCH', '/blocks/batch_update',
                body={'requests': [{'block_id': entry['file_block_id'], 'replace_file': {'token': entry['file_token']}}]},
                client_token=entry['bind_client_token'])
            entry['bind_response'] = response
            save()
        _verify(settings, binding, artifact, entry, record_path)
        save()


def ensure_attachments(settings, record_path, record, result, report_url):
    """Return two ready entries; persist every checkpoint without changing result.

    full_workbooks[role] requires absolute path, sha256, bytes, role, request,
    environment (testtwo/prod), canonical request_sha256, rules_sha256 and
    validation.status='ok'. Paths must be in <record>.work/<role>/ without links.
    Missing local files are allowed only after an upload is already checkpointed;
    the remote bytes must still match the saved, request-bound artifact digest.
    """
    record_path, binding, artifacts = _inputs(settings, record_path, record, result, report_url)
    state = record.get('report_attachments')
    if state is None:
        state = {'version': VERSION, 'binding': binding, 'roles': {role: {
            'artifact': artifacts[role], 'state': 'prepared',
            'create_client_token': str(uuid.uuid4()), 'bind_client_token': str(uuid.uuid4())}
            for role in ROLES}}
    if (not isinstance(state, dict) or state.get('version') != VERSION
            or state.get('binding') != binding or not isinstance(state.get('roles'), dict)
            or set(state['roles']) != set(ROLES)):
        raise InputError('附件发布状态的报告、身份或请求绑定发生变化。')
    for role in ROLES:
        entry = state['roles'][role]
        if (not isinstance(entry, dict) or entry.get('artifact') != artifacts[role]
                or entry.get('state') not in STATES):
            raise InputError('附件发布状态与本任务完整 Excel 清单不一致。')
        try:
            for key in ('create_client_token', 'bind_client_token'):
                if str(uuid.UUID(entry[key])) != entry[key]:
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise InputError('附件发布幂等键缺失或无效。') from None
        if entry['state'] in {'created', 'upload_pending', 'uploaded', 'bind_pending', 'ready'}:
            if not _token(entry.get('file_block_id')) or (entry.get('view_block_id') is not None
                    and not _token(entry['view_block_id'])):
                raise InputError('附件发布检查点缺少有效 File/View 块身份。')
        if entry['state'] in {'uploaded', 'bind_pending', 'ready'} and not _token(entry.get('file_token')):
            raise InputError('附件发布检查点缺少有效素材身份。')
        if entry['state'] == 'ready' and (entry.get('verified_sha256') != artifacts[role]['sha256']
                or entry.get('verified_bytes') != artifacts[role]['bytes']):
            raise InputError('附件就绪检查点缺少一致的全文件校验结果。')
        _local_file(artifacts[role], required=entry['state'] in ('prepared', 'created'))
    keys = [state['roles'][role][key] for role in ROLES
            for key in ('create_client_token', 'bind_client_token')]
    if len(set(keys)) != len(keys):
        raise InputError('不同角色或步骤的附件幂等键不能复用。')
    policy.ensure_report_bot_authorization(settings)
    record['report_attachments'] = state
    def save():
        policy.atomic_json(record_path, record)
    save()
    for role in ROLES:
        _publish_one(settings, binding, artifacts[role], state['roles'][role], record_path, save)
    return state['roles']
