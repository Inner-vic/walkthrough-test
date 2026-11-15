"""Guarded source-boundary execution patch for a private analyzer snapshot.

The supplied statistical rules and ``should_keep_log`` are left unchanged.
The first-after-end state is isolated by trusted source and filename prefix.
Explicitly audited conflicts outside every source's selection are removed
before applying that original predicate; remaining unknown identities fail.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
from pathlib import Path
import re
import textwrap

from analyzer_contract import (RUNTIME_EXTRACT_LOGS_AST_SHA256, ast_digest,
                               canonical_ast_dump, validate_extract_logs_contract)


POLICY = 'source-boundary-v2'
SOURCES = frozenset({'log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth', 'legacy'})
_STATES = '_source_boundary_states_v2'
_RESOLVER = '_source_boundary_identity_v2'
_FILTER = '_source_boundary_filter_v2'
# Retained selection receipts describe the runtime inspect/dedent method, not
# its distinct module-level docstring representation used by static preflight.
EXTRACT_LOGS_AST_SHA256 = RUNTIME_EXTRACT_LOGS_AST_SHA256
PATCHED_EXTRACT_LOGS_AST_SHA256 = 'f6fe918e4106a786aaf2d8ad82d63dfcf6ae4b7acfd44f0ce07da1093a019b46'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _ast_digest(node):
    return ast_digest(node)


def validate_source_map(mapping):
    """Require explicit provenance for every flattened basename in the map.

    ``legacy`` is an explicit compatibility identity, never an inferred
    fallback. Callers must obtain real source identities from the verified
    outer input layout or manifest, not from untrusted archive-member paths.
    """
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('分源选择需要非空的 basename→source 映射。')
    result = {}
    folded = set()
    for name, source in mapping.items():
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or '/' in name or '\\' in name or any(ord(char) < 32 for char in name)
                or not name.endswith('.log') or name.casefold() in folded):
            raise ValueError('分源映射含无效或大小写冲突的日志 basename。')
        if not isinstance(source, str) or source not in SOURCES:
            raise ValueError('分源映射含未知来源，不能猜测 FSDA/FSDB。')
        folded.add(name.casefold())
        result[name] = source
    return result


def validate_excluded_basenames(names):
    """Canonical explicit exclusions; no wildcard, path or case guessing."""
    if not isinstance(names, (list, tuple)):
        raise ValueError('排除冲突日志须为明确的 basename 列表。')
    result = []
    seen = set()
    for name in names:
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or '/' in name or '\\' in name or any(ord(char) < 32 for char in name)
                or not name.endswith('.log') or name in seen
                or any(char in name for char in '*?[]')):
            raise ValueError('排除列表含无效、通配符或重复冲突 basename。')
        seen.add(name)
        result.append(name)
    return sorted(result)


def validate_selection_audit(audit):
    """Validate the retained policy/map/patch receipt without requiring raw logs.

    This checks consistency, not authenticity of an unsigned external receipt.
    Artifact hashes and original input-manifest binding remain the caller's
    responsibility. Additional audit fields are retained for compatibility.
    """
    if not isinstance(audit, dict) or audit.get('policy') != POLICY:
        raise ValueError('分源执行审计缺失或策略版本不一致。')
    mapping = validate_source_map(audit.get('source_by_basename'))
    if (audit.get('source_map_sha256') != _digest(mapping)
            or type(audit.get('source_count')) is not int
            or audit['source_count'] != len(set(mapping.values()))
            or type(audit.get('mapped_log_count')) is not int
            or audit['mapped_log_count'] != len(mapping)
            or type(audit.get('legacy_source_used')) is not bool
            or audit['legacy_source_used'] != ('legacy' in mapping.values())
            or type(audit.get('ast_replacements')) is not int
            or audit['ast_replacements'] != 3):
        raise ValueError('分源执行审计的映射、摘要、计数或 legacy 标记不一致。')
    keys = ('original_extract_logs_ast_sha256', 'patched_extract_logs_ast_sha256')
    if (audit.get(keys[0]) != EXTRACT_LOGS_AST_SHA256
            or audit.get(keys[1]) != PATCHED_EXTRACT_LOGS_AST_SHA256):
        raise ValueError('分源执行审计的修改前后完整方法 AST 不符合已验契约。')
    excluded = validate_excluded_basenames(audit.get('excluded_basenames'))
    if (audit['excluded_basenames'] != excluded
            or audit.get('excluded_basenames_sha256') != _digest(excluded)
            or set(name.casefold() for name in mapping) & set(name.casefold() for name in excluded)):
        raise ValueError('分源执行审计的排除列表、摘要或来源映射交集不一致。')
    inventory = audit.get('input_inventory')
    if (not isinstance(inventory, dict) or inventory.get('selection_policy') != POLICY
            or inventory.get('source_by_basename') != mapping):
        raise ValueError('分源执行审计的输入成员清单与来源映射或策略不一致。')
    try:
        inventory_sha = _digest(inventory)
    except (ValueError, TypeError):
        raise ValueError('分源执行审计的输入成员清单无法进行确定性摘要。') from None
    if audit.get('input_inventory_sha256') != inventory_sha:
        raise ValueError('分源执行审计的输入成员清单摘要不一致。')
    members = inventory.get('basename_hashes')
    if (not isinstance(members, dict) or not members
            or any(name.casefold() not in members for name in list(mapping) + excluded)):
        raise ValueError('分源执行审计缺少对应日志的成员摘要清单。')
    for name, variants in members.items():
        if (not isinstance(name, str) or not isinstance(variants, (list, tuple)) or not variants
                or any(not isinstance(row, (list, tuple)) or len(row) != 2
                       or not isinstance(row[0], str) or not re.fullmatch('[0-9a-f]{64}', row[0])
                       or type(row[1]) is not int or row[1] < 0 for row in variants)):
            raise ValueError('分源执行审计的成员摘要或字节数无效。')
    selected = inventory.get('selected_members')
    if (not isinstance(selected, dict) or not selected
            or type(inventory.get('selected_log_count')) is not int
            or inventory['selected_log_count'] != len(selected)):
        raise ValueError('分源执行审计缺少实际选集成员或选集计数不一致。')
    for name, row in selected.items():
        if (not isinstance(name, str) or name not in mapping
                or not isinstance(row, dict) or row.get('source') != mapping[name]
                or not isinstance(row.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', row['sha256'])
                or type(row.get('bytes')) is not int or row['bytes'] < 0
                or {tuple(pair) for pair in members[name.casefold()]} != {(row['sha256'], row['bytes'])}):
            raise ValueError('分源执行审计实际选集成员的名称、来源或内容摘要不一致。')
    ledger = inventory.get('excluded_conflicts')
    if (not isinstance(ledger, list) or len(ledger) != len(excluded)
            or any(not isinstance(row, dict) for row in ledger)
            or any(not isinstance(row.get('basename'), str) for row in ledger)
            or {row.get('basename') for row in ledger} != set(excluded)):
        raise ValueError('分源执行审计的排除冲突清单与运行时过滤列表不一致。')
    for row in ledger:
        name = row['basename']
        variants = row.get('variants')
        if (row.get('reason') != 'outside_source_aware_full_rule_selection'
                or not isinstance(variants, list) or len(variants) < 2):
            raise ValueError('排除冲突清单缺少已证未选中的原因或完整变体。')
        owners = set()
        member_pairs = {tuple(pair) for pair in members[name.casefold()]}
        for variant in variants:
            if (not isinstance(variant, dict) or not isinstance(variant.get('name'), str)
                    or variant['name'].casefold() != name.casefold() or variant['name'] not in excluded
                    or not isinstance(variant.get('source'), str) or variant['source'] not in SOURCES
                    or not isinstance(variant.get('origin'), str) or not variant['origin']
                    or not isinstance(variant.get('sha256'), str)
                    or not re.fullmatch('[0-9a-f]{64}', variant['sha256'])
                    or type(variant.get('bytes')) is not int or variant['bytes'] < 0
                    or (variant['sha256'], variant['bytes']) not in member_pairs):
                raise ValueError('排除冲突变体的来源、名称或成员摘要不一致。')
            owners.add(variant['source'])
        if len(owners) < 2:
            raise ValueError('排除冲突须有至少两个独立可信来源。')
        if name not in {variant['name'] for variant in variants}:
            raise ValueError('排除的准确 basename 未出现在对应实际变体中。')
    return {**audit, 'source_by_basename': mapping}


def _state_expression():
    return ast.Dict(keys=[ast.Constant(value='kept_first_after_end')],
                    values=[ast.Constant(value=False)])


def _same(node, expected):
    return canonical_ast_dump(node) == canonical_ast_dump(expected)


def _patch_ast(source):
    """Return a method AST after exactly three guarded subtree replacements."""
    # A reverse-proof only proves our own edits; it cannot establish that the
    # deployment's surrounding code is the independently replayed method.
    validate_extract_logs_contract(source)
    tree = ast.parse(textwrap.dedent(source))
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef)
            or tree.body[0].name != 'extract_logs' or tree.body[0].decorator_list):
        raise ValueError('部署分析器 extract_logs 定义结构已变化，拒绝盲目打补丁。')
    original = copy.deepcopy(tree)
    method = tree.body[0]
    if (method.args.posonlyargs or method.args.kwonlyargs or method.args.vararg
            or method.args.kwarg or method.args.defaults
            or [arg.arg for arg in method.args.args] != ['self', 'theme', 'soc_type']):
        raise ValueError('部署分析器 extract_logs 调用契约已变化。')
    if any(isinstance(node, ast.Name) and node.id in (_STATES, _RESOLVER, _FILTER) for node in ast.walk(tree)):
        raise ValueError('部署分析器含分源补丁保留名称，不能重复或覆盖安装。')
    loops = [node for node in ast.walk(method) if isinstance(node, ast.For)
             and isinstance(node.target, ast.Name) and node.target.id == 'file_name'
             and isinstance(node.iter, ast.Name) and node.iter.id == 'valid_filenames']
    if len(loops) != 1:
        raise ValueError('部署分析器前缀循环已变化，不能确定边界状态作用域。')
    loop = loops[0]
    states = [node for node in loop.body if isinstance(node, ast.Assign)
              and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id == 'state' and _same(node.value, _state_expression())]
    calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'should_keep_log']
    state_names = [node for node in ast.walk(method) if isinstance(node, ast.Name) and node.id == 'state']
    expected_call = ast.parse('should_keep_log(log_path, self.time_range, state)', mode='eval').body
    if (len(states) != 1 or len(calls) != 1 or len(state_names) != 2
            or not _same(calls[0], expected_call)):
        raise ValueError('部署分析器时间谓词或 state 使用已变化，拒绝改变统计执行范围。')
    file_lists = [node for node in ast.walk(method) if isinstance(node, ast.Assign)
                  and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == 'log_paths_list']
    expected_list = ast.parse('list(Path(self.log_path).glob(f"{file_name}*.log"))', mode='eval').body
    if (len(file_lists) != 1 or file_lists[0] not in loop.body
            or not _same(file_lists[0].value, expected_list)):
        raise ValueError('部署分析器原始 glob 文件列表赋值已变化，拒绝盲目过滤。')
    file_lists[0].value = ast.Call(func=ast.Name(id=_FILTER, ctx=ast.Load()),
                                 args=[file_lists[0].value], keywords=[])
    # Reset once per prefix, use the unchanged original predicate and filter
    # only the proven exclusion list before its original sorting statement.
    states[0].targets[0].id = _STATES
    states[0].value = ast.Dict(keys=[], values=[])
    replacement = ast.Call(
        func=ast.Attribute(value=ast.Name(id=_STATES, ctx=ast.Load()), attr='setdefault', ctx=ast.Load()),
        args=[ast.Call(func=ast.Name(id=_RESOLVER, ctx=ast.Load()),
                       args=[ast.Name(id='log_path', ctx=ast.Load())], keywords=[]),
              _state_expression()], keywords=[])
    calls[0].args[2] = replacement
    # Prove that reversing the three allowed edits recovers the entire original
    # AST, including ordering, copy policy and every surrounding statement.
    verification = copy.deepcopy(tree)
    verification_loop = [node for node in ast.walk(verification) if isinstance(node, ast.For)
                         and isinstance(node.target, ast.Name) and node.target.id == 'file_name'
                         and isinstance(node.iter, ast.Name) and node.iter.id == 'valid_filenames'][0]
    for node in verification_loop.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == _STATES):
            node.targets[0].id = 'state'
            node.value = _state_expression()
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'log_paths_list'
                and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                and node.value.func.id == _FILTER):
            node.value = node.value.args[0]
    for node in ast.walk(verification_loop):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'should_keep_log':
            node.args[2] = ast.Name(id='state', ctx=ast.Load())
    if not _same(original, verification):
        raise ValueError('分源补丁超出允许的三处 AST 改动。')
    return ast.fix_missing_locations(tree), {
        'policy': POLICY,
        'original_extract_logs_ast_sha256': _ast_digest(original),
        'patched_extract_logs_ast_sha256': _ast_digest(tree),
        'ast_replacements': 3,
    }


def install_source_boundary_patch(perf_csv_module, source_by_basename, excluded_basenames=()):
    """Install on the current process's isolated analyzer import only.

    No upstream file is written, no statistical rule is touched, and unknown
    input identities fail instead of silently sharing another source's state.
    The caller is responsible for binding this map to checked archive/member
    hashes and keeping preflight selection and cached result policy in sync.
    """
    mapping = validate_source_map(source_by_basename)
    excluded = validate_excluded_basenames(excluded_basenames)
    if set(name.casefold() for name in mapping) & set(name.casefold() for name in excluded):
        raise ValueError('明确排除的冲突日志不能同时分配来源。')
    cls = getattr(perf_csv_module, 'RecordPerformance', None)
    original = getattr(cls, 'extract_logs', None) if cls is not None else None
    if not inspect.isfunction(original) or getattr(original, '__source_boundary_policy__', None):
        raise ValueError('部署分析器缺少可补丁的 extract_logs，或补丁已安装。')
    if original.__closure__:
        raise ValueError('部署分析器 extract_logs 使用闭包，不能安全替换。')
    if _RESOLVER in original.__globals__ or _FILTER in original.__globals__:
        raise ValueError('部署分析器分源帮助函数名称已被占用。')
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError):
        raise ValueError('无法读取当前部署分析器方法源码，拒绝盲目打补丁。') from None
    tree, audit = _patch_ast(source)

    def source_for(log_path):
        name = Path(log_path).name
        if name not in mapping:
            raise ValueError('原生选集包含未审计来源的日志：' + name)
        return mapping[name]

    excluded_set = frozenset(excluded)

    def filter_inputs(log_paths):
        # Preserve both the original path objects and their order. Unknown
        # non-excluded names are intentionally left for source_for to reject.
        return [path for path in log_paths if Path(path).name not in excluded_set]

    # Compile in a temporary namespace but bind the function to the original
    # globals, preserving future module-global updates and shared lock objects.
    namespace = dict(original.__globals__)
    namespace[_RESOLVER] = source_for
    namespace[_FILTER] = filter_inputs
    filename = inspect.getsourcefile(original) or '<source-boundary-patch>'
    exec(compile(tree, filename, 'exec'), namespace)
    compiled = namespace['extract_logs']
    import types
    patched = types.FunctionType(compiled.__code__, original.__globals__,
                                 name=original.__name__, argdefs=original.__defaults__)
    patched.__doc__ = original.__doc__
    patched.__module__ = original.__module__
    patched.__qualname__ = original.__qualname__
    patched.__annotations__ = dict(original.__annotations__)
    patched.__source_boundary_policy__ = POLICY
    audit.update(source_by_basename=dict(mapping), source_map_sha256=_digest(mapping), source_count=len(set(mapping.values())),
                 mapped_log_count=len(mapping), legacy_source_used='legacy' in mapping.values(),
                 excluded_basenames=excluded, excluded_basenames_sha256=_digest(excluded))
    original.__globals__[_RESOLVER] = source_for
    original.__globals__[_FILTER] = filter_inputs
    cls.extract_logs = patched
    # The worker adds the checked input_inventory and its digest before the
    # complete receipt is validated. Keeping that step outside this installer
    # avoids ever treating a map alone as proof of archive/member identity.
    return audit
