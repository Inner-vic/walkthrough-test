"""Observe native windows and extraction outputs without changing statistics.

The caller first validates the approved source contract and installs the source
boundary AST patch. This observer then checks the actual INI/RecordPerformance
window and the actual files returned by extract_logs, against the original
predicates and the already checked eligible candidate member identities.
Tied boundary candidates are an envelope; actual choices are proven from the
original collector's observed glob order, including same-scope cache reuse.
Any swallowed upstream exception remains latched and prevents final export.
"""
from __future__ import annotations

import configparser
import copy
from datetime import datetime, timedelta, timezone
import fnmatch
import functools
import hashlib
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading

POLICY = 'runtime-input-window-v2'
_SHA = re.compile(r'[0-9a-f]{64}')
_TS_RE = re.compile(r'(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})(?=\D|$)')
_FILTER = '_source_boundary_filter_v2'


class RuntimeAuditError(ValueError):
    pass


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def request_epochs(text):
    if not isinstance(text, str) or not re.fullmatch(r'[0-9]{14}/[0-9]{14}', text):
        raise RuntimeAuditError('运行审计要求明确的单个请求时间窗。')
    zone = timezone(timedelta(hours=8))
    values = [datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=zone).timestamp()
              for value in text.split('/')]
    if values[0] >= values[1]:
        raise RuntimeAuditError('运行审计请求时间窗无效。')
    return values


def _epochs(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) not in (float, int) or not math.isfinite(item) for item in value)):
        raise RuntimeAuditError('原分析器没有使用有限、明确的实际时间窗。')
    return [float(item) for item in value]


def _selected_members(inventory):
    if (not isinstance(inventory, dict) or not isinstance(inventory.get('selected_members'), dict)
            or not isinstance(inventory.get('source_by_basename'), dict)):
        raise RuntimeAuditError('输入清单缺少唯一 eligible selected_members，不能从所有变体中猜内容。')
    members = copy.deepcopy(inventory['selected_members'])
    mapping = inventory['source_by_basename']
    for name, member in members.items():
        if (not isinstance(name, str) or Path(name).name != name or '\\' in name or '/' in name
                or not name.endswith('.log') or not isinstance(member, dict)
                or set(member) != {'sha256', 'bytes', 'source'}
                or not isinstance(member['sha256'], str) or not _SHA.fullmatch(member['sha256'])
                or type(member['bytes']) is not int or member['bytes'] < 0
                or member['source'] != mapping.get(name)):
            raise RuntimeAuditError('selected_members 的内容、长度或可信来源绑定无效。')
        pairs = inventory.get('basename_hashes', {}).get(name.casefold(), [])
        if [member['sha256'], member['bytes']] not in [list(pair) for pair in pairs]:
            raise RuntimeAuditError('selected member 不在已校验归档成员清单中。')
    return members


@functools.lru_cache(maxsize=16384)
def _name_epoch(name):
    """The approved filename parser, for retained-trace replay only.

    Fresh observation additionally compares this with the actual approved
    module's parser and predicate. pytz is an existing pandas dependency and
    preserves the original Asia/Shanghai.localize behavior for historical dates.
    """
    matches = list(_TS_RE.finditer(name))
    if not matches:
        return None
    import pytz
    return pytz.timezone('Asia/Shanghai').localize(
        datetime.strptime(matches[-1].group(1), '%Y_%m_%d_%H_%M_%S')).timestamp()


def _scope_name(theme, soc_type):
    value = theme if isinstance(soc_type, list) else soc_type
    if (not isinstance(value, str) or not value or '\\' in value
            or PurePosixPath(value).is_absolute() or '..' in value.split('/')):
        raise RuntimeAuditError('原生提取目录标识无效。')
    return value


def _prefix_plan(soc_type, path_config):
    aliases = soc_type if isinstance(soc_type, list) else [soc_type]
    if not isinstance(path_config, dict) or not aliases:
        raise RuntimeAuditError('无法证明原生 soc_type 前缀映射。')
    result = []
    for alias in aliases:
        if not isinstance(alias, str) or not alias:
            raise RuntimeAuditError('原生 soc_type 无效。')
        values = path_config.get(alias, [alias])
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise RuntimeAuditError('无法证明原生 soc_type 前缀映射。')
        result.extend(values)
    return result


