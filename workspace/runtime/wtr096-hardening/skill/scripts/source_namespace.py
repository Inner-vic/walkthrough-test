"""Source-preserving input adapter for the approved native extractor.

Only the input collection/selection boundary changes. Original basenames,
prefixes, time predicates and every native grep/CSV/statistics method remain
intact. A source identity is never inferred from an archive member's path.
"""
from __future__ import annotations

import ast
import copy
import fnmatch
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import textwrap
import threading

import analyzer_contract
from native_windows_host import filesystem_path

POLICY = 'source-namespace-v3'
AUDIT_VERSION = 'source-namespace-execution-v1'


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with filesystem_path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1024**2), b''):
            result.update(part)
    return result.hexdigest()


def member_summary(member):
    return {key: member[key] for key in ('source', 'basename', 'sha256', 'bytes', 'ordinal')}


def checked_catalog(catalog):
    from source_catalog import validate_catalog
    validate_catalog(catalog)
    if catalog.get('selection_policy') != POLICY:
        raise ValueError('来源命名空间策略不一致。')
    return catalog


def plain_path(path, root=None, *, directory=False):
    """Inspect original lexical ancestors before resolving any path."""
    path = Path(path).absolute()
    if root is not None:
        root = Path(root).absolute()
        if not path.is_relative_to(root):
            raise ValueError('来源命名空间路径越界。')
    for parent in reversed((path, *path.parents)):
        physical = filesystem_path(parent)
        if not physical.exists() and not physical.is_symlink():
            continue
        info = physical.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('来源命名空间拒绝链接或重解析点。')
    physical = filesystem_path(path)
    info = physical.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('来源命名空间须为普通文件或目录。')
    if root is not None and not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('来源命名空间物理路径越界。')
    return path


def scope_name(theme, soc_type):
    name = theme if isinstance(soc_type, list) else soc_type
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or any(char in name for char in '/\\\x00')
            or any(ord(char) < 32 for char in name)):
        raise ValueError('原生提取 scope 必须是明确的单个目录名。')
    return name


def prefix_plan(soc_type, path_config):
    aliases = soc_type if isinstance(soc_type, list) else [soc_type]
    if not aliases or not isinstance(path_config, dict):
        raise ValueError('原生前缀配置无效。')
    result = []
    for alias in aliases:
        if not isinstance(alias, str) or not alias:
            raise ValueError('原生 soc_type 无效。')
        values = path_config.get(alias, [alias])
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError('原生前缀配置结构发生变化。')
        result.extend(values)
    return result


def selected_ids(catalog, prefixes, epochs, parse_time, keep_log):
    """Invoke the unchanged native parser/predicate with source-local state.

    ``ordinal`` fixes the original stable-sort tie order. An envelope containing
    all same-second boundary candidates is never an actual selection.
    """
    members = catalog['members']
    chosen = set()
    observations = []
    for prefix in prefixes:
        candidates = [identifier for identifier, member in members.items()
            if member['eligible'] and fnmatch.fnmatchcase(
                member['basename'].casefold() if catalog['windows'] else member['basename'],
                (prefix + '*.log').casefold() if catalog['windows'] else prefix + '*.log')]
        stamps = {identifier: parse_time(Path(members[identifier]['basename'])) for identifier in candidates}
        ordered = sorted(candidates, key=lambda identifier: (
            stamps[identifier] is not None, stamps[identifier], members[identifier]['ordinal']))
        states, selected = {}, []
        for identifier in ordered:
            member = members[identifier]
            state = states.setdefault(member['source'], {'kept_first_after_end': False})
            if keep_log(Path(member['basename']), epochs, state):
                selected.append(identifier)
                chosen.add(identifier)
        observations.append({'prefix': prefix, 'ordered_member_ids': ordered,
                             'selected_member_ids': selected})
    result = sorted(chosen, key=lambda identifier: members[identifier]['ordinal'])
    # Same-source ambiguous snapshots must not acquire an arbitrary owner.
    identities = {}
    for identifier in result:
        member = members[identifier]
        key = (member['source'], member['basename'].casefold())
        value = (member['basename'], member['sha256'], member['bytes'])
        if key in identities and identities[key] != value:
            raise ValueError('同来源同名日志在实际选集中有不同内容。')
        identities[key] = value
    return result, observations


def _adapter_ast_sha():
    return analyzer_contract.ast_digest(ast.parse(textwrap.dedent(inspect.getsource(_NamespaceAdapter.extract))))


def validate_selection_audit(audit):
    if not isinstance(audit, dict) or audit.get('version') != AUDIT_VERSION or audit.get('policy') != POLICY:
        raise ValueError('来源命名空间执行审计版本不一致。')
    catalog = checked_catalog(audit.get('input_inventory'))
    sources = {row['source'] for row in catalog['members'].values() if row['eligible']}
    if (audit.get('input_inventory_sha256') != canonical_sha(catalog)
            or audit.get('original_extract_logs_ast_sha256') != analyzer_contract.RUNTIME_EXTRACT_LOGS_AST_SHA256
            or audit.get('adapter_module_sha256') != file_sha(Path(__file__))
            or audit.get('adapter_extract_logs_ast_sha256') != _adapter_ast_sha()
            or type(audit.get('source_count')) is not int or audit['source_count'] != len(sources)
            or type(audit.get('mapped_log_count')) is not int or audit['mapped_log_count'] != len(catalog['members'])
            or type(audit.get('legacy_source_used')) is not bool or audit['legacy_source_used'] != ('legacy' in sources)):
        raise ValueError('来源命名空间的输入、来源或适配器代码证明不一致。')
    return copy.deepcopy(audit)


