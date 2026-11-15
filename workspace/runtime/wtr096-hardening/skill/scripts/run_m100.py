"""Run only m100-ultra analysis and export the Asgard-side results as HTML.

Default: read-only preflight. --execute: use a private source/input copy.
The worker intercepts native report publishing; no downstream pipeline is run.
"""
from __future__ import annotations

import argparse
import ast
import configparser
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import uuid
import zipfile

from native_selection import POLICY as SELECTION_POLICY, validate_source_map
from native_windows_host import filesystem_path

PLATFORMS = {'m100-ultra'}
DIRECTORIES = {'app', 'soc', 'OrinX', 'log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth'}
SKIP = {'perf', 'logs_traned', 'log', '__pycache__', '.git', '.agents', '.claude', '.venv'}
REQUIRED_ENV = ('FEISHU_APP_ID', 'FEISHU_APP_SECRET')
MAX_COPY_BYTES = 20 * 1024**3
MAX_EXPANDED_BYTES = 40 * 1024**3
MAX_ARCHIVE_MEMBERS = 100000
UNSUPPORTED_COMPRESSION = ('.clp','.zst','.gz','.tgz','.bz2','.tbz','.tbz2','.xz','.txz','.7z','.rar','.lz4')


def slim_platform_config(path):
    """Prune only a private config.py copy. Shared native modules stay intact."""
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'))
    removed = {'CONFIG_M100_MAX', 'CONFIG_THOR', 'CONFIG_ORIN'}
    body = []
    found = set()
    for node in tree.body:
        names = {target.id for target in node.targets if isinstance(target, ast.Name)} if isinstance(node, ast.Assign) else set()
        if names & removed:
            continue
        for name, keys in (('CONFIG_MAPS', {'m100-ultra'}), ('CONFIG_PATH', {'FSDA_M100_ULTRA', 'FSDB_M100_ULTRA'})):
            if name in names:
                if not isinstance(node.value, ast.Dict) or any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in node.value.keys):
                    raise ValueError('部署平台配置结构已变化；请适配，不能强行裁剪。')
                all_pairs=list(zip(node.value.keys,node.value.values))
                pairs = [(k,v) for k,v in all_pairs if k.value in keys]
                if {k.value for k,v in pairs} != keys:
                    raise ValueError('部署配置缺少 m100-ultra 的完整映射。')
                # Full rules may reference shared MCU/container aliases added in
                # the latest deployment. Never prune those input mappings.
                if name=='CONFIG_PATH':
                    pairs=all_pairs
                node.value.keys, node.value.values = [k for k,v in pairs], [v for k,v in pairs]
                found.add(name)
        body.append(node)
    tree.body = body
    if found != {'CONFIG_MAPS','CONFIG_PATH'} or any(isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id in removed for n in ast.walk(tree)):
        raise ValueError('单平台配置依赖不完整。')
    compile(tree,str(path),'exec')
    Path(path).write_text('# Generated only inside this isolated job: m100-ultra.\n'+ast.unparse(tree)+'\n',encoding='utf-8')


def inputs(root: Path):
    """Only files visible to the supplied analyzer's directory-based collector."""
    for directory, names, files in os.walk(filesystem_path(root)):
        if os.name == 'nt':
            directory = str(directory).removeprefix('\\\\?\\')
        for name in names:
            _check_plain(Path(directory)/name,root)
        names[:] = sorted(n for n in names if n not in SKIP)
        relative_parts = Path(directory).relative_to(root).parts
        if not DIRECTORIES.intersection(relative_parts):
            continue
        for name in sorted(files):
            path = Path(directory) / name
            _check_plain(path,root)
            if not filesystem_path(path).is_file():
                raise ValueError('输入含非普通文件。')
            if name == 'message' or name.endswith(('.log','.zip','.tar')):
                yield path
            else:
                raise ValueError('日志目录含当前完整适配器不支持的文件格式：'+path.name+'；请先受控归整为 .log/.zip/.tar。')


def _check_plain(path,root):
    io_path=filesystem_path(path)
    attributes=getattr(io_path.lstat(),'st_file_attributes',0)
    if io_path.is_symlink() or attributes & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('输入含符号链接、重解析点或目录外文件。')