def _rule_soc_type(metrics):
    aliases = list(dict.fromkeys(value.strip() for rule in metrics.values()
        for value in str(rule.get('soc_type', '')).split(',') if value.strip()))
    return aliases[0] if len(aliases) == 1 else aliases


def _exact_selection(names, mapping, epochs):
    """Replay the approved stable sort and first boundary per source, no guesses."""
    stamps = {name: _name_epoch(name) for name in names}
    ordered = sorted(names, key=lambda name: (stamps[name] is not None, stamps[name]))
    selected, used = set(), set()
    for name in ordered:
        stamp = stamps[name]
        if not epochs or stamp is None:
            selected.add(name)
        elif stamp < epochs[0]:
            continue
        elif stamp <= epochs[1]:
            selected.add(name)
        elif mapping[name] not in used:
            used.add(mapping[name]); selected.add(name)
    return ordered, sorted(selected)


def _candidate_envelope(prefixes, mapping, excluded, epochs):
    """All possible selected names, including every tied first-boundary name."""
    chosen = set()
    for prefix in prefixes:
        names = [name for name in mapping if name not in excluded and fnmatch.fnmatchcase(name, prefix + '*.log')]
        stamps = {name: _name_epoch(name) for name in names}
        boundary = {}
        for name in names:
            stamp = stamps[name]
            if epochs and stamp is not None and stamp > epochs[1]:
                source = mapping[name]
                boundary[source] = min(boundary.get(source, stamp), stamp)
        for name in names:
            stamp = stamps[name]
            if (not epochs or stamp is None or epochs[0] <= stamp <= epochs[1]
                    or stamp > epochs[1] and stamp == boundary.get(mapping[name])):
                chosen.add(name)
    return sorted(chosen)


def _names(value):
    if (not isinstance(value, list)
            or any(not isinstance(name, str) or PurePosixPath(name).name != name
                   or '\\' in name or not name.endswith('.log') for name in value)
            or len(value) != len(set(value))):
        raise RuntimeAuditError('实际 glob 观察含无效或重复文件名。')
    return value


def _verify_glob_event(event, prefix, mapping, excluded, epochs):
    if not isinstance(event, dict) or event.get('prefix') != prefix:
        raise RuntimeAuditError('实际 glob 前缀调用顺序不符。')
    original = _names(event.get('glob_names'))
    filtered = _names(event.get('filtered_names'))
    if (any(not fnmatch.fnmatchcase(name, prefix + '*.log') for name in original)
            or any(name not in mapping and name not in excluded for name in original)
            or filtered != [name for name in original if name not in excluded]
            or not set(_candidate_envelope([prefix], mapping, excluded, epochs)) <= set(filtered)):
        raise RuntimeAuditError('实际 glob 遗漏候选、来源不明或过滤顺序变化。')
    ordered, chosen = _exact_selection(filtered, mapping, epochs)
    if event.get('sorted_names') != ordered or event.get('selected_names') != chosen:
        raise RuntimeAuditError('实际 glob 稳定排序/原谓词选择不符。')
    return chosen


