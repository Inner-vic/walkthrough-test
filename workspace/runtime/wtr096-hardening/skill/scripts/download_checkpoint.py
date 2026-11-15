"""Fail-closed, URL-free receipts for one owned AD download directory.

The task worker owns the process lock. Distinct file transfers may commit from
multiple threads; no unreceipted file is ever adopted or silently overwritten.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from threading import Lock
import zipfile

from native_windows_host import filesystem_path
from request_model import InputError

VERSION = 'native-download-checkpoint-v1'
RECEIPT_VERSION = 'native-download-receipt-v1'
PLAN_NAME = 'download-plan.json'
RECEIPTS_NAME = 'download-receipts'
CLASSES = ('log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _no_links(path):
    for entry in (path, *path.parents):
        try:
            info = filesystem_path(entry).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise InputError('下载断点路径禁止链接或重解析点。')


def _read(path):
    _no_links(path)
    if filesystem_path(path).stat().st_size > 8 * 1024**2:
        raise InputError('下载断点记录超过大小上限。')
    try:
        return json.loads(filesystem_path(path).read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise InputError('下载断点记录格式无效；保留现场，不自动吸纳文件。') from None


def _atomic(path, value):
    _no_links(path)
    temporary = path.with_name(path.name + '.tmp')
    _no_links(temporary)
    with filesystem_path(temporary).open('xb') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                allow_nan=False).encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    filesystem_path(temporary).replace(filesystem_path(path))


def _relative(value):
    if (not isinstance(value, str) or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or any(x in ('', '.', '..') for x in value.split('/'))):
        raise InputError('下载断点目标必须是计划内的安全相对路径。')
    parts = value.split('/')
    if (len(parts) != 3 or parts[0] not in CLASSES or not re.fullmatch(r'item-\d{4}', parts[1])
            or any(c in parts[2] for c in '<>:"|?*\r\n')):
        raise InputError('下载断点目标结构无效。')
    return Path(*parts)


def plan_entry(item, relative_path, log_class):
    """Whitelist stable object identity; never include URL/auth/username fields."""
    _relative(relative_path)
    identity = {key: deepcopy(item.get(key)) for key in ('vin', 'fileKey', 'fileName', 'name', 'collectTime', 'fileSize')}
    if (log_class not in CLASSES or relative_path.split('/')[0] != log_class
            or not isinstance(identity['vin'], str)
            or any(value is not None and type(value) not in (str, int) for value in identity.values())
            or identity['fileSize'] is not None and (isinstance(identity['fileSize'], bool)
                or not re.fullmatch(r'\d+', str(identity['fileSize'])))
            or any(isinstance(value, str) and ('\x00' in value or re.search(r'https?://', value, re.I))
                   for value in identity.values())):
        raise InputError('下载断点对象身份不是可保存的稳定来源字段。')
    return {'relative_path': relative_path, 'log_class': log_class, 'identity': identity}


def verify_file(path, *, expected_size=None):
    _no_links(path)
    physical = filesystem_path(path)
    info = physical.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 2 * 1024**3:
        raise InputError('下载断点文件缺失、为空或不是普通文件。')
    if expected_size is not None and info.st_size != expected_size:
        raise InputError('下载断点文件长度与收据或来源声明不一致。')
    checksum = hashlib.sha256()
    with physical.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            checksum.update(chunk)
    if path.suffix.casefold() == '.zip':
        try:
            with zipfile.ZipFile(physical) as archive:
                for member in archive.infolist():
                    name = member.filename.replace('\\', '/')
                    mode = (member.external_attr >> 16) & 0xffff
                    if (name.startswith('/') or '..' in name.split('/') or re.match(r'^[A-Za-z]:', name)
                            or stat.S_ISLNK(mode) or stat.S_IFMT(mode) and not
                            (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                        raise InputError('ZIP 内部路径或文件类型不安全。')
                if archive.testzip() is not None:
                    raise InputError('ZIP 完整性校验失败。')
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise InputError('ZIP 完整性校验失败。') from None
    return info.st_size, checksum.hexdigest()


def _file_record(record, relative, log_class):
    if (not isinstance(record, dict) or set(record) != {'relative_path', 'bytes', 'sha256', 'log_class', 'timings'}
            or record.get('relative_path') != relative or record.get('log_class') != log_class
            or type(record.get('bytes')) is not int or record['bytes'] <= 0
            or not isinstance(record.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
            or not isinstance(record.get('timings'), dict)
            or set(record['timings']) != {'download_seconds', 'zip_validation_seconds', 'hash_seconds'}
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in record['timings'].values())):
        raise InputError('下载断点文件收据结构无效。')


class DownloadCheckpoint:
    def __init__(self, directory, *, request, environment, source_policy, entries,
                 execution_release=None, analysis_profile=None, byte_budget):
        original = Path(directory)
        if not original.is_absolute() or '..' in original.parts:
            raise InputError('下载断点目录必须是安全绝对路径。')
        _no_links(original)
        self.directory = original.resolve()
        self.lock = Lock()
        if (not isinstance(entries, list) or not entries or len(entries) > 1600
                or not isinstance(request, dict) or set(request) != {'vin', 'start', 'end'}
                or any(not isinstance(value, str) for value in request.values())
                or environment not in ('prod', 'testtwo')):
            raise InputError('下载断点计划缺少完整来源。')
        self.entries = {}
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {'relative_path', 'log_class', 'identity'}
                    or not isinstance(entry['identity'], dict)
                    or plan_entry(entry['identity'], entry['relative_path'], entry['log_class']) != entry
                    or entry['relative_path'] in self.entries
                    or entry['identity']['vin'].upper() != request.get('vin', '').upper()):
                raise InputError('下载断点计划对象重复或来源绑定无效。')
            self.entries[entry['relative_path']] = entry
        self.plan = {'version': VERSION, 'request': deepcopy(request), 'environment': environment,
            'source_policy': source_policy, 'execution_release': execution_release,
            'analysis_profile': analysis_profile, 'objects': deepcopy(entries)}
        self.fingerprint = _digest(self.plan)
        plan_path = self.directory / PLAN_NAME
        physical = filesystem_path(self.directory)
        if physical.exists() and any(physical.iterdir()):
            if not filesystem_path(plan_path).is_file() or _read(plan_path) != self.plan:
                raise InputError('下载目录没有本版匹配计划；不吸纳旧任务、外来或身份变化的输入。')
        else:
            physical.mkdir(parents=True, exist_ok=True)
            _atomic(plan_path, self.plan)
        self.receipts = self.directory / RECEIPTS_NAME
        _no_links(self.receipts)
        filesystem_path(self.receipts).mkdir(exist_ok=True)
        self.verified = {}
        self._load_receipts(byte_budget)

    def _receipt_path(self, relative):
        return self.receipts / (hashlib.sha256(relative.encode('utf-8')).hexdigest() + '.json')

    def _load_receipts(self, budget):
        expected_receipts = {self._receipt_path(name).name: name for name in self.entries}
        for physical in filesystem_path(self.receipts).iterdir():
            if physical.name not in expected_receipts or not physical.is_file():
                raise InputError('下载断点收据目录含未完成或外来记录。')
            relative = expected_receipts[physical.name]
            entry = self.entries[relative]
            saved = _read(self._receipt_path(relative))
            if (not isinstance(saved, dict) or set(saved) != {'version', 'plan_sha256', 'object_sha256', 'file'}
                    or saved['version'] != RECEIPT_VERSION or saved['plan_sha256'] != self.fingerprint
                    or saved['object_sha256'] != _digest(entry)):
                raise InputError('下载断点收据不属于当前完整计划。')
            record = saved['file']
            _file_record(record, relative, entry['log_class'])
            count, checksum = verify_file(self.directory / _relative(relative), expected_size=record['bytes'])
            declared = entry['identity'].get('fileSize')
            if checksum != record['sha256'] or declared is not None and int(declared) != count:
                raise InputError('下载断点文件哈希或来源长度已变化。')
            self.verified[relative] = deepcopy(record)
        allowed_files = {PLAN_NAME, *self.verified}
        allowed_files.update(f'{RECEIPTS_NAME}/{self._receipt_path(name).name}' for name in self.verified)
        allowed_dirs = {RECEIPTS_NAME}
        for name in self.entries:
            parts = PurePosixPath(name).parts
            allowed_dirs.update('/'.join(parts[:index]) for index in range(1, len(parts)))
        for current, dirs, files in os.walk(filesystem_path(self.directory)):
            current_path = Path(current)
            for name in (*dirs, *files):
                physical = current_path / name
                logical = self.directory / physical.relative_to(filesystem_path(self.directory))
                _no_links(logical)
                relative = logical.relative_to(self.directory).as_posix()
                if relative not in (allowed_dirs if name in dirs else allowed_files):
                    raise InputError('下载目录存在无收据、残留或外来文件；保留现场，不自动吸纳或覆盖。')
        budget.consume(sum(record['bytes'] for record in self.verified.values()))

    def reuse(self, relative):
        return deepcopy(self.verified.get(relative))

    def ensure_missing(self, relative):
        if relative not in self.entries:
            raise InputError('下载目标不在当前计划。')
        path = self.directory / _relative(relative)
        _no_links(path)
        if filesystem_path(path).exists():
            raise InputError('下载目标已有无收据文件；保留现场，不自动覆盖。')

    def commit(self, record):
        relative = record.get('relative_path') if isinstance(record, dict) else None
        if relative not in self.entries or record.get('log_class') != self.entries[relative]['log_class']:
            raise InputError('下载收据目标不在当前计划。')
        _file_record(record, relative, self.entries[relative]['log_class'])
        count, checksum = verify_file(self.directory / _relative(relative), expected_size=record.get('bytes'))
        declared = self.entries[relative]['identity'].get('fileSize')
        if checksum != record.get('sha256') or declared is not None and int(declared) != count:
            raise InputError('下载收据与已验证输入或来源长度不一致。')
        with self.lock:
            path = self._receipt_path(relative)
            if relative in self.verified or filesystem_path(path).exists():
                raise InputError('同一下载文件已有收据，不重复提交。')
            _atomic(path, {'version': RECEIPT_VERSION, 'plan_sha256': self.fingerprint,
                          'object_sha256': _digest(self.entries[relative]), 'file': record})
            self.verified[relative] = deepcopy(record)