def _safe_member_name(name):
    name=name.replace('\\','/')
    while name.startswith('./'):
        name=name[2:]
    parts=name.rstrip('/').split('/')
    if not name or name.startswith('/') or re.match(r'^[A-Za-z]:',name) or any(p in ('','.', '..') or '\x00' in p for p in parts):
        raise ValueError('归档成员路径不安全。')
    # Extraction is performed on Linux/WSL. Colon-containing core names are
    # valid there; Windows filename restrictions are not traversal controls.
    return PurePosixPath(name)


class _WindowsArchiveNames:
    """Read-only check before Windows extractall can sanitize or alias a name.

    Only archives selected by the original request-time predicate use this
    additional gate. No path is rewritten, no member is dropped and no extra
    file is selected. Component limits are intrinsic NTFS limits; there is no
    arbitrary whole-path cutoff based on a different staging destination.
    """
    def __init__(self):
        self.destinations = {}
        self.parents = set()

    def check(self, path, *, directory):
        for part in path.parts:
            if (any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                    or part.endswith((' ', '.'))):
                raise ValueError('Windows 归档成员含非法字符或尾点/空格；拒绝自动改名：'+str(path))
            stem = part.split('.', 1)[0].rstrip(' ').upper()
            if (stem in {'CON','PRN','AUX','NUL','CONIN$','CONOUT$','CLOCK$'}
                    or re.fullmatch(r'(?:COM|LPT)[1-9¹²³]',stem)):
                raise ValueError('Windows 归档成员使用保留设备名：'+str(path))
            if len(part.encode('utf-16-le')) // 2 > 255:
                raise ValueError('Windows 归档成员的单个文件名超过255个UTF-16单元：'+str(path))
            # On volumes with generated DOS 8.3 names, a short-name component
            # can alias a prior long member even when casefold keys differ.
            # The private extraction tree must never resolve such aliases.
            if (re.fullmatch(r'[^.]{0,6}~[0-9]+(?:\.[^.]{1,3})?',part)
                    and len(part.split('.',1)[0].encode('utf-16-le')) // 2 <= 8):
                raise ValueError('Windows 归档成员可能使用DOS 8.3短名别名：'+str(path))
        key = str(path).casefold()
        previous = self.destinations.get(key)
        if previous is not None:
            # Repeating an identical explicit directory does not alias a file.
            if directory and previous == (str(path), True):
                return
            raise ValueError('Windows 归档成员存在大小写/目标别名碰撞：'+str(path))
        parent_keys = {str(parent).casefold() for parent in path.parents if str(parent) != '.'}
        for parent in parent_keys:
            prior = self.destinations.get(parent)
            if prior is not None and not prior[1]:
                raise ValueError('Windows 归档成员存在文件与目录别名碰撞：'+str(path))
        if not directory and key in self.parents:
            raise ValueError('Windows 归档成员存在目录与文件别名碰撞：'+str(path))
        self.destinations[key] = (str(path), directory)
        self.parents.update(parent_keys)


class NativeLogSelection:
    """Evaluate full-rule prefix unions with the deployment's time predicates.

    Extract only pure selector functions from the source AST; importing the
    whole common package during preflight could initialize native log writers.
    """
    def __init__(self,analyzer,rules,start,end):
        import pytz
        self.time_range=start+'/'+end
        tz=pytz.timezone('Asia/Shanghai')
        self.epochs=tuple(tz.localize(datetime.strptime(x,'%Y%m%d%H%M%S')).timestamp() for x in (start,end))
        namespace={'datetime':datetime,'timedelta':timedelta,'re':re,'CN_TZ':tz,'Path':Path}
        extracted=[]
        required={'_parse_cn_dt_from_log_path','should_keep_log','is_file_time_in_range'}
        for relative,names in (('common/perf_utils.py',{'_parse_cn_dt_from_log_path','should_keep_log'}),
                               ('common/perf_path.py',{'is_file_time_in_range'})):
            tree=ast.parse((Path(analyzer)/relative).read_text(encoding='utf-8-sig'))
            nodes=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
            if {node.name for node in nodes}!=names:
                raise ValueError('部署源码的原生日志时间选择接口已变化。')
            if relative.endswith('perf_utils.py'):
                assignments=[node for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='_TS_RE' for t in node.targets)]
                if len(assignments)!=1:
                    raise ValueError('部署源码时间戳格式定义已变化。')
                nodes=assignments+nodes
            module=ast.Module(body=nodes,type_ignores=[])
            exec(compile(module,str(Path(analyzer)/relative),'exec'),namespace)
            extracted.append(ast.dump(module,include_attributes=False))
        config_namespace={'__builtins__':{}}
        tree=ast.parse((Path(analyzer)/'general_performance/config.py').read_text(encoding='utf-8-sig'))
        allowed=(ast.Assign,ast.Name,ast.Load,ast.Store,ast.Dict,ast.List,ast.Tuple,ast.Constant,ast.BinOp,ast.Add)
        for node in tree.body:
            if isinstance(node,ast.Assign):
                if any(not isinstance(item,allowed) for item in ast.walk(node)):
                    raise ValueError('部署 CONFIG_PATH 含动态表达式，不能证明完整日志选集。')
                exec(compile(ast.Module(body=[node],type_ignores=[]),'native-config-selection','exec'),config_namespace)
        mappings=config_namespace.get('CONFIG_PATH')
        if not isinstance(mappings,dict) or not isinstance(rules,dict) or not rules:
            raise ValueError('无法读取完整规则或部署 CONFIG_PATH。')
        prefixes=set()
        for theme,metrics in rules.items():
            if not isinstance(metrics,dict):
                raise ValueError('完整规则主题结构变化。')
            for fields in metrics.values():
                if not isinstance(fields,dict) or not isinstance(fields.get('soc_type'),str):
                    raise ValueError('完整规则缺少 soc_type，不能证明完整日志选集。')
                for soc in fields['soc_type'].split(','):
                    soc=soc.strip()
                    if not soc:
                        continue
                    values=mappings.get(soc,[soc])
                    if not isinstance(values,list) or any(not isinstance(v,str) or not v for v in values):
                        raise ValueError('部署 CONFIG_PATH 前缀结构变化。')
                    prefixes.update(values)
        self.prefixes=sorted(prefixes)
        self.parse_time=namespace['_parse_cn_dt_from_log_path']
        self.keep_log=namespace['should_keep_log']
        self.keep_archive=namespace['is_file_time_in_range']
        self.fingerprint=hashlib.sha256(json.dumps({'predicates':extracted,'prefixes':self.prefixes,'execution_policy':SELECTION_POLICY},sort_keys=True).encode()).hexdigest()

    def archive_selected(self,path):
        try:
            return self.keep_archive(path.name,self.time_range)
        except ValueError:
            return False  # The native collector applies this same exclusion.

    def selected_names(self,names,source_by_basename=None):
        import fnmatch
        names=set(names)
        if source_by_basename is None:
            # Explicit legacy flat input has one stream; no invented identity.
            source_by_basename={name:'legacy' for name in names}
        validate_source_map(source_by_basename)
        if not names.issubset(source_by_basename):
            raise ValueError('日志来源映射缺失，拒绝猜测 FSDA/FSDB。')
        chosen=set()
        for prefix in self.prefixes:
            groups={}
            for name in names:
                if fnmatch.fnmatchcase(name,prefix+'*.log'):
                    groups.setdefault(source_by_basename[name],[]).append(name)
            for matches in groups.values():
                matches.sort(key=lambda name:(lambda dt:(dt is not None,dt))(self.parse_time(Path(name))))
                state={'kept_first_after_end':False}
                boundary_time=None
                for name in matches:
                    stamp=self.parse_time(Path(name))
                    if self.keep_log(Path(name),self.epochs,state):
                        chosen.add(name)
                        if stamp is not None and stamp.timestamp()>self.epochs[1]:
                            boundary_time=stamp
                    elif boundary_time is not None and stamp==boundary_time:
                        # Safety preflight covers every possible native-order tie.
                        chosen.add(name)
        return chosen