def validate_runtime_execution(audit, *, request_time_range, inventory, rules=None):
    """Check exact observed-order choices; a candidate subset alone never suffices."""
    if isinstance(inventory,dict) and inventory.get('selection_policy') == 'source-namespace-v3':
        from source_runtime_audit import validate_runtime_execution as validate_namespace_runtime
        return validate_namespace_runtime(audit,request_time_range=request_time_range,inventory=inventory,rules=rules)
    members = _selected_members(inventory)
    expected = request_epochs(request_time_range)
    mapping = inventory['source_by_basename']
    excluded = {row['basename'] for row in inventory.get('excluded_conflicts', [])}
    if (not isinstance(audit, dict) or type(audit.get('schema_version')) is not int or audit.get('schema_version') != 2
            or audit.get('policy') != POLICY or audit.get('status') != 'verified'
            or audit.get('request_time_range') != request_time_range
            or audit.get('expected_epoch_range') != expected
            or audit.get('input_inventory_sha256') != canonical_sha(inventory)
            or audit.get('collector_candidate_members') != members or audit.get('errors') != []):
        raise RuntimeAuditError('原生运行时间窗/候选清单审计不完整；旧审计须使用新任务。')
    rules_sha = audit.get('rules_sha256')
    config = audit.get('path_config')
    if (not isinstance(rules_sha, str) or not _SHA.fullmatch(rules_sha)
            or rules is not None and rules_sha != canonical_sha(rules)
            or not isinstance(config, dict) or canonical_sha(config) != audit.get('path_config_sha256')):
        raise RuntimeAuditError('实际运行规则或前缀配置与请求不一致。')
    root = audit.get('flat_log_root')
    if (not isinstance(root, str) or not root
            or not (PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute())
            or '..' in root.replace('\\', '/').split('/')):
        raise RuntimeAuditError('缺少实际原生日志根目录观察。')
    specs, calls, inits = audit.get('theme_specs'), audit.get('extractions'), audit.get('initializations')
    native_themes, skipped = audit.get('native_theme_names'), audit.get('native_skipped_themes')
    proofs = audit.get('scope_proofs')
    if (not isinstance(specs, dict) or not isinstance(calls, list) or not isinstance(proofs, dict)
            or not isinstance(inits, list) or len(inits) != 1
            or not isinstance(native_themes, list) or any(not isinstance(v, str) for v in native_themes)
            or len(native_themes) != len(set(native_themes))
            or not isinstance(skipped, list) or set(specs) | set(skipped) != set(native_themes)
            or set(specs) & set(skipped)):
        raise RuntimeAuditError('实际原生实例或主题调用审计不完整。')
    candidate_union = set()
    for theme, spec in specs.items():
        if not isinstance(theme, str) or not isinstance(spec, dict):
            raise RuntimeAuditError('逐主题候选预测无效。')
        prefixes = _prefix_plan(spec.get('soc_type'), config)
        names = _candidate_envelope(prefixes, mapping, excluded, expected)
        if (spec.get('prefixes') != prefixes or spec.get('candidate_names') != names
                or not set(names) <= set(members)):
            raise RuntimeAuditError('逐主题候选 envelope 与完整输入不一致。')
        if rules is not None and (theme not in rules or _rule_soc_type(rules[theme]) != spec.get('soc_type')):
            raise RuntimeAuditError('实际主题 soc_type 不符合完整规则。')
        candidate_union.update(names)
    if candidate_union != set(members):
        raise RuntimeAuditError('完整主题候选并集与 selected_members 不一致。')
    for row in inits:
        if (not isinstance(row, dict) or row.get('constructor_epoch_range') != expected
                or row.get('record_epoch_range') != expected or row.get('ini_time_range') != request_time_range
                or row.get('ini_log_path') != root or row.get('record_log_path') != root
                or row.get('native_theme_names') != native_themes or row.get('record_rules_sha256') != rules_sha
                or row.get('record_path_config_sha256') != audit.get('path_config_sha256')
                or not isinstance(row.get('ini_sha256'), str) or not _SHA.fullmatch(row['ini_sha256'])):
            raise RuntimeAuditError('实际 INI/构造器/RecordPerformance 时间窗或路径不一致。')
    for identifier, proof in proofs.items():
        if not isinstance(proof, dict) or identifier != canonical_sha(proof):
            raise RuntimeAuditError('提取 scope 观察证明摘要无效。')
        origin = proof.get('origin_theme')
        if origin not in specs:
            raise RuntimeAuditError('scope 证明的来源主题无效。')
        spec = specs[origin]
        events = proof.get('glob_observations')
        if (proof.get('scope') != _scope_name(origin, spec['soc_type'])
                or proof.get('prefix_plan') != spec['prefixes']
                or not isinstance(events, list) or len(events) != len(spec['prefixes'])):
            raise RuntimeAuditError('scope 证明缺少完整原生 prefix 调用观察。')
        chosen = set()
        for event, prefix in zip(events, spec['prefixes']):
            chosen.update(_verify_glob_event(event, prefix, mapping, excluded, expected))
        if proof.get('expected_names') != sorted(chosen):
            raise RuntimeAuditError('scope 实际顺序对应的精确选择不符。')
    seen, referenced, latest, actual_union = set(), set(), {}, set()
    for row in calls:
        if not isinstance(row, dict) or row.get('theme') not in specs:
            raise RuntimeAuditError('存在未预测的原生主题调用。')
        theme = row['theme']; spec = specs[theme]
        scope = _scope_name(theme, spec['soc_type'])
        identifier = row.get('scope_proof_id')
        if identifier not in proofs:
            raise RuntimeAuditError('提取结果没有可核验的实际 glob scope 证明。')
        proof = proofs[identifier]
        if proof['scope'] != scope or proof['prefix_plan'] != spec['prefixes'] or row.get('scope') != scope:
            raise RuntimeAuditError('提取结果跨 scope 或前缀缓存复用。')
        if row.get('selection_mode') == 'observed_glob':
            if proof['origin_theme'] != theme:
                raise RuntimeAuditError('新提取观察证明与实际发起主题不符。')
            latest[scope] = identifier
        elif row.get('selection_mode') != 'cached_scope' or latest.get(scope) != identifier:
            raise RuntimeAuditError('缓存提取没有先前完成的同 scope 观察证明。')
        names = proof['expected_names']
        actual = {name: members[name] for name in names}
        paths = row.get('actual_paths')
        if (row.get('record_epoch_range') != expected or row.get('ini_time_range') != request_time_range
                or row.get('expected_names') != names or row.get('actual_members') != actual
                or not isinstance(paths, dict) or set(paths) != set(names)
                or any(path != 'log/' + scope + '/' + name for name, path in paths.items())):
            raise RuntimeAuditError('原生 extract_logs 实际消费文件/内容与观察到的精确选择不一致。')
        seen.add(theme); referenced.add(identifier); actual_union.update(names)
    if seen != set(specs) or referenced != set(proofs):
        raise RuntimeAuditError('原生没有实际执行所有应执行主题或存在未关联 scope 证明。')
    if audit.get('actual_selected_members') != {name: members[name] for name in sorted(actual_union)}:
        raise RuntimeAuditError('实际消费并集与逐 scope 精确选择不符。')
    final = audit.get('final_ini')
    if (not isinstance(final, dict) or final.get('time_range') != request_time_range
            or final.get('log_path') != root or not isinstance(final.get('sha256'), str)
            or not _SHA.fullmatch(final['sha256'])):
        raise RuntimeAuditError('缺少最终实际 INI 时间窗核验。')
    return copy.deepcopy(audit)

