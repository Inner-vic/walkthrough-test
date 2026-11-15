"""Optional fixed-User reads of one verified template reference.

Default Bot mode is an immediate no-op. This module never copies resources,
changes permissions, requests scopes or turns a User response into a Bot one.
Ready manifests are retained deployment evidence, not signed credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

import report_policy as policy
from request_model import InputError

SCHEMA = 'walkthrough-template-bridge-v1'
ACCESS_SCHEMA = 'template-reference-access-v1'
CHECK_SCHEMA = 'template-source-copy-check-v1'
SNAPSHOT_SESSION_SCHEMA = 'fixed-user-reference-snapshot-session-v1'
SNAPSHOT_TIMEOUT_SECONDS = 180
SNAPSHOT_MAX_CALLS = 15  # One workbook, seven layouts and seven cell ranges.
ROLES = frozenset(('baseline', 'replacement'))
VERIFIED_FLAGS = ('verified', 'equivalent', 'bot_readable', 'bot_copyable',
                  'private_scope_verified', 'label_preserved', 'bot_identity_verified')


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _token(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _plain(path, *, directory=False, missing=False):
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise InputError('模板桥接记录或其父目录缺失。') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InputError('模板桥接路径禁止链接或目录联接。')
        if entry == path:
            wanted = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not wanted:
                raise InputError('模板桥接路径不是所需的普通文件或目录。')
            if not directory and getattr(info, 'st_nlink', 1) != 1:
                raise InputError('模板桥接记录不能使用多路径硬链接。')
        elif not stat.S_ISDIR(info.st_mode):
            raise InputError('模板桥接父路径不是普通目录。')


def settings_binding(settings):
    if not isinstance(settings, dict):
        raise InputError('模板桥接配置须为对象。')
    keys = ('project_id', 'expected_lark_app_id', 'expected_operator_open_id',
            'lark_profile', 'report_lark_profile', 'lark_home')
    if any(not isinstance(settings.get(k), str) or not settings[k].strip() for k in keys):
        raise InputError('固定User模板模式缺少原项目、身份或隔离profile配置。')
    app, operator = settings['expected_lark_app_id'], settings['expected_operator_open_id']
    if (not re.fullmatch(r'cli_[A-Za-z0-9_-]+', app)
            or not re.fullmatch(r'ou_[A-Za-z0-9_-]+', operator)
            or settings['lark_profile'] == settings['report_lark_profile']
            or any(any(c in settings[k] for c in ('\x00','\n','\r')) for k in keys)):
        raise InputError('模板固定身份或User/Bot隔离配置无效。')
    home = Path(settings['lark_home']).expanduser()
    if not home.is_absolute():
        raise InputError('模板固定身份目录须为绝对路径。')
    return {'project_id': settings['project_id'], 'app_id': app,
            'operator_open_id': operator, 'user_profile': settings['lark_profile'],
            'report_bot_profile': settings['report_lark_profile'],
            'lark_home_sha256': hashlib.sha256(str(home.resolve()).encode()).hexdigest()}


def manifest_path(settings, value, *, existing=True):
    """Reject foreign/identity paths before opening any manifest bytes."""
    workspace_text = settings.get('workspace_root')
    if not isinstance(workspace_text, str) or not Path(workspace_text).is_absolute():
        raise InputError('模板桥接缺少明确工作区。')
    workspace = Path(workspace_text).absolute()
    _plain(workspace, directory=True)
    workspace = workspace.resolve(strict=True)
    if workspace.parent == workspace:
        raise InputError('模板桥接不能以磁盘根目录为工作区。')
    if not isinstance(value, (str, Path)) or any(c in str(value) for c in ('\x00','\n','\r')):
        raise InputError('模板桥接记录路径无效。')
    path = Path(value).absolute()
    if (not Path(value).is_absolute() or '..' in Path(value).parts
            or any(':' in part for part in Path(value).parts[1:])):
        raise InputError('模板桥接记录须为安全绝对路径，不能使用ADS。')
    root = workspace / 'data/walkthrough-template-bridge'
    if path == root or not path.is_relative_to(root):
        raise InputError('模板桥接记录只能位于本工作区data/walkthrough-template-bridge。')
    home = Path(str(settings.get('lark_home') or '')).expanduser()
    if home.is_absolute() and (path == home.absolute() or path.is_relative_to(home.absolute())
                              or path.resolve().is_relative_to(home.resolve())):
        raise InputError('模板桥接记录不能读取身份目录。')
    _plain(path, missing=not existing)
    if not path.resolve().is_relative_to(root.resolve()):
        raise InputError('模板桥接记录解析后越界。')
    return path.resolve()


def _secret_free(value):
    secrets = {'appsecret','clientsecret','accesstoken','refreshtoken','authorization',
               'password','devicecode','tenantaccesstoken','appaccesstoken'}
    if isinstance(value, dict):
        for key, child in value.items():
            if re.sub('[^a-z]', '', str(key).lower()) in secrets:
                raise InputError('模板桥接记录不能包含授权凭据。')
            _secret_free(child)
    elif isinstance(value, list):
        for child in value:
            _secret_free(child)


def _read(settings, path):
    path = manifest_path(settings, path)
    before = path.stat()
    if before.st_size > 1024**2:
        raise InputError('模板桥接记录超出大小上限。')
    try:
        raw = path.read_bytes()
        record = json.loads(raw.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        raise InputError('模板桥接记录不是有效UTF-8 JSON。') from None
    after = path.stat()
    _plain(path)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise InputError('模板桥接记录在读取期间发生变化。')
    _secret_free(record)
    return record, hashlib.sha256(raw).hexdigest()


def _sheet_url(value, token):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port is not None
                or parsed.query or parsed.fragment
                or not (parsed.hostname or '').endswith(('.feishu.cn','.larksuite.com'))
                or parsed.path.rstrip('/') != '/sheets/' + token):
            raise ValueError
    except (TypeError, ValueError):
        raise InputError('模板中继链接与新token不一致。') from None


def _data(payload, identity):
    if not isinstance(payload, dict) or payload.get('ok') is not True or payload.get('identity') != identity:
        raise InputError('模板接口未返回真实成功的固定'+identity+'身份。')
    value = payload.get('data')
    if isinstance(value, dict) and 'code' in value:
        if type(value['code']) is not int or value['code'] != 0:
            raise InputError('模板接口返回业务错误。')
        if isinstance(value.get('data'), dict):
            value = value['data']
        else:
            raise InputError('模板接口缺少原生成功数据。')
    if not isinstance(value, dict):
        raise InputError('模板接口缺少可校验数据。')
    return value


def _check_user(settings, binding):
    if Path(policy.WORKSPACE_ROOT).resolve() != Path(settings['workspace_root']).resolve():
        raise InputError('原模板CLI工作区绑定已变化。')
    value = policy.ensure_user_authorization(settings, binding['operator_open_id'])
    if not isinstance(value, dict) or value.get('open_id') != binding['operator_open_id']:
        raise InputError('固定模板User身份核验失败。')


def _check_bot(settings, binding, expected_open_id):
    # First enforce actual auth-status for the expected App and bot-only
    # profile; then require the documented fixed GET's actual Bot open_id.
    value = policy.ensure_report_bot_authorization(settings)
    if (not isinstance(value, dict) or value.get('profile') != binding['report_bot_profile']
            or value.get('available') is not True):
        raise InputError('当前项目独立报告Bot身份核验失败。')
    payload = policy.run_lark(settings, ['api', 'GET', '/open-apis/bot/v3/info', '--as', 'bot'],
                              profile=binding['report_bot_profile'], timeout=60)
    raw = payload.get('data') if isinstance(payload, dict) else None
    if isinstance(raw, dict) and 'code' in raw:
        # Exact legacy native-body representation; do not guess nested paths.
        if (payload.get('ok') is not True or payload.get('identity') != 'bot'
                or type(raw['code']) is not int or raw['code'] != 0
                or not isinstance(raw.get('bot'), dict)):
            raise InputError('当前报告Bot只读接口未返回可校验的原生成功回执。')
        bot = raw['bot']
    else:
        # CLI-normalized target response puts open_id directly in data.
        bot = _data(payload, 'bot')
    if bot.get('open_id') != expected_open_id:
        raise InputError('中继记录与当前项目报告Bot身份不一致。')
    if bot.get('app_id') is not None and bot['app_id'] != binding['app_id']:
        raise InputError('中继记录与报告Bot应用不一致。')


def _ready(settings, references, override, *, check_actors):
    options = settings.get('report_delivery') or {}
    if (options.get('native_template_reference_reader') != 'fixed_user'
            or options.get('native_template_spreadsheet_token') != override):
        raise InputError('固定User模板模式或中继配置在运行中变化。')
    binding = settings_binding(settings)
    path = options.get('native_template_bridge_manifest')
    record, checksum = _read(settings, path)
    if not isinstance(record, dict) or record.get('schema') != SCHEMA or record.get('state') != 'ready':
        raise InputError('固定User模板模式需要真实ready中继记录；计划或pending不是ready。')
    if record.get('binding') != binding:
        raise InputError('中继记录的项目/User/App/profile/home绑定已变化。')
    source, relay, bot, verification = [record.get(k) for k in ('source','relay','bot','verification')]
    if not isinstance(source, dict) or not _token(source.get('token')) or not _token(source.get('sheet_id')):
        raise InputError('中继记录缺少明确原模板及子表reference。')
    if (not isinstance(references, dict) or set(references) != ROLES
            or any(not isinstance(r, dict) or r.get('token') != source['token']
                   or r.get('sheet_id') != source['sheet_id'] for r in references.values())):
        raise InputError('两窗原模板reference与中继来源不一致。')
    if (not isinstance(relay, dict) or not _token(relay.get('token')) or relay['token'] == source['token']
            or override != relay['token'] or not isinstance(relay.get('name'), str) or not relay['name'].strip()):
        raise InputError('固定User模式必须明确使用记录中的新中继，不能使用原模板或其他token。')
    _sheet_url(relay.get('url'), relay['token'])
    copied = _data(record.get('copy'), 'user')
    if (copied.get('copied') is not True or copied.get('file_type') != 'sheet'
            or copied.get('source_type') != 'sheet' or copied.get('source_file_token') != source['token']
            or copied.get('file_token') != relay['token'] or copied.get('url') != relay['url']
            or copied.get('name') != relay['name']
            or 'permission_grant' in copied):
        raise InputError('中继复制回执未证明由User合法复制出唯一新token。')
    if (not isinstance(bot, dict) or not isinstance(bot.get('open_id'), str)
            or not re.fullmatch(r'ou_[A-Za-z0-9_-]+',bot['open_id']) or bot.get('app_id') != binding['app_id']):
        raise InputError('中继记录缺少同项目报告Bot身份。')
    if (not isinstance(verification, dict) or any(verification.get(k) is not True for k in VERIFIED_FLAGS)
            or not _hash(verification.get('source_snapshot_sha256'))
            or not _hash(verification.get('relay_snapshot_sha256'))):
        raise InputError('中继等价、私有范围、密级或Bot复制能力尚未全部真实核验。')
    if check_actors:
        _check_user(settings, binding)
        _check_bot(settings, binding, bot['open_id'])
    return {'schema': ACCESS_SCHEMA, 'mode': 'fixed_user', **binding,
            'source_token': source['token'], 'source_sheet_id': source['sheet_id'],
            'relay_token': relay['token'], 'relay_url': relay['url'], 'bot_open_id': bot['open_id'],
            'bot_identity_check': 'same_app_bot_profile_and_bot_info', 'fresh_bot_open_id_verified': True,
            'manifest_sha256': checksum, 'verification_sha256': canonical_sha(verification)}


def build_source_binding(settings, references, override):
    if not isinstance(settings, dict) or not isinstance(settings.get('report_delivery', {}), dict):
        raise InputError('模板读取配置须为对象。')
    mode = settings.get('report_delivery', {}).get('native_template_reference_reader', 'bot')
    if mode == 'bot':
        return None  # Preserve the complete original v1 dictionary and side effects.
    if mode != 'fixed_user':
        raise InputError('原模板读取身份只能明确为bot或fixed_user。')
    if not _token(override):
        raise InputError('fixed_user必须配置已核验中继token。')
    return _ready(settings, references, override, check_actors=True)


def _revalidate(settings, access):
    if not isinstance(access, dict) or access.get('schema') != ACCESS_SCHEMA or access.get('mode') != 'fixed_user':
        raise InputError('缺少明确固定User模板绑定。')
    references = {role: {'token': access.get('source_token'), 'sheet_id': access.get('source_sheet_id')}
                  for role in ROLES}
    current = _ready(settings, references, access.get('relay_token'), check_actors=False)
    if current != access:
        raise InputError('原模板中继记录或公开绑定在发布中变化。')
    return current


def _output_path(settings, value):
    if not isinstance(value, str) or Path(value).is_absolute() or '..' in Path(value).parts or '\\' in value or ':' in value:
        raise InputError('原模板只读快照输出必须是工作区内相对路径。')
    workspace = Path(settings['workspace_root']).resolve()
    runtime_value = settings.get('runtime_state_dir')
    if not isinstance(runtime_value, str) or not Path(runtime_value).is_absolute():
        raise InputError('原模板快照缺少明确任务runtime。')
    runtime = Path(runtime_value).absolute()
    path = (workspace / value).absolute()
    bridge = workspace / 'data/walkthrough-template-bridge'
    if (runtime == workspace or not runtime.is_relative_to(workspace) or runtime.is_relative_to(bridge)
            or path == runtime or not path.is_relative_to(runtime)
            or path.exists()):
        raise InputError('原模板快照只能写入本任务runtime的新文件，不能覆盖其他记录。')
    _plain(path, missing=True)
    home = Path(settings['lark_home']).resolve()
    if path.resolve().is_relative_to(home) or not path.resolve().is_relative_to(runtime.resolve()):
        raise InputError('原模板快照输出越界或进入身份目录。')


def _command(settings, access, args):
    specs = {('sheets','+workbook-info'): ({'--spreadsheet-token'}, set()),
             ('sheets','+sheet-info'): ({'--spreadsheet-token','--sheet-id'}, {'--include'}),
             ('sheets','+cells-get'): ({'--spreadsheet-token','--sheet-id','--range','--include','--output-path'}, set()),
             ('drive','permission.members','auth'): ({'--token','--type','--action'}, set())}
    if not isinstance(args, (tuple, list)) or any(not isinstance(a, str) or not a or '\x00' in a or '\n' in a or '\r' in a for a in args):
        raise InputError('原模板读取参数无效。')
    if not isinstance(access, dict) or not _token(access.get('source_token')):
        raise InputError('缺少受限原模板读取绑定。')
    prefix = tuple(args[:3]) if args and args[0] == 'drive' else tuple(args[:2])
    if prefix not in specs:
        raise InputError('固定User模板caller只允许原reference读取及copy权限只读核验。')
    required, optional = specs[prefix]
    values = {}
    tail = args[len(prefix):]
    if len(tail) % 2:
        raise InputError('原模板读取flag须有唯一取值。')
    for flag, value in zip(tail[::2], tail[1::2]):
        if flag not in required | optional or flag in values or value.startswith('--'):
            raise InputError('原模板读取含重复、身份覆盖或未批准flag。')
        values[flag] = value
    if not required.issubset(values):
        raise InputError('原模板读取缺少必要参数。')
    key = '--token' if prefix[0] == 'drive' else '--spreadsheet-token'
    if values[key] != access['source_token']:
        raise InputError('固定User只能读取原reference，不得操作中继或最终结果表。')
    if prefix[0] == 'drive' and (values['--type'] != 'sheet' or values['--action'] != 'copy'):
        raise InputError('仅允许原模板copy权限核验。')
    if '--sheet-id' in values and not _token(values['--sheet-id']):
        raise InputError('原模板子表ID无效。')
    if '--range' in values and not re.fullmatch(r'B1:O[1-9][0-9]{0,5}',values['--range']):
        raise InputError('原模板读取必须为原生完整B:O范围。')
    if '--output-path' in values:
        _output_path(settings, values['--output-path'])
    return list(args)


def user_reference_call(settings, access, args, *, timeout=150):
    command = _command(settings, access, args)  # Reject caller overrides before auth or file reads.
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise InputError('原模板只读请求超时无效。')
    _revalidate(settings, access)
    _check_user(settings, access)
    payload = policy.run_lark(settings, [*command, '--as', 'user'], timeout=timeout,
                              profile=access['user_profile'])
    return _data(payload, 'user')


class _ReferenceReadSession:
    """One thread, role and complete read-only snapshot under the auth lock.

    This is not a credential cache. Every API retains the original command and
    manifest checks. The existing lock still imports, guards and commits its
    encrypted replica; nested CLI calls enter that same reentrant guard.
    """
    def __init__(self, settings, access, *, scope, directory, timeout=SNAPSHOT_TIMEOUT_SECONDS):
        settings_binding(settings)
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= SNAPSHOT_TIMEOUT_SECONDS):
            raise InputError('固定User快照会话超时必须为0到180秒之间的有限值。')
        if (not isinstance(scope, dict) or set(scope) != {'job_key', 'role', 'purpose'}
                or not isinstance(scope.get('job_key'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', scope['job_key'])
                or not isinstance(scope.get('role'), str) or scope['role'] not in ROLES
                or not isinstance(scope.get('purpose'), str)
                or scope['purpose'] not in ('reference', 'reference-before-copy')):
            raise InputError('固定User快照会话缺少精确任务、角色或用途。')
        if not isinstance(directory, (str, Path)):
            raise InputError('固定User快照会话输出目录无效。')
        self.settings, self.access, self.scope = settings, access, scope
        self._settings, self._access, self._scope = deepcopy(settings), deepcopy(access), deepcopy(scope)
        self._settings_sha = canonical_sha(self._settings)
        self._access_sha, self._scope_sha = canonical_sha(self._access), canonical_sha(self._scope)
        self.directory = Path(directory).absolute()
        runtime = Path(str(self._settings.get('runtime_state_dir') or '')).absolute()
        if (not self.directory.is_relative_to(runtime) or self.directory == runtime
                or self.directory.name != scope['role']):
            raise InputError('固定User快照会话输出目录与任务角色不一致。')
        _plain(self.directory, directory=True)
        self.timeout = float(timeout)
        self.state, self.owner, self.failed = 'new', None, False
        self._manager, self._started, self._deadline = None, None, None
        self._calls, self._checks, self._api_seconds, self._auth_seconds = 0, 0, 0.0, 0.0
        self.audit = {'schema': SNAPSHOT_SESSION_SCHEMA, 'scope_sha256': self._scope_sha,
            'binding_sha256': self._access_sha, 'timeout_seconds': self.timeout,
            'max_api_calls': SNAPSHOT_MAX_CALLS, 'status': 'not_started'}

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.failed = True
            raise InputError('固定User快照会话超过有界读取时间，未确认完整快照。')
        return remaining

    def _bound(self, *, allow_failed=False):
        if self.state != 'active' or threading.get_ident() != self.owner:
            self.failed = True
            raise InputError('固定User快照会话已关闭、未进入或属于其他线程。')
        if self.failed and not allow_failed:
            raise InputError('固定User快照会话已有失败，不能继续读取。')
        if (canonical_sha(self.settings) != self._settings_sha
                or canonical_sha(self.access) != self._access_sha
                or canonical_sha(self.scope) != self._scope_sha):
            self.failed = True
            raise InputError('固定User快照会话的配置、来源或任务角色绑定已变化。')
        self._remaining()

    def _identity(self):
        self._bound(allow_failed=True)
        if Path(policy.WORKSPACE_ROOT).resolve() != Path(self._settings['workspace_root']).resolve():
            raise InputError('固定User快照会话CLI工作区绑定已变化。')
        started = time.monotonic()
        self._checks += 1
        try:
            payload = policy.auth_status(self._settings, verify=False, timeout=min(30, self._remaining()))
            policy.ensure_expected_app(self._settings, payload)
            user = policy._authorization_user(payload)
            expiry = policy._authorization_expiry(user.get('expiresAt'))
            if (user.get('openId') != self._access['operator_open_id']
                    or user.get('available') is not True
                    or str(user.get('tokenStatus') or '').casefold() != 'valid'
                    or expiry is None or expiry <= datetime.now(timezone.utc)):
                raise InputError('固定User快照会话的App/User或有效授权状态未通过核验。')
            self._remaining()
        finally:
            self._auth_seconds += time.monotonic() - started

    def __enter__(self):
        if self.state != 'new':
            raise InputError('固定User快照会话不能重复进入或跨快照复用。')
        self._started = time.monotonic()
        self._deadline = self._started + self.timeout
        self.owner = threading.get_ident()
        self._manager = policy.lark_auth_lock(self._settings, timeout=min(60, self._remaining()))
        acquired = False
        try:
            self._manager.__enter__()
            acquired = True
            self.state = 'active'
            self._bound()
            _revalidate(self._settings, self._access)
            self._identity()
            self.audit['status'] = 'reading'
            return self
        except BaseException:
            self.failed = True
            self.state = 'closed'
            self._publish_audit()
            if acquired:
                self._manager.__exit__(*sys.exc_info())
            raise

    def call(self, settings, access, args, *, scope, timeout=150):
        try:
            self._bound()
            if (canonical_sha(settings) != self._settings_sha
                    or canonical_sha(access) != self._access_sha
                    or canonical_sha(scope) != self._scope_sha):
                raise InputError('固定User快照会话不能用于其他任务、角色或来源。')
            command = _command(self._settings, self._access, args)
            if tuple(command[:2]) not in (('sheets', '+workbook-info'), ('sheets', '+sheet-info'), ('sheets', '+cells-get')):
                raise InputError('固定User快照会话只允许完整原模板的三种读取操作。')
            if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300):
                raise InputError('固定User快照会话单次读取超时无效。')
            if self._calls >= SNAPSHOT_MAX_CALLS:
                raise InputError('固定User快照会话已达到单个七子表快照的调用上限。')
            if '--output-path' in command:
                output = (Path(self._settings['workspace_root']) / command[command.index('--output-path') + 1]).absolute()
                if output.parent != self.directory:
                    raise InputError('固定User快照会话输出不能跨任务或角色目录。')
            _revalidate(self._settings, self._access)
            self._calls += 1
            started = time.monotonic()
            try:
                payload = policy.run_lark(self._settings, [*command, '--as', 'user'],
                    timeout=min(float(timeout), self._remaining()), profile=self._access['user_profile'])
                value = _data(payload, 'user')
                self._bound()
                return value
            finally:
                self._api_seconds += time.monotonic() - started
        except BaseException:
            self.failed = True
            raise

    def _publish_audit(self):
        self.audit.update(status='failed' if self.failed else 'verified',
            api_call_count=self._calls, identity_check_count=self._checks,
            api_elapsed_seconds=round(self._api_seconds, 6),
            identity_elapsed_seconds=round(self._auth_seconds, 6),
            elapsed_seconds=round(time.monotonic() - self._started, 6))

    def __exit__(self, exc_type, exc_value, traceback):
        if threading.get_ident() != self.owner or self.state != 'active':
            self.failed = True
            raise InputError('固定User快照会话只能由原线程结束一次。')
        post_error = None
        try:
            self._bound(allow_failed=True)
            _revalidate(self._settings, self._access)
            self._identity()
            if exc_type is None and (self.failed or self._calls != SNAPSHOT_MAX_CALLS):
                raise InputError('固定User快照会话没有完成唯一七子表完整读取。')
        except BaseException as error:
            self.failed = True
            post_error = error
        finally:
            if exc_type is not None:
                self.failed = True
            self.state = 'closed'
            self._publish_audit()
            self._manager.__exit__(exc_type, exc_value, traceback)
        if post_error is not None:
            raise post_error from None
        return False


def user_reference_read_session(settings, access, *, scope, directory, timeout=SNAPSHOT_TIMEOUT_SECONDS):
    """Create a fresh bounded session; never reuse it across roles or snapshots."""
    return _ReferenceReadSession(settings, access, scope=scope, directory=directory, timeout=timeout)


def assert_source_copy_allowed(settings, access):
    value = user_reference_call(settings, access, ['drive','permission.members','auth',
        '--action','copy','--token',access['source_token'],'--type','sheet'])
    if value.get('auth_result') is not True:
        raise InputError('固定User当前未被确认具有原模板copy权限；不复制、不降级。')
    return {'schema': CHECK_SCHEMA, 'identity': 'user', 'auth_result': True,
            'source_token': access['source_token'], 'source_sheet_id': access['source_sheet_id'],
            'operator_open_id': access['operator_open_id'], 'app_id': access['app_id'],
            'profile': access['user_profile'], 'lark_home_sha256': access['lark_home_sha256'],
            'manifest_sha256': access['manifest_sha256'], 'checked_at': datetime.now(timezone.utc).isoformat(),
            'response_sha256': canonical_sha(value)}