class _NamespaceAdapter:
    def __init__(self, module, catalog):
        self.module = module
        self.catalog = copy.deepcopy(checked_catalog(catalog))
        self.lock = threading.RLock()
        self.scope_locks = {}
        self.scope_plans = {}

    def extract(self, record, theme, soc_type):
        scope = scope_name(theme, soc_type)
        resolver = getattr(record, '_resolve_log_dir_name', None)
        if not callable(resolver) or resolver(theme, soc_type) != scope:
            raise ValueError('原生 scope 解析与受控提取范围不一致。')
        prefixes = prefix_plan(soc_type, record.path_config)
        root = plain_path(record.log_path, directory=True)
        expected, _ = selected_ids(self.catalog, prefixes, record.time_range,
            self.module._parse_cn_dt_from_log_path, self.module.should_keep_log)
        with self.lock:
            prior = self.scope_plans.setdefault((str(root), scope), prefixes)
            if prior != prefixes:
                raise ValueError('同一原生 scope 不能复用不同前缀计划。')
            lock = self.scope_locks.setdefault((str(root), scope), threading.RLock())
        with lock:
            scope_root = root / 'log' / scope
            # Ancestors are checked before creating paths or touching a cache.
            plain_path(root, directory=True)
            if filesystem_path(scope_root).exists():
                plain_path(scope_root, root, directory=True)
            else:
                parent = scope_root.parent
                if filesystem_path(parent).exists():
                    plain_path(parent, root, directory=True)
                filesystem_path(scope_root).mkdir(parents=True, exist_ok=True)
            outputs = []
            for identifier in expected:
                member = self.catalog['members'][identifier]
                source = plain_path(root / member['namespace_path'], root)
                if (filesystem_path(source).stat().st_size != member['bytes']
                        or file_sha(source) != member['sha256']):
                    raise ValueError('原生来源输入缺失或内容变化。')
                target = scope_root / identifier / member['basename']
                if filesystem_path(target.parent).exists():
                    plain_path(target.parent, root, directory=True)
                else:
                    filesystem_path(target.parent).mkdir(exist_ok=False)
                if filesystem_path(target).exists():
                    plain_path(target, root)
                    if (filesystem_path(target).stat().st_size != member['bytes']
                            or file_sha(target) != member['sha256']):
                        raise ValueError('原生 theme 缓存与来源成员内容不一致。')
                else:
                    try:
                        os.link(filesystem_path(source), filesystem_path(target))
                    except OSError:
                        # No symlink fallback: every actual consumer gets a
                        # verified regular file with the original basename.
                        shutil.copy2(filesystem_path(source), filesystem_path(target))
                    plain_path(target, root)
                    if (filesystem_path(target).stat().st_size != member['bytes']
                            or file_sha(target) != member['sha256']):
                        raise ValueError('原生 theme 输入复制校验失败。')
                outputs.append((member['basename'], member['ordinal'], target))
            expected_paths = {str(path.relative_to(scope_root)).replace('\\', '/') for _, _, path in outputs}
            expected_directories = {str(Path(value).parent).replace('\\', '/') for value in expected_paths}
            actual_paths = set()
            for path in filesystem_path(scope_root).rglob('*'):
                logical = scope_root / path.relative_to(filesystem_path(scope_root))
                plain_path(logical, root, directory=path.is_dir())
                if path.is_file():
                    actual_paths.add(logical.relative_to(scope_root).as_posix())
                elif logical.relative_to(scope_root).as_posix() not in expected_directories:
                    raise ValueError('原生 theme 输入含未审计目录。')
            if actual_paths != expected_paths:
                raise ValueError('原生 theme 输入含遗漏或未审计成员。')
            return [path.as_posix() for _, _, path in sorted(outputs)]


def install_source_namespace_patch(perf_csv_module, catalog):
    catalog = checked_catalog(catalog)
    cls = getattr(perf_csv_module, 'RecordPerformance', None)
    original = getattr(cls, 'extract_logs', None) if cls else None
    if not inspect.isfunction(original) or getattr(original, '__source_namespace_policy__', None):
        raise ValueError('原生提取方法缺失或来源命名空间已安装。')
    # The complete original method must still match the independently approved
    # AST. This does not learn/approve an unknown upstream implementation.
    analyzer_contract.validate_extract_logs_contract(inspect.getsource(original))
    if not callable(getattr(perf_csv_module, '_parse_cn_dt_from_log_path', None)) or not callable(getattr(perf_csv_module, 'should_keep_log', None)):
        raise ValueError('原生时间解析或时间谓词缺失。')
    adapter = _NamespaceAdapter(perf_csv_module, catalog)

    @functools.wraps(original)
    def extract(record, theme, soc_type):
        return adapter.extract(record, theme, soc_type)

    extract.__source_namespace_policy__ = POLICY
    extract.__source_namespace_catalog_sha256__ = canonical_sha(catalog)
    extract.__source_namespace_adapter__ = adapter
    sources = {row['source'] for row in catalog['members'].values() if row['eligible']}
    audit = {'version': AUDIT_VERSION, 'policy': POLICY, 'input_inventory': copy.deepcopy(catalog),
        'input_inventory_sha256': canonical_sha(catalog),
        'original_extract_logs_ast_sha256': analyzer_contract.RUNTIME_EXTRACT_LOGS_AST_SHA256,
        'adapter_module_sha256': file_sha(Path(__file__)), 'adapter_extract_logs_ast_sha256': _adapter_ast_sha(),
        'source_count': len(sources), 'mapped_log_count': len(catalog['members']),
        'legacy_source_used': 'legacy' in sources}
    validate_selection_audit(audit)
    cls.extract_logs = extract
    return audit


# The explicit new name is preferred; this alias lets the host change one
# import while preserving its existing call boundary.
install_source_boundary_patch = install_source_namespace_patch
