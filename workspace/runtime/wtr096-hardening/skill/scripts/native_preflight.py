"""Native readiness checks before new analysis; never query cloud or send IM."""
from __future__ import annotations
import argparse
import importlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys

NATIVE_MODULES = ('numpy', 'pandas', 'matplotlib', 'seaborn', 'requests',
                  'requests_toolbelt', 'pytz', 'PIL')


class NativePreflightError(ValueError):
    """An actionable error containing no identity settings or credential output."""


def probe_native(analyzer_root, *, execution='linux', windows_git_bin=None):
    from analyzer_contract import validate_analyzer_contract
    if not (Path(analyzer_root) / 'general_performance/o.ini').is_file():
        raise ValueError('缺少原生配置 general_performance/o.ini。')
    contract = validate_analyzer_contract(Path(analyzer_root))
    windows = None
    if execution == 'windows':
        from native_windows_host import probe_windows
        windows = probe_windows({'execution':'windows','python':sys.executable,
            'analyzer_root':str(analyzer_root),'windows_git_bin':windows_git_bin})
    elif execution == 'linux':
        missing = [name for name in ('mawk', 'grep') if not shutil.which(name)]
        if missing:
            raise ValueError('缺少原生工具：' + ', '.join(missing))
    else:
        raise ValueError('原生探针只支持实际 Linux 或明确 Windows 执行。')
    versions = {}
    for name in NATIVE_MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            raise ValueError('原生依赖导入失败：' + name + ' (' + type(exc).__name__ + ')') from None
        versions[name] = str(getattr(module, '__version__', 'available'))
    result = {'status': 'native_preflight_passed', 'python': sys.version.split()[0],
            'dependencies': versions, 'source_files_present': True, 'analyzer_contract': contract,
            'network_requests': 0, 'query_sent': False}
    if windows is not None:
        result.update(execution='windows',windows_native=windows)
    return result


def _wsl_path(path):
    text = str(path)
    if text.startswith('/'):
        return text
    value = PureWindowsPath(text)
    if not re.fullmatch('[A-Za-z]:', value.drive) or not value.is_absolute():
        raise NativePreflightError('WSL 预检脚本必须是可访问的本机绝对路径。')
    return '/mnt/' + value.drive[0].lower() + '/' + '/'.join(value.parts[1:])


def check_native_environment(cfg):
    execution = cfg.get('execution', 'wsl' if os.name == 'nt' else 'linux')
    script = Path(__file__).resolve()
    arguments = [cfg['python'], '-X', 'utf8', '-B',
                 _wsl_path(script) if execution == 'wsl' else str(script)]
    if execution == 'windows':
        arguments += ['--execution','windows','--windows-git-bin',str(cfg.get('windows_git_bin') or '')]
    arguments += ['--analyzer-root', cfg['analyzer_root']]
    if execution == 'wsl':
        distribution = str(cfg.get('distribution') or '')
        if not re.fullmatch(r'[A-Za-z0-9._-]+', distribution):
            raise NativePreflightError('预检需要明确的 WSL 发行版。')
        arguments = [str(cfg.get('wsl_executable') or 'wsl.exe'), '-d', distribution,
                     '--exec', *arguments]
    elif execution == 'windows':
        from native_windows_host import validate_windows_config
        try:
            validate_windows_config(cfg)
        except ValueError as error:
            raise NativePreflightError(str(error)) from None
    elif execution != 'linux' or os.name == 'nt':
        raise NativePreflightError('Windows 原生分析必须使用 WSL；Linux 使用 linux 模式。')
    try:
        if execution == 'windows':
            from native_windows_host import execution_environment
            from native_windows_process import run_windows_tree
            completed = run_windows_tree(arguments,env=execution_environment(cfg),timeout=60)
        else:
            completed = subprocess.run(arguments, capture_output=True, text=True, encoding='utf-8', timeout=60)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        raise NativePreflightError('原生解释器无法启动或预检超时；检查 WSL、发行版和解释器路径。') from None
    try:
        result = json.loads(completed.stdout or '{}')
    except (ValueError, TypeError):
        raise NativePreflightError('原生预检未返回有效 JSON；尚未下载日志或发送版本查询。') from None
    if completed.returncode or result.get('status') != 'native_preflight_passed':
        reason = result.get('error') if isinstance(result.get('error'), str) else '检查原生解释器、源码与依赖。'
        raise NativePreflightError('原生预检失败：' + reason)
    from analyzer_contract import validate_contract_audit
    try:
        validate_contract_audit(result.get('analyzer_contract'))
    except ValueError:
        raise NativePreflightError('原生预检源码契约收据无效；请先重新核对目标源码。') from None
    if (result.get('network_requests') != 0 or result.get('query_sent') is not False
            or set(result.get('dependencies', {})) != set(NATIVE_MODULES)):
        raise NativePreflightError('原生预检收据缺少完整依赖或离线执行声明。')
    if execution == 'windows':
        from native_windows_host import validate_probe
        try:
            if result.get('execution') != 'windows':
                raise ValueError('Windows execution declaration missing')
            validate_probe(result.get('windows_native'),cfg)
        except ValueError as error:
            raise NativePreflightError('Windows 原生工具或时区探针收据无效。') from error
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analyzer-root', required=True)
    parser.add_argument('--execution',choices=('linux','windows'),default='linux')
    parser.add_argument('--windows-git-bin')
    try:
        args = parser.parse_args()
        data = probe_native(args.analyzer_root,execution=args.execution,windows_git_bin=args.windows_git_bin)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(data, ensure_ascii=False))
