"""Bind complete per-window source discovery before either window downloads."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import native_log_source as ad
import service_log_source as pcm
from environment_policy import POLICY_ORDERS, candidates, policy, provider
from request_model import InputError, comparison_context

VERSION = 'environment-selection-full-v2'
TASK_VERSION = 'task-environment-selection-full-v1'
TASK_WINDOW_VERSION = 'environment-selection-task-window-v1'
TASK_PIPELINE_VERSION = 'cloud-window-full-task-v6'
WINDOW_RESULT_VERSION = 'window-result-full-v3'
# Frozen legacy versions above are accepted only by their original validators.
WINDOW_VERSION = 'environment-selection-full-v3'
PER_WINDOW_TASK_VERSION = 'task-environment-selection-per-window-v2'
PER_WINDOW_TASK_WINDOW_VERSION = 'environment-selection-task-window-v2'
PER_WINDOW_PIPELINE_VERSION = 'cloud-window-full-task-v7'
ROLES = ('baseline', 'replacement')
CLASSES = (*ad.LOG_CLASSES, pcm.LOG_CLASS)
CACHE_NAMES = ('environment-selection.json', 'input', 'service-input', 'native-result.json',
               'pcm-result.json', 'window-result.json', 'native-prepared', 'analysis')


def task_requests(windows):
    if not isinstance(windows, dict) or set(windows) != set(ROLES):
        raise InputError('任务级环境选择必须包含 Base 与 PCM更新两个时段。')
    result = {role: comparison_context(windows[role], role) for role in ROLES}
    if len({value['vin'] for value in result.values()}) != 1:
        raise InputError('任务级环境选择拒绝跨 VIN。')
    return result


def task_selection_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _validate_counts(row):
    counts = row.get('counts') if isinstance(row, dict) else None
    if (not isinstance(counts, dict) or set(counts) != set(CLASSES)
            or any(type(number) is not int or not 0 <= number <=
                   (ad.MAX_FILES_PER_CLASS if name in ad.LOG_CLASSES else 200)
                   for name, number in counts.items())
            or row.get('pcm_status') != 'ok'):
        raise InputError('任务级环境选择缺少成功、完整的两窗五类查询依据。')
    return counts


def _legacy_validate_task_decision(value, windows, expected_policy):
    requests = task_requests(windows)
    if (not isinstance(value, dict) or value.get('version') != TASK_VERSION
            or value.get('status') != 'selected' or value.get('request') != requests
            or value.get('policy') != expected_policy
            or expected_policy not in ('test_then_prod', 'test', 'prod')):
        raise InputError('任务级环境证据与本次双窗、版本或策略不一致；不迁移旧证据。')
    env = value.get('environment')
    order = ('test', 'prod') if expected_policy == 'test_then_prod' else (expected_policy,)
    if env not in order or value.get('provider_environment') != provider(env):
        raise InputError('任务级环境选择不符合当前策略。')
    expected = [provider(name) for name in order[:order.index(env) + 1]]
    attempts = value.get('attempts')
    if not isinstance(attempts, list) or len(attempts) != len(expected):
        raise InputError('任务级环境选择缺少完整候选环境查询依据。')
    for index, (attempt, name) in enumerate(zip(attempts, expected)):
        if (not isinstance(attempt, dict) or attempt.get('environment') != name
                or not isinstance(attempt.get('windows'), dict)
                or set(attempt['windows']) != set(ROLES)):
            raise InputError('任务级环境查询顺序或双窗清单不一致。')
        available = {role: any(_validate_counts(attempt['windows'][role]).values()) for role in ROLES}
        if index < len(attempts) - 1:
            if all(available.values()):
                raise InputError('test 两窗均有来源时，禁止无依据切换至 prod。')
        elif not all(available.values()):
            raise InputError('选中环境必须同时具有 Base 和 PCM更新两窗来源，禁止混用环境。')
    return value


def _legacy_window_decision(task_selection, role):
    if not isinstance(task_selection, dict) or role not in ROLES:
        raise InputError('任务级环境证据中的时段角色无效。')
    validate_task_decision(task_selection, task_selection.get('request'), task_selection.get('policy'))
    return {'version': TASK_WINDOW_VERSION, 'selection_scope': 'task', 'role': role,
            'request': deepcopy(task_selection['request'][role]), 'policy': task_selection['policy'],
            'environment': task_selection['environment'],
            'provider_environment': task_selection['provider_environment'],
            'attempts': [{'environment': attempt['environment'], **deepcopy(attempt['windows'][role])}
                         for attempt in task_selection['attempts']],
            'task_selection_sha256': task_selection_sha256(task_selection),
            'task_selection': deepcopy(task_selection)}


def bind_task_window(task_selection, role, metadata, settings, directory):
    expected = window_decision(task_selection, role)
    if (expected['request'] != comparison_context(metadata, role)
            or expected['policy'] != policy(settings)):
        raise InputError('本时段与整单选定环境的请求或策略不一致。')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'environment-selection.json'
    if target.is_file():
        saved = json.loads(target.read_text(encoding='utf-8'))
        if saved != expected:
            raise InputError('已有时段环境证据未绑定本次整单选择；保留旧证据，不迁移缓存。')
    else:
        if any((directory / name).exists() for name in CACHE_NAMES if name != target.name):
            raise InputError('已有时段缓存缺少任务级环境绑定；不迁移或重算旧缓存。')
        ad.write_json(target, expected)
    return expected


def _window_rules_sha256(value):
    fingerprint = value.get('rules_sha256')
    if (value.get('version') != WINDOW_RESULT_VERSION or value.get('status') not in ('ok', 'partial')
            or not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(char not in '0123456789abcdef' for char in fingerprint)):
        raise InputError('完整结果的时段版本、状态或规则指纹无效；保留原结果，不重算。')
    return fingerprint


def _validate_sources(sources, metadata, environment):
    expected = {
        'ad': {'environment': environment, 'log_classes': list(ad.LOG_CLASSES),
               'query': ad.archive_query_metadata(metadata), 'source_policy': ad.SOURCE_POLICY},
        'pcm': {'environment': environment, 'log_classes': [pcm.LOG_CLASS],
                'query': metadata, 'source_policy': pcm.POLICY}}
    if not isinstance(sources, dict) or set(sources) != {'ad', 'pcm'}:
        raise InputError('完整结果缺少准确来源清单；保留原结果。')
    for name, planned in expected.items():
        if not isinstance(sources[name], dict) or any(sources[name].get(key) != value
                                                    for key, value in planned.items()):
            raise InputError('完整结果的来源类型、窗口、环境或策略不一致；保留原结果。')


def _legacy_validate_task_pipeline(pipeline, windows, expected_policy):
    """Reject mixed environments and old finals instead of silently retagging."""
    requests = task_requests(windows)
    if (not isinstance(pipeline, dict) or pipeline.get('version') != TASK_PIPELINE_VERSION
            or not isinstance(pipeline.get('task_environment_selection'), dict)
            or pipeline['task_environment_selection'].get('version') != TASK_VERSION):
        raise InputError('已有结果缺少本版任务级同环境证据；保留原结果，不迁移或重算。')
    task = validate_task_decision(pipeline['task_environment_selection'], requests, expected_policy)
    fingerprint = task_selection_sha256(task)
    if (pipeline.get('environment') != task['provider_environment']
            or pipeline.get('environment_policy') != expected_policy
            or pipeline.get('task_selection_sha256') != fingerprint
            or not isinstance(pipeline.get('windows'), dict)
            or set(pipeline['windows']) != set(ROLES)):
        raise InputError('完整结果的任务级环境、指纹或双窗清单不一致。')
    top_sources = pipeline.get('sources')
    if 'sources' in pipeline and (not isinstance(top_sources, dict) or set(top_sources) != set(ROLES)):
        raise InputError('完整结果的双窗来源清单不一致；保留原结果。')
    hashes = []
    for role in ROLES:
        window = pipeline['windows'][role]
        if (not isinstance(window, dict) or window.get('request') != requests[role]
                or window.get('environment') != task['provider_environment']
                or window.get('environment_selection') != window_decision(task, role)):
            raise InputError('两窗结果未绑定同一任务环境；禁止混合 test 与 prod。')
        hashes.append(_window_rules_sha256(window))
        if 'sources' in window:
            _validate_sources(window['sources'], requests[role], task['provider_environment'])
        elif top_sources is None:
            raise InputError('完整结果缺少绑定双窗的来源清单；保留原结果。')
        if top_sources is not None:
            _validate_sources(top_sources[role], requests[role], task['provider_environment'])
    if len(set(hashes)) != 1:
        raise InputError('完整结果的两窗规则快照不同；保留原结果，不重算。')
    return pipeline


def _legacy_validate_cached_pipeline(pipeline, windows, expected_policy):
    """Recover a verified old same-environment final without manufacturing v6 proof.

    The worker verifies the final file SHA and source/request binding before
    calling this function. New computation never uses this legacy path.
    """
    if isinstance(pipeline, dict) and pipeline.get('version') == TASK_PIPELINE_VERSION:
        return validate_task_pipeline(pipeline, windows, expected_policy)
    requests = task_requests(windows)
    if (not isinstance(pipeline, dict) or pipeline.get('version') != 'cloud-window-full-v5'
            or pipeline.get('environment') != 'per_window'
            or pipeline.get('environment_policy') != expected_policy
            or any(name in pipeline for name in ('task_environment_selection', 'task_selection_sha256'))
            or not isinstance(pipeline.get('windows'), dict) or set(pipeline['windows']) != set(ROLES)
            or not isinstance(pipeline.get('sources'), dict) or set(pipeline['sources']) != set(ROLES)):
        raise InputError('旧最终结果的环境证据不完整；保留原结果，不迁移或重算。')
    environments = [pipeline['windows'][role].get('environment')
                    if isinstance(pipeline['windows'][role], dict) else None for role in ROLES]
    if any(env not in ('testtwo', 'prod') for env in environments) or len(set(environments)) != 1:
        raise InputError('旧最终结果混合了两种环境；保留原结果，不重新计算或补交付。')
    hashes = []
    for role in ROLES:
        value = pipeline['windows'][role]
        decision = value.get('environment_selection')
        if (value.get('request') != requests[role] or value.get('status') not in ('ok', 'partial')
                or not isinstance(decision, dict) or decision.get('version') != VERSION):
            raise InputError('旧最终结果的请求、规则或逐窗证据不一致；保留原结果，不重算。')
        rules_sha256 = _window_rules_sha256(value)
        validate_decision(decision, requests[role], expected_policy)
        if decision['provider_environment'] != value['environment']:
            raise InputError('旧最终结果与其原环境选择不一致；保留原结果。')
        _validate_sources(pipeline['sources'][role], requests[role], value['environment'])
        hashes.append(rules_sha256)
    if len(set(hashes)) != 1:
        raise InputError('旧最终结果的两窗规则快照不同；保留原结果，不重算。')
    return pipeline


def _legacy_validate_decision(value, metadata, expected_policy):
    if isinstance(value, dict) and value.get('version') == TASK_WINDOW_VERSION:
        task = value.get('task_selection')
        if not isinstance(task, dict):
            raise InputError('本时段缺少完整任务级环境证据。')
        validate_task_decision(task, task.get('request'), expected_policy)
        if (value.get('role') not in ROLES or value.get('request') != metadata
                or value != window_decision(task, value['role'])):
            raise InputError('本时段与任务级环境证据不一致。')
        return value
    if (not isinstance(value, dict) or value.get('version') != VERSION
            or value.get('request') != metadata or value.get('policy') != expected_policy
            or expected_policy not in ('test_then_prod', 'test', 'prod')):
        raise InputError('已有环境选择的请求、版本或策略不一致；请使用新任务。')
    env = value.get('environment')
    if value.get('provider_environment') != provider(env):
        raise InputError('环境选择记录不一致。')
    order = ('test', 'prod') if expected_policy == 'test_then_prod' else (expected_policy,)
    if env not in order:
        raise InputError('环境选择不符合当前策略。')
    expected = [provider(e) for e in order[:order.index(env) + 1]]
    attempts = value.get('attempts')
    if not isinstance(attempts, list) or len(attempts) != len(expected):
        raise InputError('环境选择缺少完整查询依据。')
    for i, (row, name) in enumerate(zip(attempts, expected)):
        if not isinstance(row, dict) or row.get('environment') != name:
            raise InputError('环境查询顺序不一致。')
        counts = row.get('counts')
        if (not isinstance(counts, dict) or set(counts) != set(CLASSES)
                or any(type(v) is not int or not 0 <= v <=
                       (ad.MAX_FILES_PER_CLASS if name in ad.LOG_CLASSES else 200)
                       for name, v in counts.items())
                or row.get('pcm_status') not in ('ok', 'failed')):
            raise InputError('环境查询计数或状态无效。')
        if row['pcm_status'] == 'failed' and counts[pcm.LOG_CLASS] != 0:
            raise InputError('失败的 PCM 查询不能宣称有完整候选数据。')
        if i < len(attempts) - 1:
            if any(counts.values()) or row['pcm_status'] != 'ok':
                raise InputError('只有成功查询且全部所需来源候选全空时才能回退。')
        elif not any(counts.values()):
            raise InputError('不能把空环境锁定为可用来源。')
    return value


def _validate_attempt_compat(attempt, metadata, *, role=None):
    """Optional provenance never changes old decisions or candidate counts."""
    if 'empty_response_compat' not in attempt:
        return
    evidence = attempt['empty_response_compat']
    if (attempt.get('environment') != 'prod' or not isinstance(evidence, list)
            or not evidence or len(evidence) > len(CLASSES)):
        raise InputError('空响应兼容证据须来自本窗生产环境的有限首屏查询。')
    try:
        ad.cloud.validate_empty_response_compat(evidence, metadata, environment='prod', role=role)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise InputError('空响应兼容证据与当前时段、角色或查询签名不一致。') from exc
    classes = [row.get('log_class') for row in evidence]
    if (any(name not in CLASSES for name in classes) or len(set(classes)) != len(classes)
            or any(attempt['counts'].get(name) != 0 for name in classes)):
        raise InputError('空响应兼容证据的日志类别重复、未知或与查询计数不一致。')


def _validate_window_choice(value, metadata, expected_policy, *, role=None):
    """Validate the new strict five-source, one-window discovery proof."""
    if (not isinstance(value, dict) or value.get('version') != WINDOW_VERSION
            or value.get('selection_scope') != 'window' or value.get('status') != 'selected'
            or value.get('request') != metadata or value.get('policy') != expected_policy
            or not isinstance(expected_policy, str) or expected_policy not in POLICY_ORDERS):
        raise InputError('逐窗环境证据与请求、版本或策略不一致；不迁移旧证据。')
    env = value.get('environment')
    order = POLICY_ORDERS[expected_policy]
    if env not in order or value.get('provider_environment') != provider(env):
        raise InputError('逐窗环境选择不符合当前策略。')
    attempts = value.get('attempts')
    expected = [provider(name) for name in order[:order.index(env) + 1]]
    if not isinstance(attempts, list) or len(attempts) != len(expected):
        raise InputError('逐窗环境选择缺少完整候选环境查询依据。')
    for index, (attempt, name) in enumerate(zip(attempts, expected)):
        if not isinstance(attempt, dict) or attempt.get('environment') != name:
            raise InputError('逐窗环境查询顺序不一致。')
        available = any(_validate_counts(attempt).values())
        _validate_attempt_compat(attempt, metadata, role=role)
        if index < len(attempts) - 1 and available:
            raise InputError('只有本时段五类来源成功查询且全空后才能切换环境。')
        if index == len(attempts) - 1 and not available:
            raise InputError('不能把本时段的空环境锁定为可用来源。')
    return value


def validate_task_decision(value, windows, expected_policy):
    if isinstance(value, dict) and value.get('version') == TASK_VERSION:
        return _legacy_validate_task_decision(value, windows, expected_policy)
    requests = task_requests(windows)
    if (not isinstance(value, dict) or value.get('version') != PER_WINDOW_TASK_VERSION
            or value.get('status') != 'selected' or value.get('selection_scope') != 'per_window'
            or value.get('environment') != 'per_window' or value.get('provider_environment') != 'per_window'
            or value.get('request') != requests or value.get('policy') != expected_policy
            or not isinstance(value.get('windows'), dict) or set(value['windows']) != set(ROLES)
            or 'attempts' in value):
        raise InputError('任务逐窗环境证据与本次双窗、版本或策略不一致；不迁移旧证据。')
    for role in ROLES:
        _validate_window_choice(value['windows'][role], requests[role], expected_policy, role=role)
    return value


def window_decision(task_selection, role):
    if isinstance(task_selection, dict) and task_selection.get('version') == TASK_VERSION:
        return _legacy_window_decision(task_selection, role)
    if not isinstance(task_selection, dict) or role not in ROLES:
        raise InputError('任务逐窗环境证据中的时段角色无效。')
    validate_task_decision(task_selection, task_selection.get('request'), task_selection.get('policy'))
    choice = task_selection['windows'][role]
    return {'version': PER_WINDOW_TASK_WINDOW_VERSION, 'selection_scope': 'task_window', 'role': role,
            'request': deepcopy(choice['request']), 'policy': task_selection['policy'],
            'environment': choice['environment'], 'provider_environment': choice['provider_environment'],
            'attempts': deepcopy(choice['attempts']),
            'task_selection_sha256': task_selection_sha256(task_selection),
            'task_selection': deepcopy(task_selection)}


def validate_decision(value, metadata, expected_policy):
    if isinstance(value, dict) and value.get('version') in (VERSION, TASK_WINDOW_VERSION):
        return _legacy_validate_decision(value, metadata, expected_policy)
    if isinstance(value, dict) and value.get('version') == PER_WINDOW_TASK_WINDOW_VERSION:
        task = value.get('task_selection')
        if not isinstance(task, dict) or task.get('version') != PER_WINDOW_TASK_VERSION:
            raise InputError('本时段缺少本版完整逐窗环境证据。')
        validate_task_decision(task, task.get('request'), expected_policy)
        if (value.get('role') not in ROLES or value.get('request') != metadata
                or value != window_decision(task, value['role'])):
            raise InputError('本时段与任务逐窗环境证据不一致。')
        return value
    return _validate_window_choice(value, metadata, expected_policy)


def _discover_window(metadata, settings, username, *, role=None, on_attempt=None, on_query=None):
    """Errors propagate; only a complete successful empty query permits fallback."""
    attempts = []
    for env in candidates(settings):
        def query_event(event):
            if on_query:
                on_query(deepcopy(event), deepcopy(compat))
        with ad.cloud.query_audit_context(role=role, request=metadata, environment=env,
                                          on_event=query_event) as compat:
            selected = {name: ad.query_class(ad.archive_query_metadata(metadata), name, username,
                                            environment=env) for name in ad.LOG_CLASSES}
            try:
                service = pcm.discover(metadata, settings, environment=env)
            except (ad.cloud.CloudAuthError, ad.cloud.CloudResponseContractError):
                raise
            except (OSError, RuntimeError, ValueError):
                raise InputError(f'{provider(env)} 的 PCM/CPU 查询失败，无法完成本时段五类来源核对；未切换环境。') from None
        counts = {name: len(selected[name]) for name in ad.LOG_CLASSES}
        counts[pcm.LOG_CLASS] = len(service)
        attempt = {'environment': provider(env), 'counts': counts, 'pcm_status': 'ok'}
        if compat:
            attempt['empty_response_compat'] = deepcopy(compat)
        _validate_counts(attempt)
        _validate_attempt_compat(attempt, metadata, role=role)
        attempts.append(attempt)
        if on_attempt:
            on_attempt(deepcopy(attempts))
        if not any(counts.values()):
            continue
        decision = {'version': WINDOW_VERSION, 'status': 'selected', 'selection_scope': 'window',
                    'request': deepcopy(metadata), 'policy': policy(settings), 'environment': env,
                    'provider_environment': provider(env), 'attempts': attempts}
        _validate_window_choice(decision, metadata, policy(settings), role=role)
        return decision, {'ad': selected, 'pcm': service, 'pcm_error': False}
    environments = ' 和 '.join(provider(env) for env in candidates(settings))
    raise InputError(f'车辆 {metadata["vin"]} 在指定时间段 {metadata["start"]} ～ {metadata["end"]}，'
                     f'{environments} 均未查到所需日志（AD 归档查询包含结束后一小时）。')


def select_task(windows, settings, directory):
    """Choose each window independently and persist both proofs before downloads."""
    requests = task_requests(windows)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'task-environment-selection.json'
    chosen_policy = policy(settings)
    if target.is_file():
        saved = json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(saved, dict) or saved.get('version') != PER_WINDOW_TASK_VERSION:
            raise InputError('旧任务环境证据仅供原结果恢复；不自动迁移或重新计算。')
        return validate_task_decision(saved, requests, chosen_policy), None
    if any((directory / role / name).exists() for role in ROLES for name in CACHE_NAMES):
        raise InputError('已有逐窗环境或输入缓存没有本版任务选择证据；保留旧记录，不自动迁移。')
    username = ad.cloud.load_settings(Path(settings['cloud_settings_path']))['username']
    choices = {}
    discovery = {}
    audit = {'version': PER_WINDOW_TASK_VERSION, 'selection_scope': 'per_window',
             'request': requests, 'policy': chosen_policy, 'windows': {}, 'status': 'querying'}
    guard=threading.RLock()
    def discover_one(role):
        def save_query(event, compat, role=role):
            with guard:
                environment = event.get('environment') if isinstance(event, dict) else None
                if environment not in candidates(settings):
                    raise InputError('查询审计环境不属于当前任务策略。')
                try:
                    ad.cloud.validate_query_audit_event(event, requests[role], environment=environment, role=role)
                    if compat:
                        ad.cloud.validate_empty_response_compat(compat, requests[role],
                                                                 environment=environment, role=role)
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise InputError('查询进度审计与当前时段、角色或查询签名不一致。') from exc
                progress = audit['windows'].setdefault(role, {'request': requests[role]})
                progress.update(status='querying', query_progress=event)
                progress.pop('pending_empty_response_compat', None)
                if compat:
                    progress['pending_empty_response_compat'] = compat
                ad.write_json(directory / 'task-environment-attempts.json', audit)
        def save_attempts(attempts, role=role):
            with guard:
                audit['windows'][role] = {'request': requests[role], 'attempts': attempts,
                                         'status': 'available' if any(attempts[-1]['counts'].values()) else 'missing_sources'}
                ad.write_json(directory / 'task-environment-attempts.json', audit)
        try:
            choices[role], discovery[role] = _discover_window(requests[role], settings, username,
                                                            role=role, on_attempt=save_attempts,
                                                            on_query=save_query)
        except Exception:
            with guard:
                audit['status'] = 'failed'
                if audit['windows'].get(role, {}).get('status') == 'querying':
                    audit['windows'][role]['status'] = 'failed'
                ad.write_json(directory / 'task-environment-attempts.json', audit)
                raise
    if (settings.get('execution_policy') or {}).get('parallel_window_discovery') is True:
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='walkthrough-discovery') as executor:
            pending=[executor.submit(discover_one,role) for role in ROLES]
            errors=[]
            for future in pending:
                try:future.result()
                except Exception as error:errors.append(error)
        if errors:raise errors[0]
    else:
        for role in ROLES:discover_one(role)
    decision = {'version': PER_WINDOW_TASK_VERSION, 'status': 'selected', 'selection_scope': 'per_window',
                'request': requests, 'policy': chosen_policy, 'environment': 'per_window',
                'provider_environment': 'per_window', 'windows': choices}
    validate_task_decision(decision, requests, chosen_policy)
    ad.write_json(target, decision)
    return decision, discovery


def select_window(metadata, settings, directory):
    """Standalone entry follows the same strict new window-selection contract."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'environment-selection.json'
    if target.is_file():
        saved = json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(saved, dict) or saved.get('version') != WINDOW_VERSION:
            raise InputError('旧时段环境证据仅供原结果恢复；不迁移或重算缓存。')
        return _validate_window_choice(saved, metadata, policy(settings)), None
    if any((directory / name).exists() for name in CACHE_NAMES if name != target.name):
        raise InputError('已有日志或缓存缺少环境选择记录；请使用本版新任务目录。')
    username = ad.cloud.load_settings(Path(settings['cloud_settings_path']))['username']
    choice, discovery = _discover_window(metadata, settings, username)
    ad.write_json(target, choice)
    return choice, discovery


