"""Independent observation of source-qualified native inputs and windows.

The adapter is observed before the original native grep/CSV/statistics code.
Prediction here replays the approved time policy independently of the adapter;
actual parser/predicate calls, collector bytes, INI, constructor and returned
theme files must all agree. Latched errors survive an upstream catch.
"""
from __future__ import annotations

import configparser
import copy
import fnmatch
import functools
import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import threading

from runtime_audit import (RuntimeAuditError, canonical_sha, request_epochs,
                           _epochs, _name_epoch, _rule_soc_type)
from source_namespace import (POLICY, checked_catalog, file_sha, member_summary,
                              plain_path, prefix_plan, scope_name)
from native_windows_host import filesystem_path

_SHA = re.compile(r'[0-9a-f]{64}')


def _checksum(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _predict(catalog, prefixes, epochs):
    """Independent exact replay, with stable ordinal ties and per-source state."""
    members, chosen, observations, calls = catalog['members'], set(), [], []
    for prefix_index, prefix in enumerate(prefixes):
        matches = [identifier for identifier, row in members.items()
            if row['eligible'] and fnmatch.fnmatchcase(
                row['basename'].casefold() if catalog['windows'] else row['basename'],
                (prefix + '*.log').casefold() if catalog['windows'] else prefix + '*.log')]
        stamps = {identifier: _name_epoch(members[identifier]['basename']) for identifier in matches}
        ordered = sorted(matches, key=lambda identifier: (
            stamps[identifier] is not None, stamps[identifier], members[identifier]['ordinal']))
        used, selected = set(), []
        for identifier in ordered:
            member, stamp = members[identifier], stamps[identifier]
            before = member['source'] in used
            kept = stamp is None or epochs[0] <= stamp <= epochs[1]
            if stamp is not None and stamp > epochs[1] and not before:
                kept = True
                used.add(member['source'])
            calls.append({'prefix': prefix, 'prefix_index': prefix_index, 'member_id': identifier, 'basename': member['basename'],
                'source': member['source'], 'parsed_epoch': stamp, 'state_before': before,
                'kept': kept, 'state_after': member['source'] in used})
            if kept:
                selected.append(identifier)
                chosen.add(identifier)
        observations.append({'prefix': prefix, 'ordered_member_ids': ordered,
                             'selected_member_ids': selected})
    selected = sorted(chosen, key=lambda identifier: members[identifier]['ordinal'])
    return selected, observations, calls


def _summaries(catalog, identifiers):
    return {identifier: member_summary(catalog['members'][identifier]) for identifier in identifiers}


def _return_order(catalog, identifiers):
    return sorted(identifiers, key=lambda identifier: (
        catalog['members'][identifier]['basename'], catalog['members'][identifier]['ordinal']))


def _receipt_expected(catalog):
    identifiers = sorted((key for key, row in catalog['members'].items() if row['eligible']),
                         key=lambda key: catalog['members'][key]['ordinal'])
    members = {key: {field: catalog['members'][key][field]
        for field in ('namespace_path', 'sha256', 'bytes')} for key in identifiers}
    return {'version': 'source-materialization-v1', 'catalog_sha256': catalog['catalog_sha256'],
        'members': members, 'member_count': len(members),
        'bytes': sum(row['bytes'] for row in members.values())}


def _validate_request(catalog, request):
    expected = request_epochs(request)
    selection = catalog.get('selection')
    if selection is not None and (selection.get('time_range') != request or _epochs(selection.get('epochs')) != expected):
        raise RuntimeAuditError('来源目录与原请求时间窗不一致。')
    return expected


def _validate_runtime_execution(audit, *, request_time_range, inventory, rules=None):
    catalog = checked_catalog(inventory)
    epochs = _validate_request(catalog, request_time_range)
    if (not isinstance(audit, dict) or type(audit.get('schema_version')) is not int
            or audit['schema_version'] != 3 or audit.get('policy') != POLICY
            or audit.get('status') != 'verified' or audit.get('errors') != []
            or audit.get('request_time_range') != request_time_range
            or audit.get('expected_epoch_range') != epochs
            or audit.get('input_inventory_sha256') != canonical_sha(catalog)
            or audit.get('collector_receipt') != _receipt_expected(catalog)
            or audit.get('auditor_module_sha256') != file_sha(Path(__file__))):
        raise RuntimeAuditError('来源运行审计版本、目录、时间窗或收集证明不一致。')
    root = audit.get('flat_log_root')
    if (not isinstance(root, str) or not root
            or not (PurePosixPath(root).is_absolute() or PureWindowsPath(root).is_absolute())
            or '\x00' in root or '..' in root.replace('\\', '/').split('/')):
        raise RuntimeAuditError('来源运行审计没有明确的原生日志根目录。')
    config = audit.get('path_config')
    if (not isinstance(config, dict) or audit.get('path_config_sha256') != canonical_sha(config)
            or not _checksum(audit.get('rules_sha256'))
            or rules is not None and not isinstance(rules, dict)
            or rules is not None and audit['rules_sha256'] != canonical_sha(rules)):
        raise RuntimeAuditError('来源运行审计的规则或前缀配置不一致。')
    eligible = [key for key, row in catalog['members'].items() if row['eligible']]
    if audit.get('actual_collector_members') != _summaries(catalog, eligible):
        raise RuntimeAuditError('实际 collector 没有证明全部准入成员。')
    specs, calls, initializations = audit.get('theme_specs'), audit.get('extractions'), audit.get('initializations')
    themes, skipped = audit.get('native_theme_names'), audit.get('native_skipped_themes')
    if (not isinstance(specs, dict) or not isinstance(calls, list)
            or not isinstance(themes, list) or any(not isinstance(t, str) for t in themes)
            or len(themes) != len(set(themes)) or not isinstance(skipped, list)
            or any(not isinstance(t, str) for t in skipped) or len(skipped) != len(set(skipped))
            or set(specs) | set(skipped) != set(themes) or set(specs) & set(skipped)
            or not isinstance(initializations, list) or len(initializations) != 1):
        raise RuntimeAuditError('来源运行审计的实例或完整主题覆盖不一致。')
    if rules is not None and themes != [theme for theme in rules if str(theme).strip()]:
        raise RuntimeAuditError('原生实际主题列表不符合完整规则。')
    union = set()
    for theme, spec in specs.items():
        if not isinstance(spec, dict):
            raise RuntimeAuditError('逐主题来源计划无效。')
        prefixes = prefix_plan(spec.get('soc_type'), config)
        expected, observations, predicate_calls = _predict(catalog, prefixes, epochs)
        if (spec.get('prefixes') != prefixes or spec.get('selected_member_ids') != expected
                or spec.get('prefix_observations') != observations
                or spec.get('predicate_calls') != predicate_calls
                or rules is not None and (theme not in rules or _rule_soc_type(rules[theme]) != spec['soc_type'])):
            raise RuntimeAuditError('逐主题计划不符合精确来源边界或原规则。')
        union.update(expected)
    if union != set(catalog['selected_member_ids']):
        raise RuntimeAuditError('完整主题精确选集与来源目录不一致。')
    for row in initializations:
        if (not isinstance(row, dict) or row.get('constructor_epoch_range') != epochs
                or row.get('record_epoch_range') != epochs or row.get('ini_time_range') != request_time_range
                or row.get('ini_log_path') != root or row.get('record_log_path') != root
                or row.get('native_theme_names') != themes or row.get('record_rules_sha256') != audit['rules_sha256']
                or row.get('record_path_config_sha256') != audit['path_config_sha256']
                or not _checksum(row.get('ini_sha256'))):
            raise RuntimeAuditError('实际 INI/构造器/实例绑定不一致。')
    seen, actual_union, scopes = set(), set(), {}
    for row in calls:
        if not isinstance(row, dict) or not isinstance(row.get('theme'), str) or row['theme'] not in specs:
            raise RuntimeAuditError('原生存在未知主题提取。')
        theme, spec = row['theme'], specs[row['theme']]
        scope = scope_name(theme, spec['soc_type'])
        identifiers = spec['selected_member_ids']
        order = _return_order(catalog, identifiers)
        prior = scopes.setdefault(scope, spec['prefixes'])
        if prior != spec['prefixes']:
            raise RuntimeAuditError('原生 scope 被不同前缀计划复用。')
        expected_paths = {key: 'log/' + scope + '/' + key + '/' + catalog['members'][key]['basename'] for key in identifiers}
        if (row.get('scope') != scope or row.get('record_epoch_range') != epochs
                or row.get('ini_time_range') != request_time_range
                or row.get('prefix_observations') != spec['prefix_observations']
                or row.get('actual_predicate_calls') != spec['predicate_calls']
                or row.get('actual_member_ids_in_order') != order
                or row.get('actual_members') != _summaries(catalog, identifiers)
                or row.get('actual_paths') != expected_paths):
            raise RuntimeAuditError('实际原生返回路径、字节或谓词调用与精确选集不一致。')
        seen.add(theme)
        actual_union.update(identifiers)
    if seen != set(specs) or actual_union != union:
        raise RuntimeAuditError('原生未实际完成所有应执行主题。')
    if audit.get('actual_selected_members') != _summaries(catalog, sorted(actual_union)):
        raise RuntimeAuditError('实际原生消费并集不一致。')
    final = audit.get('final_ini')
    if (not isinstance(final, dict) or final.get('time_range') != request_time_range
            or final.get('log_path') != root or not _checksum(final.get('sha256'))):
        raise RuntimeAuditError('缺少最终实际 INI 绑定。')
    return copy.deepcopy(audit)


def validate_runtime_execution(audit, *, request_time_range, inventory, rules=None):
    """Reject malformed external receipts with a finite validation failure."""
    try:
        return _validate_runtime_execution(audit, request_time_range=request_time_range,
                                          inventory=inventory, rules=rules)
    except (TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
        raise RuntimeAuditError('来源运行审计含无效的字段类型或结构。') from None


class RuntimeAudit:
    def __init__(self, perf_csv, perf_value, *, request_time_range, inventory, rules,
                 path_config, flat_log_root, ini_path, collector_receipt=None):
        self.module, self.request = perf_csv, request_time_range
        self.catalog = copy.deepcopy(checked_catalog(inventory))
        self.epochs = _validate_request(self.catalog, self.request)
        self.rules, self.config = copy.deepcopy(rules), copy.deepcopy(path_config)
        self.root, self.ini = Path(flat_log_root).absolute(), Path(ini_path).absolute()
        self.receipt_source = collector_receipt
        self.lock, self.local = threading.RLock(), threading.local()
        self.scope_locks, self.errors, self.initializations, self.extractions = {}, [], [], []
        self.cache, self.hash_files, self.hash_bytes, self.cache_hits = {}, 0, 0, 0
        self.themes = list(perf_value.get_theme_lists(self.rules))
        if len(self.themes) != len(set(self.themes)):
            raise RuntimeAuditError('原生主题列表重复。')
        self.specs, self.skipped = {}, []
        for theme in self.themes:
            exists, soc_type, _ = perf_value.get_theme(self.rules, theme)
            if not exists:
                self.skipped.append(theme)
                continue
            prefixes = prefix_plan(soc_type, self.config)
            selected, observations, calls = _predict(self.catalog, prefixes, self.epochs)
            self.specs[theme] = {'soc_type': copy.deepcopy(soc_type), 'prefixes': prefixes,
                'selected_member_ids': selected, 'prefix_observations': observations, 'predicate_calls': calls}
        union = set().union(*(set(spec['selected_member_ids']) for spec in self.specs.values())) if self.specs else set()
        if union != set(self.catalog['selected_member_ids']):
            raise RuntimeAuditError('来源目录与独立原规则精确选集不一致。')
        # Compare the actual approved parser with the independently retained
        # parser before observing any predicate call.
        for member in self.catalog['members'].values():
            parsed = self.module._parse_cn_dt_from_log_path(Path(member['basename']))
            if (parsed.timestamp() if parsed is not None else None) != _name_epoch(member['basename']):
                raise RuntimeAuditError('原生文件名时间解析契约变化。')

    def latch(self, error):
        with self.lock:
            self.errors.append(type(error).__name__)

    def _hash(self, path):
        path = plain_path(path, self.root)
        physical = filesystem_path(path)
        before = physical.stat()
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
               before.st_ctime_ns, before.st_nlink)
        with self.lock:
            if key in self.cache:
                self.cache_hits += 1
                return self.cache[key], before.st_size
        digest = file_sha(path)
        after = physical.stat()
        current = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                   after.st_ctime_ns, after.st_nlink)
        hardlink_only = current[:4] == key[:4] and after.st_nlink > before.st_nlink
        if current != key and not hardlink_only:
            raise RuntimeAuditError('原生消费日志在哈希核验期间变化。')
        with self.lock:
            self.cache[current] = digest
            self.hash_files += 1
            self.hash_bytes += before.st_size
        return digest, before.st_size

    def _collector(self):
        root = plain_path(self.root, directory=True)
        members_root = plain_path(root / 'members', root, directory=True)
        eligible = {key: row for key, row in self.catalog['members'].items() if row['eligible']}
        actual, paths = {}, set()
        for path in filesystem_path(members_root).rglob('*'):
            logical = members_root / path.relative_to(filesystem_path(members_root))
            plain_path(logical, root, directory=path.is_dir())
            if path.is_file():
                paths.add(logical.relative_to(root).as_posix())
            elif logical.relative_to(members_root).as_posix() not in eligible:
                raise RuntimeAuditError('collector 含未知命名空间目录。')
        if paths != {row['namespace_path'] for row in eligible.values()}:
            raise RuntimeAuditError('collector 含缺失或额外命名空间成员。')
        if any(filesystem_path(root).glob('*.log')):
            raise RuntimeAuditError('collector 仍含未审计的扁平日志。')
        for identifier, row in eligible.items():
            digest, size = self._hash(root / row['namespace_path'])
            if (digest, size) != (row['sha256'], row['bytes']):
                raise RuntimeAuditError('collector 实际字节与来源目录不一致。')
            actual[identifier] = member_summary(row)
        receipt = self.receipt_source() if callable(self.receipt_source) else self.receipt_source
        expected = _receipt_expected(self.catalog)
        if self.receipt_source is not None and receipt != expected:
            raise RuntimeAuditError('collector materialization 回执与实际来源目录不一致。')
        return actual, expected

    def _ini(self):
        plain_path(self.ini)
        raw = filesystem_path(self.ini).read_bytes()
        parser = configparser.ConfigParser()
        parser.read_string(raw.decode('utf-8-sig'))
        value, path = parser.get('PARAMETER', 'time', fallback='').strip(), parser.get('PARAMETER', 'logPath', fallback='').strip()
        if value != self.request or not path or Path(path).resolve() != self.root.resolve():
            raise RuntimeAuditError('实际 INI 窗口或日志目录与请求不一致。')
        return {'time_range': value, 'log_path': str(self.root.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}

    def _record(self, record):
        if (_epochs(getattr(record, 'time_range', None)) != self.epochs
                or Path(getattr(record, 'log_path', '')).resolve() != self.root.resolve()
                or canonical_sha(getattr(record, 'config_date', None)) != canonical_sha(self.rules)
                or canonical_sha(getattr(record, 'path_config', None)) != canonical_sha(self.config)):
            raise RuntimeAuditError('实际 RecordPerformance 窗口、规则或目录漂移。')
        return self._ini()

    def install(self):
        cls = getattr(self.module, 'RecordPerformance', None)
        original_init, original_extract = getattr(cls, '__init__', None), getattr(cls, 'extract_logs', None)
        predicate = getattr(self.module, 'should_keep_log', None)
        if (not callable(original_init) or not callable(original_extract) or not callable(predicate)
                or getattr(original_extract, '__source_namespace_policy__', None) != POLICY
                or getattr(original_extract, '__source_namespace_catalog_sha256__', None) != canonical_sha(self.catalog)
                or getattr(original_extract, '__runtime_audited__', False)):
            raise RuntimeAuditError('必须先安装受检来源命名空间适配器，且只能审计一次。')
        audit = self

        @functools.wraps(predicate)
        def observed_predicate(path, epochs, state):
            context = getattr(audit.local, 'extraction', None)
            if context is None:
                return predicate(path, epochs, state)
            try:
                position = len(context['actual_calls'])
                if position >= len(context['expected_calls']):
                    raise RuntimeAuditError('原生谓词调用超出完整前缀计划。')
                expected = context['expected_calls'][position]
                if (Path(path).name != expected['basename'] or _epochs(epochs) != audit.epochs
                        or state != {'kept_first_after_end': expected['state_before']}):
                    raise RuntimeAuditError('原生谓词收到错误日志、时间窗或来源状态。')
                key = (expected['prefix_index'], expected['source'])
                previous = context['states'].setdefault(key, state)
                if previous is not state or any(value is state for other, value in context['states'].items() if other != key):
                    raise RuntimeAuditError('原生谓词共享或更换了来源边界状态。')
                result = predicate(path, epochs, state)
                if type(result) is not bool or result != expected['kept'] or state != {'kept_first_after_end': expected['state_after']}:
                    raise RuntimeAuditError('实际原生时间谓词不符合受检选择策略。')
                context['actual_calls'].append(copy.deepcopy(expected))
                return result
            except Exception as error:
                audit.latch(error)
                raise

        @functools.wraps(original_init)
        def initialize(record, log_path, theme_lists, time_range, path_config, config_date):
            try:
                if _epochs(time_range) != audit.epochs:
                    raise RuntimeAuditError('原生构造器收到错误时间窗。')
                original_init(record, log_path, theme_lists, time_range, path_config, config_date)
                ini = audit._record(record)
                if list(theme_lists) != audit.themes:
                    raise RuntimeAuditError('原生构造器主题列表与完整规则不一致。')
                audit._collector()
                with audit.lock:
                    if audit.initializations:
                        raise RuntimeAuditError('单窗口出现多个原生性能实例。')
                    audit.initializations.append({'constructor_epoch_range': _epochs(time_range),
                        'record_epoch_range': _epochs(record.time_range), 'ini_time_range': ini['time_range'],
                        'ini_sha256': ini['sha256'], 'ini_log_path': ini['log_path'],
                        'record_log_path': str(Path(record.log_path).resolve()), 'native_theme_names': list(theme_lists),
                        'record_rules_sha256': canonical_sha(record.config_date),
                        'record_path_config_sha256': canonical_sha(record.path_config)})
            except Exception as error:
                audit.latch(error)
                raise

        @functools.wraps(original_extract)
        def extract(record, theme, soc_type):
            try:
                if theme not in audit.specs or audit.specs[theme]['soc_type'] != soc_type:
                    raise RuntimeAuditError('实际提取主题或 soc_type 不属于原规则。')
                spec = audit.specs[theme]
                scope = scope_name(theme, soc_type)
                with audit.lock:
                    lock = audit.scope_locks.setdefault(scope, threading.RLock())
                with lock:
                    ini = audit._record(record)
                    if getattr(audit.local, 'extraction', None) is not None:
                        raise RuntimeAuditError('原生提取意外重入。')
                    context = {'expected_calls': spec['predicate_calls'], 'actual_calls': [], 'states': {}}
                    audit.local.extraction = context
                    try:
                        returned = original_extract(record, theme, soc_type)
                    finally:
                        audit.local.extraction = None
                    audit._record(record)
                    if not isinstance(returned, list) or context['actual_calls'] != spec['predicate_calls']:
                        raise RuntimeAuditError('原生提取没有完整实际执行所有前缀谓词。')
                    identifiers = spec['selected_member_ids']
                    paths = {key: 'log/' + scope + '/' + key + '/' + audit.catalog['members'][key]['basename'] for key in identifiers}
                    by_path = {str((audit.root / value).absolute()): key for key, value in paths.items()}
                    actual, order = {}, []
                    for value in returned:
                        path = Path(value).absolute()
                        identifier = by_path.get(str(path))
                        if identifier is None or identifier in actual:
                            raise RuntimeAuditError('实际返回日志路径重复、跨scope或来源不明。')
                        digest, size = audit._hash(path)
                        member = audit.catalog['members'][identifier]
                        if (digest, size) != (member['sha256'], member['bytes']):
                            raise RuntimeAuditError('实际返回日志字节与来源成员不一致。')
                        actual[identifier] = member_summary(member)
                        order.append(identifier)
                    if set(actual) != set(identifiers) or order != _return_order(audit.catalog, identifiers):
                        raise RuntimeAuditError('实际消费集合或顺序与精确来源选集不一致。')
                    with audit.lock:
                        audit.extractions.append({'theme': theme, 'scope': scope,
                            'record_epoch_range': _epochs(record.time_range), 'ini_time_range': ini['time_range'],
                            'prefix_observations': copy.deepcopy(spec['prefix_observations']),
                            'actual_predicate_calls': context['actual_calls'], 'actual_member_ids_in_order': order,
                            'actual_members': actual, 'actual_paths': paths})
                    return returned
            except Exception as error:
                audit.latch(error)
                raise

        extract.__runtime_audited__ = True
        self.module.should_keep_log, cls.__init__, cls.extract_logs = observed_predicate, initialize, extract
        return self

    def finalize(self):
        try:
            collector, receipt = self._collector()
            final = self._ini()
            if self.errors:
                raise RuntimeAuditError('原生流程曾吞下来源审计错误，禁止导出统计。')
            union = set().union(*(set(row['actual_members']) for row in self.extractions)) if self.extractions else set()
            result = {'schema_version': 3, 'policy': POLICY, 'status': 'verified',
                'request_time_range': self.request, 'expected_epoch_range': self.epochs,
                'input_inventory_sha256': canonical_sha(self.catalog), 'auditor_module_sha256': file_sha(Path(__file__)),
                'rules_sha256': canonical_sha(self.rules), 'path_config': copy.deepcopy(self.config),
                'path_config_sha256': canonical_sha(self.config), 'flat_log_root': str(self.root.resolve()),
                'collector_receipt': receipt, 'actual_collector_members': collector,
                'actual_selected_members': _summaries(self.catalog, sorted(union)),
                'native_theme_names': list(self.themes), 'native_skipped_themes': list(self.skipped),
                'theme_specs': copy.deepcopy(self.specs), 'initializations': copy.deepcopy(self.initializations),
                'extractions': copy.deepcopy(self.extractions), 'errors': [], 'final_ini': final,
                'hash_work': {'physical_files_hashed': self.hash_files, 'bytes_hashed': self.hash_bytes,
                    'stable_stat_cache_hits': self.cache_hits},
                'observation_boundary': 'actual source-qualified collector, INI, constructor, native predicates and extract return before original grep/CSV/statistics',
                'continuous_log_body_coverage_proven': False}
            return validate_runtime_execution(result, request_time_range=self.request, inventory=self.catalog, rules=self.rules)
        except Exception as error:
            self.latch(error)
            raise


def install_runtime_audit(perf_csv, perf_value, **kwargs):
    return RuntimeAudit(perf_csv, perf_value, **kwargs).install()