class RuntimeAudit:
    def __init__(self, perf_csv, perf_value, *, request_time_range, inventory, rules,
                 path_config, flat_log_root, ini_path):
        self.module = perf_csv
        self.request = request_time_range
        self.epochs = request_epochs(request_time_range)
        self.inventory = copy.deepcopy(inventory)
        self.members = _selected_members(inventory)
        self.mapping = copy.deepcopy(inventory['source_by_basename'])
        self.excluded = {row['basename'] for row in inventory.get('excluded_conflicts', [])}
        self.rules = copy.deepcopy(rules)
        self.path_config = copy.deepcopy(path_config)
        self.root = Path(flat_log_root).absolute()
        self.ini = Path(ini_path).absolute()
        self.lock = threading.RLock()
        self.cache = {}
        self.errors = []
        self.initializations = []
        self.extractions = []
        self.local = threading.local()
        self.scope_locks, self.scope_latest, self.proofs = {}, {}, {}
        self.hash_bytes = self.hash_files = self.cache_hits = 0
        self.native_themes = list(perf_value.get_theme_lists(self.rules))
        if len(self.native_themes) != len(set(self.native_themes)):
            raise RuntimeAuditError('原生主题清单重复。')
        self.specs, self.skipped = {}, []
        for theme in self.native_themes:
            exists, soc_type, _ = perf_value.get_theme(self.rules, theme)
            if not exists:
                self.skipped.append(theme)
                continue
            self.specs[theme] = self._predict(soc_type)
        union = set().union(*(set(row['candidate_names']) for row in self.specs.values())) if self.specs else set()
        if union != set(self.members):
            raise RuntimeAuditError('原生原谓词预测与预检 full-rule selected_members 并集不一致。')

    def _predict(self, soc_type):
        prefixes = _prefix_plan(soc_type, self.path_config)
        return {'soc_type': copy.deepcopy(soc_type), 'prefixes': prefixes,
                'candidate_names': _candidate_envelope(prefixes, self.mapping, self.excluded, self.epochs)}

    def _scope_lock(self, scope):
        with self.lock:
            return self.scope_locks.setdefault(scope, threading.RLock())

    def _observe_filter(self, original_filter, log_paths):
        """Observe the existing helper once; return its exact list unchanged."""
        try:
            context = getattr(self.local, 'extraction', None)
            if context is None or len(context['events']) >= len(context['prefixes']):
                raise RuntimeAuditError('实际 glob 没有对应的主题/prefix 调用上下文。')
            prefix = context['prefixes'][len(context['events'])]
            if not isinstance(log_paths, list):
                raise RuntimeAuditError('原生 glob 输入契约变化。')
            root = self.root.resolve()
            def names_for(paths):
                if not isinstance(paths, list):
                    raise RuntimeAuditError('原生 glob 过滤返回契约变化。')
                names = []
                for value in paths:
                    path = Path(value)
                    if path.resolve(strict=True).parent != root or path.parent.resolve() != root:
                        raise RuntimeAuditError('原生 glob 观察路径越界或不是扁平输入。')
                    names.append(path.name)
                return _names(names)
            original_names = names_for(log_paths)
            filtered = original_filter(log_paths)
            filtered_names = names_for(filtered)
            # These use the actual approved parser/predicate on copies only.
            # No path, state or list supplied to the native extractor is changed.
            stamps = {name: self.module._parse_cn_dt_from_log_path(Path(name)) for name in filtered_names}
            ordered = sorted(filtered_names, key=lambda name: (stamps[name] is not None, stamps[name]))
            states, selected = {}, set()
            for name in ordered:
                if name not in self.mapping:
                    raise RuntimeAuditError('原生 glob 来源不明：' + name)
                stamp = stamps[name]
                if (stamp.timestamp() if stamp is not None else None) != _name_epoch(name):
                    raise RuntimeAuditError('实际原分析器文件名时间解析契约变化。')
                state = states.setdefault(self.mapping[name], {'kept_first_after_end': False})
                if self.module.should_keep_log(Path(name), tuple(self.epochs), state):
                    selected.add(name)
            event = {'prefix': prefix, 'glob_names': original_names, 'filtered_names': filtered_names,
                     'sorted_names': ordered, 'selected_names': sorted(selected)}
            _verify_glob_event(event, prefix, self.mapping, self.excluded, self.epochs)
            context['events'].append(event)
            return filtered
        except Exception as error:
            self.latch(error)
            raise

    def _ini_observation(self):
        parser = configparser.ConfigParser()
        data = self.ini.read_bytes()
        parser.read_string(data.decode('utf-8-sig'))
        time_range = parser.get('PARAMETER', 'time', fallback='').strip()
        log_path = parser.get('PARAMETER', 'logPath', fallback='').strip()
        if time_range != self.request or not log_path or Path(log_path).resolve() != self.root.resolve():
            raise RuntimeAuditError('实际使用的 o.ini 时间窗或 logPath 与原请求不一致。')
        return {'time_range': time_range, 'log_path': str(self.root.resolve()), 'sha256': hashlib.sha256(data).hexdigest()}

    def _hash_file(self, path):
        path = Path(path)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_file():
            raise RuntimeAuditError('原生实际日志路径越界或不是普通文件。')
        before = resolved.stat()
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
               before.st_ctime_ns, before.st_nlink)
        with self.lock:
            if key in self.cache:
                self.cache_hits += 1
                return self.cache[key], before.st_size
            value = hashlib.sha256()
            with resolved.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    value.update(block)
            after = resolved.stat()
            current = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                       after.st_ctime_ns, after.st_nlink)
            # Parallel approved extractors may add a hard link to this inode
            # while it is read. That changes ctime/nlink without changing the
            # data. Retain ctime in cache keys; allow only increasing nlink
            # during this read, with dev/inode/size/mtime unchanged. Callers
            # still require the computed SHA to equal the selected member.
            hardlink_only = current[:4] == key[:4] and after.st_nlink > before.st_nlink
            if current != key and not hardlink_only:
                raise RuntimeAuditError('日志在实际内容核验过程中发生变化。')
            self.cache[current] = value.hexdigest()
            self.hash_files += 1
            self.hash_bytes += before.st_size
            return value.hexdigest(), before.st_size

    def _verify_collector(self):
        if not self.root.is_dir() or self.root.is_symlink():
            raise RuntimeAuditError('原生 collector 未创建可信日志根目录。')
        for name, member in self.members.items():
            sha, size = self._hash_file(self.root / name)
            if (sha, size) != (member['sha256'], member['bytes']):
                raise RuntimeAuditError('collector 实际展开内容与选中归档成员不一致：' + name)

    def _record_observation(self, record):
        if (_epochs(getattr(record, 'time_range', None)) != self.epochs
                or Path(getattr(record, 'log_path', '')).resolve() != self.root.resolve()
                or canonical_sha(getattr(record, 'config_date', None)) != canonical_sha(self.rules)
                or canonical_sha(getattr(record, 'path_config', None)) != canonical_sha(self.path_config)):
            raise RuntimeAuditError('实际 RecordPerformance 窗口、路径或规则与请求不一致。')
        return self._ini_observation()

    def latch(self, error):
        with self.lock:
            self.errors.append(type(error).__name__ + ': ' + str(error))

    def install(self):
        cls = getattr(self.module, 'RecordPerformance', None)
        original_init = getattr(cls, '__init__', None)
        original_extract = getattr(cls, 'extract_logs', None)
        if (not callable(original_init) or not callable(original_extract)
                or getattr(original_extract, '__runtime_audited__', False)
                or not getattr(original_extract, '__source_boundary_policy__', None)):
            raise RuntimeAuditError('必须先安装经过结构校验的原生分源补丁，再安装运行观察器。')
        namespace = original_extract.__globals__
        original_filter = namespace.get(_FILTER)
        if not callable(original_filter) or getattr(original_filter, '__runtime_glob_observer__', False):
            raise RuntimeAuditError('原生分源过滤 hook 缺失或已安装观察器。')
        audit = self

        @functools.wraps(original_filter)
        def observe_filter(log_paths):
            return audit._observe_filter(original_filter, log_paths)
        observe_filter.__runtime_glob_observer__ = True

        @functools.wraps(original_init)
        def initialize(record, log_path, theme_lists, time_range, path_config, config_date):
            try:
                if _epochs(time_range) != audit.epochs:
                    raise RuntimeAuditError('RecordPerformance 构造器实际收到错误时间窗。')
                original_init(record, log_path, theme_lists, time_range, path_config, config_date)
                ini = audit._record_observation(record)
                if list(theme_lists) != audit.native_themes:
                    raise RuntimeAuditError('原生实际主题清单与完整请求规则不一致。')
                audit._verify_collector()
                with audit.lock:
                    if audit.initializations:
                        raise RuntimeAuditError('意外创建多个原生性能实例，无法证明单窗口执行。')
                    audit.initializations.append({
                        'constructor_epoch_range': _epochs(time_range), 'record_epoch_range': _epochs(record.time_range),
                        'ini_time_range': ini['time_range'], 'ini_sha256': ini['sha256'],
                        'ini_log_path': ini['log_path'], 'record_log_path': str(Path(record.log_path).resolve()),
                        'native_theme_names': list(theme_lists),
                        'record_rules_sha256': canonical_sha(record.config_date),
                        'record_path_config_sha256': canonical_sha(record.path_config),
                    })
            except Exception as error:
                audit.latch(error)
                raise

        @functools.wraps(original_extract)
        def extract(record, theme, soc_type):
            try:
                if theme not in audit.specs or audit._predict(soc_type) != audit.specs[theme]:
                    raise RuntimeAuditError('实际 extract_logs 的主题/soc_type 与原规则预测不同。')
                scope = _scope_name(theme, soc_type)
                # The upstream scope lock ends before this wrapper resumes.
                # Extend that same-scope serialization through proof publication
                # so a parallel cache hit cannot race its first observed call.
                with audit._scope_lock(scope):
                    ini = audit._record_observation(record)
                    if getattr(audit.local, 'extraction', None) is not None:
                        raise RuntimeAuditError('原生提取观察发生意外重入。')
                    context = {'prefixes': audit.specs[theme]['prefixes'], 'events': []}
                    audit.local.extraction = context
                    try:
                        result = original_extract(record, theme, soc_type)
                    finally:
                        audit.local.extraction = None
                    audit._record_observation(record)
                    if not isinstance(result, list):
                        raise RuntimeAuditError('原生 extract_logs 返回契约变化。')
                    if context['events']:
                        if len(context['events']) != len(context['prefixes']):
                            raise RuntimeAuditError('原生提取未观察到全部 prefix 调用。')
                        chosen = set().union(*(set(row['selected_names']) for row in context['events']))
                        proof = {'origin_theme': theme, 'scope': scope,
                            'prefix_plan': list(context['prefixes']),
                            'glob_observations': copy.deepcopy(context['events']), 'expected_names': sorted(chosen)}
                        identifier, mode = canonical_sha(proof), 'observed_glob'
                    else:
                        with audit.lock:
                            identifier = audit.scope_latest.get(scope)
                            proof = copy.deepcopy(audit.proofs.get(identifier))
                        if (proof is None or proof['scope'] != scope
                                or proof['prefix_plan'] != context['prefixes']):
                            raise RuntimeAuditError('缓存提取缺少同 scope 已完成的实际 glob 证明。')
                        mode = 'cached_scope'
                    expected_names = proof['expected_names']
                    actual, paths, returned_names = {}, {}, []
                    for value in result:
                        path = Path(value).absolute()
                        name = path.name
                        if name in actual or name not in audit.members:
                            raise RuntimeAuditError('实际提取有重复或未审计日志：' + name)
                        expected_path = audit.root / 'log' / scope / name
                        if path != expected_path or path.resolve(strict=True) != expected_path.resolve(strict=True):
                            raise RuntimeAuditError('实际提取路径与同 scope 证明不符：' + name)
                        sha, size = audit._hash_file(path)
                        actual[name] = {'sha256': sha, 'bytes': size, 'source': audit.members[name]['source']}
                        paths[name] = 'log/' + scope + '/' + name
                        returned_names.append(name)
                    expected_members = {name: audit.members[name] for name in expected_names}
                    if actual != expected_members or returned_names != expected_names:
                        raise RuntimeAuditError('实际 extract_logs 文件集合/顺序或内容与精确 glob 证明不同：' + theme)
                    with audit.lock:
                        audit.proofs[identifier] = proof
                        audit.scope_latest[scope] = identifier
                        audit.extractions.append({'theme': theme, 'scope': scope, 'scope_proof_id': identifier,
                            'selection_mode': mode, 'record_epoch_range': _epochs(record.time_range),
                            'ini_time_range': ini['time_range'], 'expected_names': list(expected_names),
                            'actual_members': actual, 'actual_paths': paths})
                    return result
            except Exception as error:
                audit.latch(error)
                raise

        extract.__runtime_audited__ = True
        namespace[_FILTER] = observe_filter
        cls.__init__ = initialize
        cls.extract_logs = extract
        return self

    def finalize(self):
        try:
            self._verify_collector()
            final_ini = self._ini_observation()
            if self.errors:
                raise RuntimeAuditError('原生流程曾吞下运行审计错误，拒绝导出统计结果。')
            result = {
                'schema_version': 2, 'policy': POLICY, 'status': 'verified',
                'request_time_range': self.request, 'expected_epoch_range': self.epochs,
                'input_inventory_sha256': canonical_sha(self.inventory),
                'rules_sha256': canonical_sha(self.rules), 'path_config_sha256': canonical_sha(self.path_config),
                'path_config': copy.deepcopy(self.path_config),
                'flat_log_root': str(self.root.resolve()), 'collector_candidate_members': self.members,
                'actual_selected_members': {name: self.members[name] for name in sorted(
                    set().union(*(set(row['actual_members']) for row in self.extractions)))},
                'native_theme_names': self.native_themes, 'native_skipped_themes': self.skipped,
                'theme_specs': self.specs, 'initializations': self.initializations,
                'scope_proofs': self.proofs,
                'extractions': self.extractions, 'errors': [], 'final_ini': final_ini,
                'hash_work': {'physical_files_hashed': self.hash_files, 'bytes_hashed': self.hash_bytes,
                              'stable_stat_cache_hits': self.cache_hits},
                'observation_boundary': 'actual INI and RecordPerformance; actual extract_logs return before native tag prefilter/mawk',
                'continuous_log_body_coverage_proven': False,
            }
            return validate_runtime_execution(result, request_time_range=self.request,
                                              inventory=self.inventory, rules=self.rules)
        except Exception as error:
            self.latch(error)
            raise


def install_runtime_audit(perf_csv, perf_value, **kwargs):
    if (kwargs.get('inventory') or {}).get('selection_policy') == 'source-namespace-v3':
        from source_runtime_audit import install_runtime_audit as install_namespace_runtime
        return install_namespace_runtime(perf_csv,perf_value,**kwargs)
    return RuntimeAudit(perf_csv, perf_value, **kwargs).install()
