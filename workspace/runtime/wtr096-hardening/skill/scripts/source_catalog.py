"""Verified, source-preserving native log identities and confined staging.

An original basename is an input to the native filename predicates, never an
object identity. Logical duplicates within one source retain every archive
origin but are consumed once. Different sources are always independent.
"""
from __future__ import annotations

from contextlib import contextmanager
import fnmatch
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zipfile

from native_windows_host import filesystem_path

VERSION = 'source-catalog-v1'
POLICY = 'source-namespace-v3'
MATERIALIZATION_VERSION = 'source-materialization-v1'
SOURCES = frozenset(('log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth', 'legacy'))
MAX_INPUT_BYTES = 20 * 1024**3
MAX_EXPANDED_BYTES = 40 * 1024**3
MAX_ARCHIVE_MEMBERS = 100000
UNSUPPORTED_COMPRESSION = ('.clp', '.zst', '.gz', '.tgz', '.bz2', '.tbz', '.tbz2', '.xz', '.txz', '.7z', '.rar', '.lz4')
_HEX = re.compile(r'[0-9a-f]{64}\Z')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _integer(value, *, maximum=None):
    return type(value) is int and value >= 0 and (maximum is None or value <= maximum)


def _checksum(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _safe_member_name(name):
    if type(name) is not str:
        raise ValueError('归档成员路径不安全。')
    name = name.replace('\\', '/')
    while name.startswith('./'):
        name = name[2:]
    parts = name.rstrip('/').split('/')
    if (not name or name.startswith('/') or re.match(r'^[A-Za-z]:', name)
            or any(part in ('', '.', '..') or '\x00' in part for part in parts)):
        raise ValueError('归档成员路径不安全。')
    return PurePosixPath(name)


class _WindowsNames:
    """Reject extraction aliases without sanitizing any original component."""
    def __init__(self):
        self.destinations = {}
        self.parents = set()

    def check(self, path, *, directory):
        for part in path.parts:
            if (any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                    or part.endswith((' ', '.'))):
                raise ValueError('Windows 归档成员含非法字符或尾点/空格。')
            stem = part.split('.', 1)[0].rstrip(' ').upper()
            if (stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$'}
                    or re.fullmatch(r'(?:COM|LPT)[1-9¹²³]', stem)):
                raise ValueError('Windows 归档成员使用保留设备名。')
            if len(part.encode('utf-16-le')) // 2 > 255:
                raise ValueError('Windows 归档成员文件名超过255个UTF-16单元。')
            if (re.fullmatch(r'[^.]{0,6}~[0-9]+(?:\.[^.]{1,3})?', part)
                    and len(part.split('.', 1)[0].encode('utf-16-le')) // 2 <= 8):
                raise ValueError('Windows 归档成员可能使用DOS 8.3短名别名。')
        key = str(path).casefold()
        prior = self.destinations.get(key)
        if prior is not None:
            if directory and prior == (str(path), True):
                return
            raise ValueError('Windows 归档成员存在大小写/目标别名碰撞。')
        parents = {str(parent).casefold() for parent in path.parents if str(parent) != '.'}
        if any(parent in self.destinations and not self.destinations[parent][1] for parent in parents):
            raise ValueError('Windows 归档成员存在文件与目录别名碰撞。')
        if not directory and key in self.parents:
            raise ValueError('Windows 归档成员存在目录与文件别名碰撞。')
        self.destinations[key] = (str(path), directory)
        self.parents.update(parents)


def _plain(path, root):
    """Check every component, including the root, without following a link."""
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('输入路径位于受控目录之外。') from None
    cursor = root
    for part in (None,) + relative.parts:
        if part is not None:
            cursor = cursor / part
        io = filesystem_path(cursor)
        attributes = getattr(io.lstat(), 'st_file_attributes', 0)
        if io.is_symlink() or attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('输入含符号链接或重解析点。')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('输入路径位于受控目录之外。')


def _file_hash(path, *, maximum=MAX_INPUT_BYTES):
    checksum, length = hashlib.sha256(), 0
    with filesystem_path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            length += len(chunk)
            if length > maximum:
                raise ValueError('文件大小超过安全上限。')
            checksum.update(chunk)
    return checksum.hexdigest(), length


def _source(relative):
    found = set(relative.parts[:-1]) & (SOURCES - {'legacy'})
    if len(found) > 1:
        raise ValueError('输入路径含多个来源类别，无法确定归属。')
    return next(iter(found), 'legacy')


def _artifact_id(value):
    return _digest({key: value[key] for key in ('source', 'relative_path', 'sha256', 'bytes', 'kind')})


def _member_id(value):
    return _digest({key: value[key] for key in ('source', 'basename', 'sha256', 'bytes')})


def _hash_stream(stream, size, destination=None):
    checksum, actual = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(1024**2), b''):
        actual += len(chunk)
        if actual > size or actual > MAX_EXPANDED_BYTES:
            raise ValueError('归档成员实际大小与声明不符。')
        checksum.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if actual != size:
        raise ValueError('归档成员数据不完整。')
    return checksum.hexdigest(), actual


def _log_kind(name):
    folded = name.casefold()
    if folded.endswith(('.zip', '.tar') + UNSUPPORTED_COMPRESSION):
        raise ValueError('发现嵌套或未支持的压缩格式。')
    if name == 'message':
        raise ValueError('message 文件须先归整为明确命名的 .log。')
    if not name.endswith('.log'):
        if folded.endswith('.log'):
            raise ValueError('日志扩展名须归整为小写 .log。')
        if folded.endswith(('.bin', '.elf', '.dat', '.dlt', '.pcap', '.pcapng')):
            raise ValueError('发现未解码的二进制日志。')
        return False
    return True


@contextmanager
def _entries(path, kind, *, windows):
    """Yield (safe full name, exact archive name, length, opener) for files."""
    if kind == 'log':
        yield [(path.name, path.name, filesystem_path(path).stat().st_size, lambda: filesystem_path(path).open('rb'))]
        return
    with (zipfile.ZipFile(filesystem_path(path)) if kind == 'zip'
          else tarfile.open(filesystem_path(path), 'r:')) as archive:
        destinations = set()
        names = _WindowsNames() if windows else None
        result = []
        for entry in archive.infolist() if kind == 'zip' else archive:
            # ZipInfo.filename can normalize platform separators or truncate a
            # NUL. orig_filename retains the decoded central-directory name.
            original_name = entry.orig_filename if kind == 'zip' else entry.name
            safe = _safe_member_name(original_name)
            directory = entry.is_dir() if kind == 'zip' else entry.isdir()
            if kind == 'zip':
                mode = (entry.external_attr >> 16) & 0xffff
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('ZIP 含符号链接或特殊文件。')
            elif not (entry.isfile() or directory):
                raise ValueError('TAR 含链接或特殊文件。')
            if names is not None:
                names.check(safe, directory=directory)
            if directory:
                continue
            if kind == 'zip' and entry.flag_bits & 1:
                raise ValueError('不支持加密 ZIP。')
            key = str(safe).casefold()
            if key in destinations:
                raise ValueError('归档含重复目标路径。')
            destinations.add(key)
            size = entry.file_size if kind == 'zip' else entry.size
            if not _integer(size, maximum=MAX_EXPANDED_BYTES):
                raise ValueError('归档成员大小超过安全上限。')
            opener = (lambda entry=entry: archive.open(entry)) if kind == 'zip' else (lambda entry=entry: archive.extractfile(entry))
            result.append((str(safe), original_name, size, opener))
            if len(result) > MAX_ARCHIVE_MEMBERS:
                raise ValueError('归档展开数量超过安全上限。')
        yield result


def _selection_record(selection):
    if selection is None:
        return None
    return {'fingerprint': selection.fingerprint, 'time_range': selection.time_range,
            'epochs': list(selection.epochs), 'prefixes': list(selection.prefixes)}


def _selected(members, selection, windows):
    eligible = [(key, value) for key, value in members.items() if value['eligible']]
    if selection is None:
        selected = {key for key, value in eligible}
    else:
        selected = set()
        for prefix in selection.prefixes:
            groups = {}
            for key, value in eligible:
                name, pattern = value['basename'], prefix + '*.log'
                matches = fnmatch.fnmatchcase(name.casefold(), pattern.casefold()) if windows else fnmatch.fnmatchcase(name, pattern)
                if matches:
                    groups.setdefault(value['source'], []).append((key, value))
            for values in groups.values():
                values.sort(key=lambda row: (lambda dt: (dt is not None, dt, row[1]['ordinal']))(selection.parse_time(Path(row[1]['basename']))))
                state = {'kept_first_after_end': False}
                for key, value in values:
                    if selection.keep_log(Path(value['basename']), selection.epochs, state):
                        selected.add(key)
    conflicts = {}
    for key, value in eligible:
        conflicts.setdefault((value['source'], value['basename'].casefold()), []).append(key)
    for keys in conflicts.values():
        if len(keys) > 1 and selected.intersection(keys):
            raise ValueError('同一来源同名日志内容或大小写不同且进入原生实际选集，拒绝覆盖或重复计数。')
    return sorted(selected, key=lambda key: members[key]['ordinal'])


def build_catalog(files, selection, input_root, windows=False):
    """Read all admitted inputs; preserve identities, not flat filenames.

    ``selection`` supplies the original pure archive/parser/keep predicates.
    No import of the run_m100 CLI or its classes is needed.
    """
    if type(windows) is not bool:
        raise ValueError('Windows 归档准入模式必须明确为布尔值。')
    root = Path(input_root).absolute()
    _plain(root, root)
    paths = [Path(path).absolute() for path in files]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError('原生日志输入为空或存在重复文件路径。')
    paths.sort(key=lambda path: path.relative_to(root).as_posix())
    artifacts, members = {}, {}
    expanded, count, input_bytes = 0, 0, 0
    for ordinal, path in enumerate(paths):
        _plain(path, root)
        if not filesystem_path(path).is_file():
            raise ValueError('输入含非普通文件。')
        relative = path.relative_to(root)
        safe_relative = _safe_member_name(relative.as_posix())
        source = _source(safe_relative)
        suffix = path.suffix
        if suffix not in ('.log', '.zip', '.tar'):
            raise ValueError('当前安全适配仅接受 .log/.zip/.tar。')
        kind = suffix[1:]
        checksum, length = _file_hash(path)
        input_bytes += length
        if input_bytes > MAX_INPUT_BYTES:
            raise ValueError('输入文件总大小超过安全上限。')
        admitted = kind == 'log' or selection is None or selection.archive_selected(path)
        if type(admitted) is not bool:
            raise ValueError('原生归档准入谓词未返回布尔值。')
        artifact = {'source': source, 'relative_path': str(safe_relative), 'sha256': checksum,
                    'bytes': length, 'kind': kind, 'eligible': admitted, 'ordinal': ordinal}
        artifact_id = _artifact_id(artifact)
        artifacts[artifact_id] = artifact
        with _entries(path, kind, windows=windows and admitted) as entries:
            for member_path, original_name, size, opener in entries:
                count += 1
                expanded += size
                if count > MAX_ARCHIVE_MEMBERS or expanded > MAX_EXPANDED_BYTES:
                    raise ValueError('归档展开数量或大小超过安全上限。')
                basename = PurePosixPath(member_path).name
                if not _log_kind(basename):
                    continue
                if windows and admitted:
                    _WindowsNames().check(PurePosixPath(basename), directory=False)
                stream = opener()
                if stream is None:
                    raise ValueError('无法读取归档成员。')
                with stream:
                    checksum, actual = _hash_stream(stream, size)
                value = {'source': source, 'basename': basename, 'sha256': checksum, 'bytes': actual}
                member_id = _member_id(value)
                origin = {'artifact_id': artifact_id, 'member_path': member_path,
                          'archive_member_name': original_name}
                if member_id not in members:
                    value.update({'namespace_path': 'members/' + member_id + '/' + basename,
                                  'eligible': admitted, 'origins': [origin], 'ordinal': len(members)})
                    members[member_id] = value
                else:
                    members[member_id]['origins'].append(origin)
                    members[member_id]['eligible'] |= admitted
        _plain(path, root)
        if _file_hash(path) != (artifact['sha256'], artifact['bytes']):
            raise ValueError('输入文件在建立审计目录期间发生变化。')
    if not members:
        raise ValueError('输入中没有可供完整统计的 .log 文本文件。')
    selected = _selected(members, selection, windows)
    descriptor = _selection_record(selection)
    result = {'version': VERSION, 'selection_policy': POLICY, 'windows': windows,
              'selection': descriptor, 'selection_sha256': descriptor['fingerprint'] if descriptor else None,
              'artifacts': artifacts, 'members': members, 'selected_member_ids': selected,
              'totals': {'artifacts': len(artifacts), 'logical_members': len(members),
                         'eligible_members': sum(value['eligible'] for value in members.values()),
                         'selected_members': len(selected), 'archive_members': count,
                         'expanded_bytes': expanded, 'input_bytes': input_bytes}}
    result['catalog_sha256'] = _digest(result)
    return validate_catalog(result)


def _validate_catalog(catalog):
    """Strict structural/digest validation; return the same immutable-by-contract object.

    Runtime independently replays the original predicates before consumption.
    A JSON descriptor alone cannot establish that external source contract.
    """
    expected = {'version', 'selection_policy', 'windows', 'selection', 'selection_sha256',
                'artifacts', 'members', 'selected_member_ids', 'totals', 'catalog_sha256'}
    if type(catalog) is not dict or set(catalog) != expected:
        raise ValueError('来源目录审计结构不完整。')
    if catalog['version'] != VERSION or catalog['selection_policy'] != POLICY or type(catalog['windows']) is not bool:
        raise ValueError('来源目录版本或策略不匹配。')
    if not _checksum(catalog['catalog_sha256']) or _digest({key: value for key, value in catalog.items() if key != 'catalog_sha256'}) != catalog['catalog_sha256']:
        raise ValueError('来源目录摘要不一致。')
    selection = catalog['selection']
    if selection is None:
        if catalog['selection_sha256'] is not None:
            raise ValueError('来源目录规则身份不一致。')
    else:
        if (type(selection) is not dict or set(selection) != {'fingerprint', 'time_range', 'epochs', 'prefixes'}
                or not _checksum(selection['fingerprint']) or catalog['selection_sha256'] != selection['fingerprint']
                or type(selection['time_range']) is not str or not re.fullmatch(r'\d{14}/\d{14}', selection['time_range'])
                or type(selection['epochs']) is not list or len(selection['epochs']) != 2
                or any(type(number) not in (int, float) or not math.isfinite(number) for number in selection['epochs'])
                or selection['epochs'][0] > selection['epochs'][1]
                or type(selection['prefixes']) is not list or not selection['prefixes']
                or any(type(prefix) is not str or not prefix or '/' in prefix or '\\' in prefix or '\x00' in prefix for prefix in selection['prefixes'])
                or len(set(selection['prefixes'])) != len(selection['prefixes'])):
            raise ValueError('来源目录时间或规则描述不完整。')
    artifacts, members = catalog['artifacts'], catalog['members']
    if type(artifacts) is not dict or not artifacts or type(members) is not dict or not members:
        raise ValueError('来源目录缺少输入或日志成员。')
    if len(artifacts) > MAX_ARCHIVE_MEMBERS or len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError('来源目录数量超过安全上限。')
    paths, artifact_ordinals = set(), set()
    for key, value in artifacts.items():
        if type(value) is not dict or set(value) != {'source', 'relative_path', 'sha256', 'bytes', 'kind', 'eligible', 'ordinal'}:
            raise ValueError('来源目录输入身份不完整。')
        if (not _checksum(key) or type(value['source']) is not str or value['source'] not in SOURCES or not _checksum(value['sha256'])
                or not _integer(value['bytes'], maximum=MAX_INPUT_BYTES) or type(value['eligible']) is not bool
                or not _integer(value['ordinal']) or value['ordinal'] in artifact_ordinals
                or value['kind'] not in ('log', 'zip', 'tar') or type(value['relative_path']) is not str):
            raise ValueError('来源目录输入身份无效。')
        safe = _safe_member_name(value['relative_path'])
        if (str(safe) != value['relative_path'] or safe.suffix != '.' + value['kind']
                or value['source'] != _source(safe) or value['relative_path'].casefold() in paths
                or _artifact_id(value) != key or value['kind'] == 'log' and not value['eligible']):
            raise ValueError('来源目录输入路径或摘要无效。')
        paths.add(value['relative_path'].casefold())
        artifact_ordinals.add(value['ordinal'])
    if artifact_ordinals != set(range(len(artifacts))):
        raise ValueError('来源目录输入顺序不连续。')
    member_ordinals, origins_seen, origin_bytes = set(), set(), 0
    for key, value in members.items():
        if type(value) is not dict or set(value) != {'source', 'basename', 'sha256', 'bytes', 'namespace_path', 'eligible', 'origins', 'ordinal'}:
            raise ValueError('来源目录成员身份不完整。')
        if (not _checksum(key) or type(value['source']) is not str or value['source'] not in SOURCES or not _checksum(value['sha256'])
                or not _integer(value['bytes'], maximum=MAX_EXPANDED_BYTES) or type(value['eligible']) is not bool
                or not _integer(value['ordinal']) or value['ordinal'] in member_ordinals
                or type(value['basename']) is not str or _safe_member_name(value['basename']).name != value['basename']
                or not value['basename'].endswith('.log') or _member_id(value) != key
                or value['namespace_path'] != 'members/' + key + '/' + value['basename']
                or type(value['origins']) is not list or not value['origins']):
            raise ValueError('来源目录成员路径、顺序或摘要无效。')
        member_ordinals.add(value['ordinal'])
        eligible = False
        last_ordinal = -1
        for origin in value['origins']:
            if (type(origin) is not dict or set(origin) != {'artifact_id', 'member_path', 'archive_member_name'}
                    or type(origin['artifact_id']) is not str or origin['artifact_id'] not in artifacts
                    or type(origin['member_path']) is not str or type(origin['archive_member_name']) is not str):
                raise ValueError('来源目录成员归档引用无效。')
            artifact = artifacts[origin['artifact_id']]
            safe = _safe_member_name(origin['member_path'])
            identity = (origin['artifact_id'], origin['member_path'])
            if (str(safe) != origin['member_path'] or safe.name != value['basename']
                    or str(_safe_member_name(origin['archive_member_name'])) != origin['member_path']
                    or artifact['source'] != value['source'] or identity in origins_seen
                    or artifact['ordinal'] < last_ordinal
                    or artifact['kind'] == 'log' and (origin['member_path'] != PurePosixPath(artifact['relative_path']).name
                                                    or origin['archive_member_name'] != origin['member_path']
                                                    or artifact['sha256'] != value['sha256'] or artifact['bytes'] != value['bytes'])):
                raise ValueError('来源目录成员来源或内容身份无效。')
            if catalog['windows'] and artifact['eligible']:
                _WindowsNames().check(safe, directory=False)
            last_ordinal = artifact['ordinal']
            origins_seen.add(identity)
            origin_bytes += value['bytes']
            eligible |= artifact['eligible']
        if value['eligible'] != eligible:
            raise ValueError('来源目录成员准入与归档准入不一致。')
    if member_ordinals != set(range(len(members))):
        raise ValueError('来源目录成员顺序不连续。')
    selected = catalog['selected_member_ids']
    if (type(selected) is not list or any(type(key) is not str or key not in members or not members[key]['eligible'] for key in selected)
            or len(set(selected)) != len(selected)
            or selected != sorted(selected, key=lambda key: members[key]['ordinal'])):
        raise ValueError('来源目录实际选集无效。')
    if selection is None and set(selected) != {key for key, value in members.items() if value['eligible']}:
        raise ValueError('无选择谓词的来源目录不得缩减准入日志。')
    groups = {}
    for key, value in members.items():
        if value['eligible']:
            groups.setdefault((value['source'], value['basename'].casefold()), []).append(key)
    if any(len(keys) > 1 and set(selected).intersection(keys) for keys in groups.values()):
        raise ValueError('同一来源同名日志内容或大小写不同且进入原生实际选集。')
    totals = catalog['totals']
    if (type(totals) is not dict or set(totals) != {'artifacts', 'logical_members', 'eligible_members', 'selected_members', 'archive_members', 'expanded_bytes', 'input_bytes'}
            or any(not _integer(value) for value in totals.values())
            or totals['artifacts'] != len(artifacts) or totals['logical_members'] != len(members)
            or totals['eligible_members'] != sum(value['eligible'] for value in members.values())
            or totals['selected_members'] != len(selected)
            or not len(origins_seen) <= totals['archive_members'] <= MAX_ARCHIVE_MEMBERS
            or not origin_bytes <= totals['expanded_bytes'] <= MAX_EXPANDED_BYTES
            or totals['input_bytes'] != sum(value['bytes'] for value in artifacts.values())
            or totals['input_bytes'] > MAX_INPUT_BYTES):
        raise ValueError('来源目录预算或计数无效。')
    return catalog


def validate_catalog(catalog):
    """Validate a finite JSON catalog, returning the same object without edits."""
    try:
        return _validate_catalog(catalog)
    except (TypeError, OverflowError, RecursionError, UnicodeError):
        raise ValueError('来源目录含无效的审计字段类型。') from None


def materialize_catalog(catalog, source_root, target_root):
    """Copy all archive-admitted logs to unique paths; never extractall/link.

    Every source artifact is digest-checked before and after staging. The caller
    publishes the returned receipt only after this operation succeeds in full.
    """
    validate_catalog(catalog)
    source_root, target_root = Path(source_root).absolute(), Path(target_root).absolute()
    _plain(source_root, source_root)
    if target_root == source_root or source_root.is_relative_to(target_root):
        raise ValueError('原生暂存目标不能覆盖输入目录。')
    ancestor = target_root
    while not filesystem_path(ancestor).exists():
        if ancestor.parent == ancestor:
            raise ValueError('原生暂存目标缺少受控父目录。')
        ancestor = ancestor.parent
    _plain(ancestor, Path(ancestor.anchor))
    if filesystem_path(target_root).exists():
        _plain(target_root, ancestor)
        if not filesystem_path(target_root).is_dir() or any(filesystem_path(target_root).iterdir()):
            raise ValueError('原生暂存目录已存在内容，拒绝混入或复用。')
    else:
        filesystem_path(target_root).mkdir(parents=True, exist_ok=False)
    artifacts = catalog['artifacts']
    def check_inputs():
        for artifact in sorted(artifacts.values(), key=lambda row: row['ordinal']):
            path = source_root.joinpath(*PurePosixPath(artifact['relative_path']).parts)
            _plain(path, source_root)
            if not filesystem_path(path).is_file() or _file_hash(path) != (artifact['sha256'], artifact['bytes']):
                raise ValueError('原生暂存输入与已验证来源目录不一致。')
    check_inputs()
    by_artifact = {}
    staged = {}
    for key, member in sorted(catalog['members'].items(), key=lambda row: row[1]['ordinal']):
        if member['eligible']:
            origin = next(origin for origin in member['origins'] if artifacts[origin['artifact_id']]['eligible'])
            by_artifact.setdefault(origin['artifact_id'], {})[origin['member_path']] = (key, member, origin['archive_member_name'])
    total = 0
    for artifact_id, wanted in by_artifact.items():
        artifact = artifacts[artifact_id]
        path = source_root.joinpath(*PurePosixPath(artifact['relative_path']).parts)
        with _entries(path, artifact['kind'], windows=catalog['windows']) as entries:
            found = set()
            for member_path, original_name, size, opener in entries:
                if member_path not in wanted:
                    continue
                key, member, expected_original_name = wanted[member_path]
                if size != member['bytes'] or original_name != expected_original_name:
                    raise ValueError('原生暂存成员大小与来源目录不一致。')
                destination = target_root.joinpath(*PurePosixPath(member['namespace_path']).parts)
                filesystem_path(destination.parent).mkdir(parents=True, exist_ok=False)
                _plain(destination.parent, target_root)
                stream = opener()
                if stream is None:
                    raise ValueError('无法读取原生暂存成员。')
                with stream, filesystem_path(destination).open('xb') as output:
                    checksum, length = _hash_stream(stream, size, output)
                total += length
                if total > MAX_EXPANDED_BYTES or (checksum, length) != (member['sha256'], member['bytes']):
                    raise ValueError('原生暂存成员内容与来源目录不一致。')
                _plain(destination, target_root)
                if _file_hash(destination, maximum=MAX_EXPANDED_BYTES) != (checksum, length):
                    raise ValueError('原生暂存成员写入后校验失败。')
                staged[key] = {'namespace_path': member['namespace_path'], 'sha256': checksum, 'bytes': length}
                found.add(member_path)
            if found != set(wanted):
                raise ValueError('原生暂存成员在来源归档中缺失。')
    check_inputs()
    if len(staged) != catalog['totals']['eligible_members']:
        raise ValueError('原生暂存成员不完整。')
    return {'version': MATERIALIZATION_VERSION, 'catalog_sha256': catalog['catalog_sha256'],
            'members': staged, 'member_count': len(staged), 'bytes': total}