def _input_source(path,input_root):
    parts=(path.relative_to(input_root).parts if input_root is not None else path.parts)
    found={name for name in parts[:-1] if name in
           ('log_fsdA','log_fsdB','log_xcu_mcu','log_bzcu_eth')}
    if len(found)>1:
        raise ValueError('输入路径含多个来源类别，无法确定归属。')
    return next(iter(found),'legacy')


def validate_log_inventory(files,selection=None,input_root=None,*,windows=False):
    """Read-only validation before the native collector flattens basenames.

    CLP cannot expose a trustworthy member list in the supplied implementation;
    reject it explicitly until a confined decoder has been validated. Nested
    archives are likewise rejected instead of silently skipping their logs.
    """
    if type(windows) is not bool:
        raise ValueError('Windows 归档准入模式必须明确为布尔值。')
    inventory={}
    source_current=None
    eligible_names=set()
    eligible_current=True
    total=0
    members=0
    def record(name,stream,size,origin):
        nonlocal total,members
        members+=1
        total+=size
        if members>MAX_ARCHIVE_MEMBERS or total>MAX_EXPANDED_BYTES:
            raise ValueError('归档展开数量或大小超过安全上限。')
        folded=name.casefold()
        if folded.endswith(('.zip','.tar')+UNSUPPORTED_COMPRESSION):
            raise ValueError('发现嵌套或未支持的压缩格式；请先受控解包并验证。')
        if name=='message':
            # The native pairing of message files with SOC names is implicit
            # and can overwrite another source. Require explicit normalization.
            raise ValueError('message 文件存在隐式 SOC 重命名覆盖风险；请先归整为明确命名的 .log。')
        if not name.endswith('.log'):
            if folded.endswith('.log'):
                raise ValueError('日志扩展名须归整为小写 .log，避免原生入口漏读。')
            if folded.endswith(('.bin','.elf','.dat','.dlt','.pcap','.pcapng')):
                raise ValueError('发现未解码的二进制日志，不能当作完整文本统计。')
            return
        sha=hashlib.sha256()
        actual=0
        for chunk in iter(lambda:stream.read(1024**2),b''):
            actual+=len(chunk)
            if actual>size or actual>MAX_EXPANDED_BYTES:
                raise ValueError('归档成员实际大小与声明不符。')
            sha.update(chunk)
        if actual!=size:
            raise ValueError('归档成员数据不完整。')
        key=folded
        value={'sha256':sha.hexdigest(),'bytes':actual,'origin':origin,'name':name,'eligible':eligible_current,'source':source_current}
        inventory.setdefault(key,[]).append(value)
        if eligible_current:
            eligible_names.add(name)
    for path in files:
        source_current=_input_source(path,input_root)
        eligible_current=selection is None or not path.name.endswith(('.zip','.tar')) or selection.archive_selected(path)
        if path.name.endswith('.zip'):
            with zipfile.ZipFile(filesystem_path(path)) as archive:
                destinations=set()
                windows_names=_WindowsArchiveNames() if windows and eligible_current else None
                for member in archive.infolist():
                    safe=_safe_member_name(member.filename)
                    mode=(member.external_attr>>16)&0xffff
                    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise ValueError('ZIP 含符号链接或特殊文件。')
                    if windows_names is not None:
                        windows_names.check(safe,directory=member.is_dir())
                    if member.is_dir():
                        continue
                    if member.flag_bits&1:
                        raise ValueError('不支持加密 ZIP。')
                    key=str(safe).casefold()
                    if key in destinations:
                        raise ValueError('ZIP 含重复目标路径。')
                    destinations.add(key)
                    with archive.open(member) as stream:
                        record(safe.name,stream,member.file_size,path.name+'::'+str(safe))
        elif path.name.endswith('.tar'):
            with tarfile.open(filesystem_path(path),'r:') as archive:
                destinations=set()
                windows_names=_WindowsArchiveNames() if windows and eligible_current else None
                for member in archive:
                    safe=_safe_member_name(member.name)
                    if not (member.isfile() or member.isdir()):
                        raise ValueError('TAR 含链接或特殊文件。')
                    if windows_names is not None:
                        windows_names.check(safe,directory=member.isdir())
                    if member.isdir():
                        continue
                    key=str(safe).casefold()
                    if key in destinations:
                        raise ValueError('TAR 含重复目标路径。')
                    destinations.add(key)
                    stream=archive.extractfile(member)
                    if stream is None:
                        raise ValueError('无法读取 TAR 成员。')
                    with stream:
                        record(safe.name,stream,member.size,path.name+'::'+str(safe))
        elif path.name.endswith('.log') or path.name=='message':
            with filesystem_path(path).open('rb') as stream:
                record(path.name,stream,filesystem_path(path).stat().st_size,path.name)
        else:
            raise ValueError('当前安全适配不接受该压缩格式：'+path.name)
    if not inventory:
        raise ValueError('输入中没有可供完整统计的 .log 文本文件。')
    # Compute each stream's original selection before flattening same names.
    # A late duplicate may be irrelevant in every stream; it must not block
    # valid analysis, and cannot be assigned an invented source identity.
    by_source={}
    for variants in inventory.values():
        for value in variants:
            if value['eligible']:
                by_source.setdefault(value['source'],set()).add(value['name'])
    planned=set()
    for source,names in by_source.items():
        planned.update(selection.selected_names(names,{name:source for name in names})
                       if selection is not None else names)
    source_by_basename={}
    excluded_conflicts=[]
    excluded_names=set()
    for variants in inventory.values():
        eligible=[value for value in variants if value['eligible']]
        owners={value['source'] for value in eligible}
        if len(owners)>1:
            if any(value['name'] in planned for value in eligible):
                raise ValueError('同名日志跨来源且可能进入原生实际选集，不能确定边界归属：'+eligible[0]['name'])
            for name in sorted({value['name'] for value in eligible}):
                excluded_names.add(name)
                excluded_conflicts.append({'basename':name,
                    'reason':'outside_source_aware_full_rule_selection',
                    'variants':[{key:value[key] for key in ('source','sha256','bytes','origin','name')}
                                for value in eligible]})
            continue
        if eligible:
            for value in eligible:
                source_by_basename[value['name']]=value['source']
    consumable=eligible_names-excluded_names
    selected=(selection.selected_names(consumable,source_by_basename)
              if selection is not None else consumable)
    if selected!=planned:
        raise ValueError('过滤无消费冲突后原谓词选集发生变化，拒绝修改统计输入。')
    ignored_conflicts=[]
    for key,variants in inventory.items():
        eligible=[value for value in variants if value['eligible']]
        if len({value['sha256'] for value in eligible})>1:
            if any(value['name'] in selected for value in eligible):
                raise ValueError('同名日志内容不同，原生实际选集会覆盖：'+eligible[0]['name'])
            ignored_conflicts.append({'basename':eligible[0]['name'],'reason':'outside_native_full_rule_selection',
                                      'variants':[{'sha256':v['sha256'],'bytes':v['bytes'],'origin':v['origin']} for v in eligible]})
    selected_members={}
    for name in sorted(selected):
        variants=[v for v in inventory[name.casefold()] if v['eligible'] and v['name']==name]
        pairs={(v['sha256'],v['bytes']) for v in variants}
        if len(pairs)!=1 or {v['source'] for v in variants}!={source_by_basename[name]}:
            raise ValueError('实际可消费日志没有唯一来源及内容：'+name)
        checksum,length=next(iter(pairs))
        selected_members[name]={'sha256':checksum,'bytes':length,'source':source_by_basename[name]}
    return {'log_count':len(inventory),'expanded_bytes':total,
            'selection_sha256':selection.fingerprint if selection is not None else None,
            'selection_policy':SELECTION_POLICY,'source_by_basename':source_by_basename,
            'excluded_conflicts':excluded_conflicts,
            'selected_log_count':len(selected),'selected_members':selected_members,'ignored_conflicts':ignored_conflicts,
            'basename_hashes':{k:sorted({(v['sha256'],v['bytes']) for v in variants}) for k,variants in sorted(inventory.items())}}


