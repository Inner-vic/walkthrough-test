"""Fail-closed static contract for the independently replayed analyzer source.

Only the explicit source family below is supported. An unknown newer analyzer
must be reviewed and replayed before this allowlist is changed; it is never
learned from deployment inputs or replaced with an old bundled source copy.
No upstream module is imported, compiled for execution, or modified here.
This contract covers source execution compatibility, not third-party runtimes
or authenticity of an unsigned retained audit receipt.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import textwrap
from types import MappingProxyType


PROFILE = 'm100-approved-execution-ast-v1'
CONTRACT_ID = 'm100-asgard-attachment-20261024-v1'
EXTRACT_LOGS_AST_SHA256 = '1a409e92a69e7bb71f529cdcabe815218dbe973adc56fdad627e81775f5207d8'
# inspect.getsource retains class indentation; textwrap.dedent removes it from
# the source and from multiline docstring contents. Pin this distinct complete
# runtime representation, rather than ignoring docstrings or learning hashes.
RUNTIME_EXTRACT_LOGS_AST_SHA256 = 'ca00fd425b7f8168fd7adc60c19e6100401d52139bdcd19c947f39eaa551fd1d'
CONFIG_FILE = 'general_performance/config.py'
SLIM_CONFIG_AST_SHA256 = '4d4920d05111022817610818c89c1adec8ba793b65bcc5b1bfe8f51c8fcbb11f'
APPROVED_AST_SHA256 = MappingProxyType({
    'common/common_wrapper.py': '0bb8fe267147437932b515aa525b91929e3f753fbbce4d04f009333b04d779b4',
    'common/feishu_client.py': '0bf29349a3347c54979d619d05404039939483d8736bd41fa30b95bda8fcf927',
    'common/perf_csv.py': '20153f5ec10659f3be8e46898ba3efc555c4dc1d79028331d7745bc2d37775ef',
    'common/perf_log.py': '02d62cda2cbfc739baabd2330c4e9be839a2d725ec109e3860389c6bafdd3d77',
    'common/perf_path.py': '1e385efb3aca56030729ed1e3aa9d84f1a14d667dd7edf5c08b92c66ca627bd6',
    'common/perf_plot.py': '3e15688304cf702e4e1b4951d0b3e3a978edd5e6c9461c5b263fe385b114f357',
    'common/perf_utils.py': '546e18cdb4e6a37dd5f2a4241e843860963e420668dc835eeea0af51e5572d9a',
    'common/perf_value.py': 'c768e82e1edb39c357d4066193ef6c2c927871b4bc237399d69b464c3532ee10',
    'common/secrets_manager.py': '8b418c8fa04059a8198e9924af66a68859676f69c0e7e1f310ad7783adb0cb4e',
    CONFIG_FILE: '8617131040b2c27c99a8096cd3d8e3a48391630e0c21cafd5a4a83ffee7ea948',
    'general_performance/performance.py': 'a91c2566e0f0464865ad6d10b86ba98449b523decda30da451b5c37e2f5b5754',
    'general_performance/start.py': '4602a7894a1277d9acd0a3a05452f6451fbd552a077f98c37b326e8b23cfa31c',
})


# Source-only archive variant: fixed reviewed removal of private defaults.
ARCHIVE_SANITIZED_AST_SHA256 = MappingProxyType({'common/feishu_client.py': 'f9bb523305cc76cae5fd25ecb88261b72f2de4db0837e259510b66b4d68ef6df', 'common/perf_path.py': '8669a22dc66bcd5523ce0c27b0c8f2c26b44b0df2e68956a938c26ba7e8beb1a', 'common/perf_plot.py': '1761cbe23dd0857b9a088c984ce2a30fcb50bdc668dd6e41345ff6d651f4ad48', 'general_performance/config.py': '5f0828460e686a919cf24ac2192ed72f83d73c01be132e928e1865fcaba865a5'})
ARCHIVE_SANITIZED_SLIM_CONFIG_SHA256 = '01b98aca158f7125be2ba12a5c3353e8df7684cef86bc324d2016027c10cc4c8'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def canonical_ast_dump(tree):
    """Stable full-field dump matching the approved CPython 3.11 representation.

    Newer Python adds an empty type_params field to otherwise identical
    functions/classes. Only that empty field is normalized away. Nonempty type
    parameters and every other new field remain visible and fail the contract.
    We render empty lists explicitly rather than relying on ast.dump defaults.
    Compatibility here does not replace runtime acceptance on another Python.
    """
    def render(value):
        if isinstance(value, ast.AST):
            fields = []
            for name in value._fields:
                if not hasattr(value, name):
                    continue
                item = getattr(value, name)
                if name == 'type_params' and item == []:
                    continue
                if item is None and getattr(type(value), name, ...) is None:
                    continue
                fields.append(name + '=' + render(item))
            return type(value).__name__ + '(' + ', '.join(fields) + ')'
        if isinstance(value, list):
            return '[' + ', '.join(render(item) for item in value) + ']'
        return repr(value)
    if not isinstance(tree, ast.AST):
        raise TypeError('expected an AST node')
    return render(tree)


def ast_digest(tree):
    return hashlib.sha256(canonical_ast_dump(tree).encode()).hexdigest()


_ast_digest = ast_digest


CONTRACT_FINGERPRINT = _digest({
    'profile': PROFILE, 'contract_id': CONTRACT_ID,
    'canonical_ast_sha256': dict(APPROVED_AST_SHA256),
    'extract_logs_ast_sha256': EXTRACT_LOGS_AST_SHA256,
})


def _incompatible(detail):
    return ValueError('部署分析器不符合已验执行契约，未知最新版须适配和回放验收，不能覆盖为旧源码：' + detail)


def validate_extract_logs_contract(source):
    """Require the one approved inspect/dedent method representation.

    Static module validation separately uses EXTRACT_LOGS_AST_SHA256. These
    two explicit constants differ only because dedent changes the approved
    multiline docstring's internal whitespace. Neither check strips strings.
    """
    if not isinstance(source, str):
        raise _incompatible('extract_logs 源码类型无效。')
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError, TypeError):
        raise _incompatible('extract_logs 不能进行静态 AST 校验。') from None
    if _ast_digest(tree) != RUNTIME_EXTRACT_LOGS_AST_SHA256:
        raise _incompatible('extract_logs 完整 AST 已变化（包括排序、条件和复制上下文）。')
    return {'profile': PROFILE, 'contract_id': CONTRACT_ID,
            'static_extract_logs_ast_sha256': EXTRACT_LOGS_AST_SHA256,
            'runtime_extract_logs_ast_sha256': RUNTIME_EXTRACT_LOGS_AST_SHA256}


def _plain(path, *, directory=False):
    try:
        info = path.lstat()
    except OSError:
        raise _incompatible('缺少源码路径：' + path.name) from None
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise _incompatible('源码须为普通文件和目录，拒绝链接或重解析点：' + path.name)


def _read_sources(root):
    """Read the exact executable source surface; never import upstream code."""
    root = Path(root).expanduser().absolute()
    _plain(root, directory=True)
    observed = set()
    for folder in ('common', 'general_performance'):
        directory = root / folder
        _plain(directory, directory=True)
        for base, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs:
                _plain(Path(base) / name, directory=True)
            for name in files:
                if name.endswith('.py'):
                    path = Path(base) / name
                    _plain(path)
                    observed.add(path.relative_to(root).as_posix())
    if observed != set(APPROVED_AST_SHA256):
        raise _incompatible('Python 模块集合变化；缺少或新增模块：' +
                            ', '.join(sorted(observed ^ set(APPROVED_AST_SHA256))))
    return {name: (root / name).read_bytes() for name in sorted(observed)}


def validate_analyzer_contract(root):
    """Validate original or the one approved slim snapshot without side effects.

    Both variants share the canonical fingerprint. The observed config AST and
    byte hashes remain explicit, so callers must compare the fingerprint rather
    than requiring the whole original/slim receipts to be identical.
    """
    sources = _read_sources(root)
    files = {}
    for name, raw in sources.items():
        try:
            tree = ast.parse(raw.decode('utf-8-sig'), filename=name)
        except (UnicodeError, SyntaxError, ValueError):
            raise _incompatible('源码无法静态解析：' + name) from None
        observed = _ast_digest(tree)
        canonical = APPROVED_AST_SHA256[name]
        variant = 'original'
        if name == CONFIG_FILE and observed == ARCHIVE_SANITIZED_SLIM_CONFIG_SHA256:
            variant = 'archive_sanitized_slim'
        elif observed == ARCHIVE_SANITIZED_AST_SHA256.get(name):
            variant = 'archive_sanitized'
        elif name == CONFIG_FILE and observed == SLIM_CONFIG_AST_SHA256:
            variant = 'slim_m100_ultra'
        elif observed != canonical:
            raise _incompatible('完整模块 AST 不匹配：' + name)
        if name == 'common/perf_csv.py':
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'RecordPerformance']
            methods = [node for cls in classes for node in cls.body
                       if isinstance(node, ast.FunctionDef) and node.name == 'extract_logs']
            if len(methods) != 1 or _ast_digest(ast.Module(body=methods, type_ignores=[])) != EXTRACT_LOGS_AST_SHA256:
                raise _incompatible('extract_logs 完整方法契约不匹配。')
        files[name] = {'ast_sha256': observed, 'canonical_ast_sha256': canonical,
                       'file_sha256': hashlib.sha256(raw).hexdigest(), 'variant': variant}
    receipt = {'schema_version': 1, 'status': 'compatible', 'profile': PROFILE,
               'contract_id': CONTRACT_ID, 'contract_fingerprint': CONTRACT_FINGERPRINT,
               'extract_logs_ast_sha256': EXTRACT_LOGS_AST_SHA256,
               'configuration_variant': files[CONFIG_FILE]['variant'], 'files': files}
    return validate_contract_audit(receipt)


def validate_contract_audit(receipt):
    """Check retained evidence against the fixed allowlist even after cleanup.

    This is a consistency check against approved semantic source hashes, not a
    signature. Byte hashes cannot be recomputed without original source files.
    Unknown extra diagnostic fields are retained in an independent deep copy.
    """
    if (not isinstance(receipt, dict) or type(receipt.get('schema_version')) is not int
            or receipt['schema_version'] != 1 or receipt.get('status') != 'compatible'
            or receipt.get('profile') != PROFILE or receipt.get('contract_id') != CONTRACT_ID
            or receipt.get('contract_fingerprint') != CONTRACT_FINGERPRINT
            or receipt.get('extract_logs_ast_sha256') != EXTRACT_LOGS_AST_SHA256):
        raise _incompatible('保留审计的契约标识或固定摘要不匹配。')
    files = receipt.get('files')
    if not isinstance(files, dict) or set(files) != set(APPROVED_AST_SHA256):
        raise _incompatible('保留审计的模块集合不匹配。')
    for name, canonical in APPROVED_AST_SHA256.items():
        row = files[name]
        if not isinstance(row, dict):
            raise _incompatible('保留审计的模块记录无效：' + name)
        variant = row.get('variant')
        expected = canonical
        if name == CONFIG_FILE and variant == 'archive_sanitized_slim':
            expected = ARCHIVE_SANITIZED_SLIM_CONFIG_SHA256
        elif variant == 'archive_sanitized' and name in ARCHIVE_SANITIZED_AST_SHA256:
            expected = ARCHIVE_SANITIZED_AST_SHA256[name]
        elif name == CONFIG_FILE and variant == 'slim_m100_ultra':
            expected = SLIM_CONFIG_AST_SHA256
        elif variant != 'original':
            raise _incompatible('保留审计的模块变体未知：' + name)
        if (row.get('canonical_ast_sha256') != canonical or row.get('ast_sha256') != expected
                or not isinstance(row.get('file_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', row['file_sha256'])):
            raise _incompatible('保留审计的模块规范摘要不匹配：' + name)
    if receipt.get('configuration_variant') != files[CONFIG_FILE]['variant']:
        raise _incompatible('保留审计的配置变体标记不一致。')
    return copy.deepcopy(receipt)