def validate_task_pipeline(pipeline, windows, expected_policy):
    """Allow mixed environments only with the explicit new per-window proof."""
    if isinstance(pipeline, dict) and pipeline.get('version') == TASK_PIPELINE_VERSION:
        return _legacy_validate_task_pipeline(pipeline, windows, expected_policy)
    requests = task_requests(windows)
    if (not isinstance(pipeline, dict) or pipeline.get('version') != PER_WINDOW_PIPELINE_VERSION
            or not isinstance(pipeline.get('task_environment_selection'), dict)
            or pipeline['task_environment_selection'].get('version') != PER_WINDOW_TASK_VERSION):
        raise InputError('已有结果缺少本版任务逐窗环境证据；保留原结果，不迁移或重算。')
    task = validate_task_decision(pipeline['task_environment_selection'], requests, expected_policy)
    if (pipeline.get('environment') != 'per_window' or pipeline.get('environment_policy') != expected_policy
            or pipeline.get('task_selection_sha256') != task_selection_sha256(task)
            or not isinstance(pipeline.get('windows'), dict) or set(pipeline['windows']) != set(ROLES)):
        raise InputError('完整结果的逐窗环境、指纹或双窗清单不一致。')
    top_sources = pipeline.get('sources')
    if 'sources' in pipeline and (not isinstance(top_sources, dict) or set(top_sources) != set(ROLES)):
        raise InputError('完整结果的双窗来源清单不一致；保留原结果。')
    hashes = []
    for role in ROLES:
        value = pipeline['windows'][role]
        decision = window_decision(task, role)
        environment = decision['provider_environment']
        if (not isinstance(value, dict) or value.get('request') != requests[role]
                or value.get('environment') != environment or value.get('environment_selection') != decision):
            raise InputError('时段结果未绑定其角色、查询证据及选定环境。')
        hashes.append(_window_rules_sha256(value))
        if 'sources' in value:
            _validate_sources(value['sources'], requests[role], environment)
        elif top_sources is None:
            raise InputError('完整结果缺少绑定双窗的来源清单；保留原结果。')
        if top_sources is not None:
            _validate_sources(top_sources[role], requests[role], environment)
    if len(set(hashes)) != 1:
        raise InputError('完整结果的两窗规则快照不同；保留原结果，不重算。')
    return pipeline


def validate_cached_pipeline(pipeline, windows, expected_policy):
    if isinstance(pipeline, dict) and pipeline.get('version') == PER_WINDOW_PIPELINE_VERSION:
        return validate_task_pipeline(pipeline, windows, expected_policy)
    return _legacy_validate_cached_pipeline(pipeline, windows, expected_policy)