def preflight(analyzer: Path, logs: Path, start: str, end: str, platform: str, rules_json: Path | None = None, *, native_execution: str = 'linux', source_identity: str = 'legacy_v2'):
    if native_execution not in ('linux','windows'):
        raise ValueError('原生后端须显式为 linux 或 windows。')
    if source_identity not in ('legacy_v2','namespace_v3'):
        raise ValueError('未知日志来源身份协议。')
    if native_execution == 'windows' and rules_json is None:
        raise ValueError('Windows 原生预检须提供本次完整规则快照 --rules-json，以保持原请求时间与选集准入。')
    analyzer,logs=Path(analyzer).resolve(),Path(logs).resolve()
    if platform not in PLATFORMS:
        raise ValueError('不支持的平台。')
    if len(start) != 14 or len(end) != 14 or not start.isdigit() or not end.isdigit():
        raise ValueError('时间须为 14 位 YYYYMMDDHHMMSS。')
    if datetime.strptime(start, '%Y%m%d%H%M%S') >= datetime.strptime(end, '%Y%m%d%H%M%S'):
        raise ValueError('开始时间必须早于结束时间。')
    from analyzer_contract import validate_analyzer_contract
    source_contract=validate_analyzer_contract(analyzer)
    for relative in ('general_performance/start.py', 'general_performance/o.ini', 'common/perf_path.py'):
        if not (analyzer / relative).is_file():
            raise ValueError(f'部署源码缺少 {relative}；请先核对最新入口契约。')
    for folder in ('common', 'general_performance'):
        for path in (analyzer/folder).rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(analyzer):
                raise ValueError('分析源码含链接到其他位置的文件；请先提供普通文件形式的源码快照。')
    if not logs.is_dir():
        raise ValueError('日志目录不存在。')
    files = list(inputs(logs))
    if not files:
        raise ValueError('未发现指定子目录中的日志；不会启动空数据分析。')
    size = sum(filesystem_path(p).stat().st_size for p in files)
    if size > MAX_COPY_BYTES:
        raise ValueError('输入超过本试验的 20 GiB 复制上限；请先缩小到一个验证窗口。')
    selection=(NativeLogSelection(analyzer,json.loads(Path(rules_json).read_text(encoding='utf-8-sig')),start,end)
               if rules_json is not None else None)
    if source_identity == 'namespace_v3':
        if selection is None:
            raise ValueError('来源目录协议需要完整规则快照。')
        from source_catalog import build_catalog
        inventory=build_catalog(files,selection,logs,windows=native_execution=='windows')
    else:
        inventory=validate_log_inventory(files,selection,logs,windows=native_execution=='windows')
    result = {'status': 'preflight_ok', 'platform': platform,
              'time_range': f'{start}/{end}', 'selected_files': len(files),
              'copy_bytes': size, 'needs_clp': False,'input_inventory':inventory,
              'python': sys.version.split()[0], 'system': sys.platform,'source_identity':source_identity,
              'analyzer_contract':source_contract,
              'upstream_source_sha256': {}}
    for folder in ('common', 'general_performance'):
        for path in sorted((analyzer/folder).rglob('*.py')):
            if not path.is_symlink():
                result['upstream_source_sha256'][path.relative_to(analyzer).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result, files


def execute(analyzer: Path, logs: Path, work: Path, result: dict, files: list[Path], timeout: int, expected_vin: str | None = None, rules_json: Path | None = None, *, native_execution: str = 'linux'):
    analyzer,logs,work=Path(analyzer).resolve(),Path(logs).resolve(),Path(work).resolve()
    if native_execution not in ('linux','windows') or native_execution == 'windows' and os.name != 'nt':
        raise ValueError('Windows 原生执行须显式选择并运行在 Windows；不自动切换后端。')
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)] if rules_json is None else []
    if missing:
        raise ValueError('读取在线统计规则前须显式配置应用身份：' + ', '.join(missing))
    if not expected_vin:
        raise ValueError('执行时须提供 --expected-vin，以核对实际分析的车辆。')
    if work == logs or work.is_relative_to(logs) or work == analyzer or work.is_relative_to(analyzer):
        raise ValueError('work-root 必须放在日志目录和部署源码目录之外。')
    work.mkdir(parents=True, exist_ok=True)
    # Native filenames are preserved. A shorter Windows job prefix avoids the
    # extra directory length crossing Win32's limit inside the original code.
    job = work/(('w-'+uuid.uuid4().hex[:12]) if native_execution == 'windows' else
               ('local-perf-trial-'+uuid.uuid4().hex))
    job.mkdir(exist_ok=False)
    result.update(status='prepared', job_dir=str(job), final_delivery_verified=False)
    manifest = job/'trial-result.json'
    try:
        snapshot = job/'analyzer'
        for folder in ('common', 'general_performance'):
            shutil.copytree(analyzer/folder, snapshot/folder,
                            ignore=shutil.ignore_patterns('secrets.json', 'settings.json', '.env', '.env.*', '__pycache__', '*.pyc', 'log', 'perf'))
        slim_platform_config(snapshot/'general_performance/config.py')
        from analyzer_contract import validate_analyzer_contract
        snapshot_contract=validate_analyzer_contract(snapshot)
        if snapshot_contract['contract_fingerprint']!=result['analyzer_contract']['contract_fingerprint']:
            raise ValueError('隔离源码副本执行契约与预检不一致。')
        local_logs = job/'input'
        for source in files:
            destination = local_logs/source.relative_to(logs)
            filesystem_path(destination.parent).mkdir(parents=True, exist_ok=True)
            shutil.copy2(filesystem_path(source), filesystem_path(destination))
        selection=(NativeLogSelection(snapshot,json.loads(Path(rules_json).read_text(encoding='utf-8-sig')),*result['time_range'].split('/'))
                   if rules_json is not None and (result.get('source_identity') == 'namespace_v3' or
                       result.get('input_inventory',{}).get('selection_sha256') is not None) else None)
        if result.get('source_identity') == 'namespace_v3':
            from source_catalog import build_catalog
            copied_inventory=build_catalog(list(inputs(local_logs)),selection,local_logs,
                                          windows=native_execution=='windows')
        else:
            copied_inventory=validate_log_inventory(list(inputs(local_logs)),selection,local_logs,
                                                  windows=native_execution=='windows')
        if copied_inventory!=result.get('input_inventory'):
            raise ValueError('复制后日志内容或归档清单变化，拒绝使用不一致的任务输入。')
        selection_manifest=job/'source-selection.json'
        selection_payload={'policy':copied_inventory['selection_policy'],'input_inventory':copied_inventory}
        if result.get('source_identity') != 'namespace_v3':
            selection_payload['source_by_basename']=copied_inventory['source_by_basename']
        selection_manifest.write_text(json.dumps(selection_payload,
            ensure_ascii=False,sort_keys=True,indent=2),encoding='utf-8')
        result['source_selection_policy']=copied_inventory['selection_policy']
        ini = snapshot/'general_performance/o.ini' 
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(ini, encoding='utf-8')
        # Preserve the latest deployment's themes and other parameters.
        for key in list(config['PARAMETER']):
            if key.lower() == 'upload':
                config['PARAMETER'][key] = '0'
        config['PARAMETER']['upload'] = '0'
        config['PARAMETER']['theme'] = 'summary'
        with ini.open('w', encoding='utf-8') as handle:
            config.write(handle)
        env = os.environ.copy()
        env.update(TZ='CST-8' if native_execution == 'windows' else 'Asia/Shanghai',
                   MPLBACKEND='Agg', PYTHONUTF8='1')
        result['command'] = [sys.executable, str(Path(__file__).with_name('m100_attachment_worker.py')),
                             '--source-root', str(snapshot), '--log-dir', str(local_logs),
                             '--time-range', result['time_range'], '--expected-vin', expected_vin,
                             '--output', str(job/'report.html'),
                             '--selection-manifest',str(selection_manifest),
                             '--native-execution',native_execution]
        if rules_json:
            result['command'] += ['--rules-json', str(rules_json.resolve())]
        with (job/'upstream.log').open('w', encoding='utf-8') as output:
            if native_execution == 'windows':
                from native_windows_process import run_windows_tree
                try:
                    completed = run_windows_tree(result['command'], env=env, timeout=timeout,
                        cwd=snapshot/'general_performance')
                except subprocess.TimeoutExpired as exc:
                    output.write(exc.stdout or '')
                    output.write(exc.stderr or '')
                    raise RuntimeError('试验超时；停止本次 Windows 分析进程树，不自动重新分析。') from None
                output.write(completed.stdout or '')
                output.write(completed.stderr or '')
                returncode = completed.returncode
            else:
                process = subprocess.Popen(result['command'], cwd=snapshot/'general_performance', env=env,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=(os.name == 'posix'))
                try:
                    process.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    if os.name == 'posix':
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait()
                    raise RuntimeError('试验超时或中断；停止当前进程组，不自动重新分析。') from None
                returncode = process.returncode
        result['upstream_exit_code'] = returncode
        if returncode:
            raise RuntimeError('上游进程失败；查看任务目录中的 upstream.log。')
        if expected_vin:
            generated_config = configparser.ConfigParser()
            generated_config.read(ini, encoding='utf-8')
            if generated_config.get('PARAMETER', 'vin', fallback='').strip().upper() != expected_vin.upper():
                raise RuntimeError('分析器识别的 VIN 与机器人请求不一致或缺失；拒绝关联结果。')
        artifact = job/'report.html'
        details = job/'report-data.json'
        if not artifact.is_file() or not details.is_file():
            raise RuntimeError('进程退出 0，但没有本次 HTML 及统计结果；拒绝标记成功。')
        statistics = json.loads(details.read_text(encoding='utf-8'))
        if statistics.get('status') not in ('ok','partial'):
            raise RuntimeError('没有可用的阿斯加德侧统计结果。')
        result.update(status=statistics['status'], artifact={'path':str(artifact),'content_type':'text/html',
                      'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}, metrics=statistics['metrics'],
                      platform_scope='m100-ultra', next_check='本地统计附件已生成；交给原机器人后续处理。')
        return result
    except Exception as exc:
        result.update(status='failed', error=str(exc))
        raise RuntimeError(f'{exc}；任务记录：{manifest}') from exc
    finally:
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--analyzer-root', type=Path, required=True)
    parser.add_argument('--log-dir', type=Path, required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--platform', choices=sorted(PLATFORMS), default='m100-ultra')
    parser.add_argument('--native-execution', choices=('linux','windows'), default='linux',
                        help='显式原生后端；WSL 内仍使用 linux。')
    parser.add_argument('--source-identity',choices=('legacy_v2','namespace_v3'),default='legacy_v2')
    parser.add_argument('--expected-vin', help='机器人调用时必须指定；校验分析器写入的 VIN。')
    parser.add_argument('--rules-json', type=Path, help='可选：部署机导出的同版本统计规则快照；提供后不访问飞书。')
    parser.add_argument('--work-root', type=Path)
    parser.add_argument('--timeout', type=int, default=7200)
    parser.add_argument('--execute', action='store_true', help='运行统计并生成本地 HTML；不发布飞书报告。')
    args = parser.parse_args()
    phase = 'input_validation'
    try:
        analyzer, logs = args.analyzer_root.resolve(), args.log_dir.resolve()
        result, selected = preflight(analyzer, logs, args.start, args.end, args.platform,args.rules_json,
                                    native_execution=args.native_execution,source_identity=args.source_identity)
        if args.execute:
            if args.work_root is None or args.timeout <= 0:
                raise ValueError('--execute 须指定 --work-root，且 timeout > 0。')
            phase = 'native_execution'
            result = execute(analyzer, logs, args.work_root.resolve(), result, selected, args.timeout,
                             args.expected_vin, args.rules_json, native_execution=args.native_execution)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') == 'partial' else 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        if args.source_identity == 'namespace_v3':
            from native_analysis_failure import failure
            print(json.dumps({'status':'failed','failure':failure(phase)},ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
