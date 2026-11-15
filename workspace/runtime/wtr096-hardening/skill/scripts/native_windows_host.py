"""Explicit Windows native configuration, offline probe and cache binding.

Existing WSL/Linux defaults remain owned by their original callers. This helper
never chooses Windows automatically, rewrites settings, downloads tools, reads
identity storage, imports the external analyzer, or publishes an artifact.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import struct
import subprocess
import sys
import time

POLICY = 'windows-native-host-v1'
WINDOWS_ANALYSIS_PROFILE = 'm100-full-native-audited-v12-windows-native'
NON_WINDOWS_ANALYSIS_PROFILE = 'm100-full-native-audited-v8-native-sheets'
ADAPTER_POLICY = 'windows-git-gawk-private-v3'
BRACKET_INPUT_KEYS = frozenset(('finish', 'pc ts', 'pc_size', 'denoise'))
TOOL_VERSION_PREFIXES = {'gawk': 'GNU Awk 5.3.2', 'grep': 'grep (GNU grep) 3.0'}


class WindowsHostError(ValueError):
    pass


def filesystem_path(path):
    """Use extended Win32 names only at Python filesystem I/O boundaries.

    Logical paths and subprocess arguments retain their original spelling.
    The Git tools accept ordinary long paths but do not accept this prefix.
    Callers retain their own containment and no-link checks.
    """
    value = os.fspath(path)
    if os.name != 'nt':
        return Path(value)
    if value.startswith('\\\\?\\'):
        value = value[4:]
    windows = PureWindowsPath(value)
    if (not windows.is_absolute() or not re.fullmatch(r'[A-Za-z]:', windows.drive)
            or '..' in windows.parts or any(c in value for c in ('\x00', '\n', '\r'))):
        raise WindowsHostError('Windows filesystem I/O requires a local absolute path without traversal.')
    return Path('\\\\?\\' + str(windows))


def analysis_profile(cfg=None):
    return WINDOWS_ANALYSIS_PROFILE if cfg and cfg.get('execution') == 'windows' else NON_WINDOWS_ANALYSIS_PROFILE


def sha(path):
    value = hashlib.sha256()
    with filesystem_path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024**2), b''):
            value.update(chunk)
    return value.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _no_links(path):
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise WindowsHostError('Windows native paths cannot contain links or reparse points.')


def _local_path(value, *, directory):
    if not isinstance(value, str) or not value or any(c in value for c in ('\x00', '\n', '\r', '"')):
        raise WindowsHostError('Windows native configuration needs explicit local absolute paths.')
    windows = PureWindowsPath(value)
    if (not windows.is_absolute() or not re.fullmatch(r'[A-Za-z]:', windows.drive)
            or '..' in windows.parts or len(windows.parts) < 2):
        raise WindowsHostError('Windows native paths must be local drive absolute paths, not UNC or relative paths.')
    path = Path(value)
    _no_links(path)
    path = path.resolve(strict=True)
    if directory and (not path.is_dir() or path.parent == path) or not directory and not path.is_file():
        raise WindowsHostError('Configured Windows native file or directory is missing.')
    return path


def validate_windows_config(cfg):
    if os.name != 'nt' or cfg.get('execution') != 'windows':
        raise WindowsHostError('Explicit windows execution requires a Windows host.')
    try:
        python = _local_path(cfg.get('python'), directory=False)
        analyzer = _local_path(cfg.get('analyzer_root'), directory=True)
        git = _local_path(cfg.get('windows_git_bin'), directory=True)
        if python.suffix.casefold() != '.exe':
            raise WindowsHostError('Windows native python must be an actual .exe, not a shell command.')
        for name in ('gawk', 'grep'):
            _local_path(str(git / (name + '.exe')), directory=False)
    except (OSError, ValueError) as error:
        if isinstance(error, WindowsHostError):
            raise
        raise WindowsHostError('Windows native paths or tools are unavailable.') from None
    return {'python': str(python), 'analyzer_root': str(analyzer), 'windows_git_bin': str(git)}


def _adapter_path():
    return Path(__file__).with_name('windows_native_tools.py')


def runtime_binding(cfg):
    paths = validate_windows_config(cfg)
    adapter = _adapter_path()
    _no_links(adapter)
    try:
        tree = ast.parse(adapter.read_text('utf-8-sig'))
        values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(n, ast.Name) and n.id == 'POLICY' for n in node.targets)
                  and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    except (OSError, ValueError, SyntaxError):
        raise WindowsHostError('The managed Windows tool adapter is unavailable or invalid.') from None
    if values != [ADAPTER_POLICY]:
        raise WindowsHostError('The managed Windows tool adapter policy is not approved for this host interface.')
    result = {'policy': POLICY, 'analysis_profile': WINDOWS_ANALYSIS_PROFILE,
        'execution': 'windows', 'python': {'path': paths['python'], 'sha256': sha(paths['python'])},
        'adapter': {'policy': ADAPTER_POLICY, 'sha256': sha(adapter)},
        'process_isolation': {'policy': 'windows-analysis-job-object-v1',
            'sha256': sha(Path(__file__).with_name('native_windows_process.py'))},
        'tools': {name: {'path': str(Path(paths['windows_git_bin']) / (name + '.exe')),
                         'sha256': sha(Path(paths['windows_git_bin']) / (name + '.exe'))}
                  for name in ('gawk', 'grep')},
        'timezone': 'CST-8', 'locale': 'C'}
    result['binding_sha256'] = _digest(result)
    return result


def execution_environment(cfg, parent=None):
    paths = validate_windows_config(cfg)
    environment = dict(os.environ if parent is None else parent)
    environment.update(TZ='CST-8', LC_ALL='C', LANG='C', PYTHONUTF8='1',
        PYTHONIOENCODING='utf-8', MPLBACKEND='Agg',
        WALKTHROUGH_WINDOWS_GIT_BIN=paths['windows_git_bin'])
    return environment


def validate_binding(receipt, cfg):
    expected = runtime_binding(cfg)
    if receipt != expected:
        raise WindowsHostError('Windows cached interpreter, tools, adapter or process isolation differs from current configuration.')
    return expected


def _tool_run(argv, *, environment, input=None):
    try:
        result = subprocess.run(argv, input=input, capture_output=True, text=True,
            encoding='utf-8', env=environment, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise WindowsHostError('A configured Windows native tool cannot complete its offline probe.') from None
    return result


def _version_matches(name, value):
    prefix = TOOL_VERSION_PREFIXES[name]
    return isinstance(value, str) and (value == prefix or value.startswith(prefix + ','))


def probe_windows(cfg):
    """Run in the actual configured analysis interpreter, not the host proxy."""
    if sys.version_info[:2] != (3, 11) or struct.calcsize('P') != 8:
        raise WindowsHostError('Windows native analysis requires CPython 3.11 x86-64.')
    binding = runtime_binding(cfg)
    if Path(sys.executable).resolve() != Path(binding['python']['path']).resolve():
        raise WindowsHostError('Native preflight is not running in the configured Windows interpreter.')
    environment = execution_environment(cfg)
    versions = {}
    for name, info in binding['tools'].items():
        result = _tool_run([info['path'], '--version'], environment=environment)
        first = result.stdout.splitlines()[0] if result.stdout else ''
        if result.returncode or not _version_matches(name, first):
            raise WindowsHostError('Configured ' + name + ' version is outside the tested Windows tool family.')
        versions[name] = first
    expected_epoch = int(datetime(2026, 10, 23, 15, 42, tzinfo=timezone(timedelta(hours=8))).timestamp())
    program = 'BEGIN { printf "%.0f %.0f\\n", mktime("2026 10 23 15 42 00"), strtonum("0x35") }'
    awk = _tool_run([binding['tools']['gawk']['path'], program], environment=environment)
    grep = _tool_run([binding['tools']['grep']['path'], '-F', 'native-probe'],
                     environment=environment, input='other\nnative-probe\n')
    crt_epoch = int(time.mktime(time.strptime('20261023154200', '%Y%m%d%H%M%S')))
    if (awk.returncode or awk.stdout.strip() != str(expected_epoch) + ' 53'
            or grep.returncode or grep.stdout.replace('\r\n', '\n') != 'native-probe\n'
            or crt_epoch != expected_epoch or os.environ.get('TZ') != 'CST-8'):
        raise WindowsHostError('Windows Python/Git timezone, hexadecimal or grep probe differs from the fixed +08:00 oracle.')
    validate_binding(binding, cfg)  # Refuse changes during the probe.
    return {'policy': POLICY, 'binding': binding, 'versions': versions,
            'python_crt_epoch': crt_epoch, 'gawk_epoch': expected_epoch,
            'hexadecimal_value': 53, 'grep_exact': True, 'self_test_passed': True,
            'network_requests': 0}


def validate_probe(receipt, cfg):
    if (not isinstance(receipt, dict) or receipt.get('policy') != POLICY
            or receipt.get('self_test_passed') is not True or receipt.get('network_requests') != 0
            or receipt.get('grep_exact') is not True or receipt.get('hexadecimal_value') != 53
            or receipt.get('python_crt_epoch') != 1792741320 or receipt.get('gawk_epoch') != 1792741320
            or set(receipt.get('versions', {})) != set(TOOL_VERSION_PREFIXES)):
        raise WindowsHostError('Windows native preflight lacks an exact offline execution receipt.')
    if any(not _version_matches(name, receipt['versions'][name]) for name in TOOL_VERSION_PREFIXES):
        raise WindowsHostError('Windows native preflight tool versions differ.')
    validate_binding(receipt.get('binding'), cfg)
    return receipt


def validate_parsing_scheduler(scheduling,pools):
    if (not isinstance(scheduling,dict) or set(scheduling)!={'policy','cpu_count','concurrent_windows',
            'max_workers_per_window','requested_workers'}
            or scheduling.get('policy')!='windows-native-bounded-parsing-v1'
            or scheduling.get('concurrent_windows')!=2):
        raise WindowsHostError('Windows parsing scheduler receipt is invalid.')
    cpu=scheduling.get('cpu_count')
    if cpu is not None and (type(cpu) is not int or cpu<1):
        raise WindowsHostError('Windows parsing scheduler CPU budget is invalid.')
    workers=max(1,min(4,(2 if cpu is None else cpu)//4))
    requested=scheduling.get('requested_workers')
    if (type(scheduling.get('max_workers_per_window')) is not int
            or scheduling['max_workers_per_window']!=workers
            or not isinstance(requested,list) or not requested
            or any(type(count) is not int or count!=workers for count in requested)
            or not isinstance(pools,list) or len(pools)!=len(requested)):
        raise WindowsHostError('Windows parsing scheduler exceeded its finite worker budget.')
    for count,pool in zip(requested,pools):
        if (not isinstance(pool,dict) or pool.get('start_method')!='spawn'
                or type(pool.get('requested_max_workers')) is not int
                or type(pool.get('actual_max_workers')) is not int
                or pool['requested_max_workers']!=count or pool['actual_max_workers']!=count):
            raise WindowsHostError('Windows parsing scheduler did not match actual spawned workers.')
    return scheduling


def validate_execution_audit(receipt, binding):
    if not isinstance(receipt, dict) or receipt.get('policy') != ADAPTER_POLICY:
        raise WindowsHostError('Windows analysis lacks its actual adapter execution audit.')
    records = receipt.get('records')
    if not isinstance(records, list) or not records:
        raise WindowsHostError('Windows analysis has no actual mapped tool calls.')
    counts = {'mawk': 0, 'grep': 0}
    for row in records:
        logical = row.get('logical_tool') if isinstance(row, dict) else None
        if logical not in counts:
            raise WindowsHostError('Windows analysis audit contains an unexpected tool.')
        counts[logical] += 1
        expected = binding['tools']['gawk' if logical == 'mawk' else 'grep']
        if (row.get('policy') != ADAPTER_POLICY or row.get('errors')
                or row.get('timezone') != 'CST-8' or row.get('locale') != 'C'
                or row.get('executable_sha256') != expected['sha256']
                or Path(row.get('executable', '')).resolve() != Path(expected['path']).resolve()
                or type(row.get('returncode')) is not int
                or row['returncode'] not in ((0,) if logical == 'mawk' else (0, 1))):
            raise WindowsHostError('Windows analysis consumed a different tool or failed invocation.')
        if logical == 'mawk':
            brackets = row.get('explicit_dynamic_regex_bracket_inputs')
            if (not isinstance(brackets, list) or any(not isinstance(x, str) for x in brackets)
                    or len(set(brackets)) != len(brackets) or not set(brackets).issubset(BRACKET_INPUT_KEYS)
                    or row.get('reversible_translation_verified') is not True
                    or row.get('only_default_symbol_renamed') is not (not bool(brackets))
                    or any(not re.fullmatch(r'[0-9a-f]{64}', str(row.get(k, '')))
                           for k in ('original_program_sha256', 'actual_program_sha256'))):
                raise WindowsHostError('Windows analysis lacks the exact reversible program adaptation audit.')
    if (not counts['mawk'] or receipt.get('actual_gawk_calls') != counts['mawk']
            or receipt.get('actual_grep_calls') != counts['grep']):
        raise WindowsHostError('Windows analysis tool counts do not match actual calls.')
    return receipt
