"""Normalize only verified XVM Zstandard text for the native directory collector."""
import codecs
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from native_windows_host import filesystem_path

MAX_EXPANDED_BYTES = 20 * 1024**3
VERSION = 'native-input-v2'
RECOVERY = 'complete_decoded_lines'


def sha(path):
    value=hashlib.sha256()
    with filesystem_path(path).open('rb') as handle:
        for part in iter(lambda:handle.read(1024**2),b''):value.update(part)
    return value.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _quality(rows):
    issues=[]
    for row in rows:
        if (not isinstance(row,dict) or type(row.get('bytes')) is not int or row['bytes']<0
                or not isinstance(row.get('relative_path'),str) or not row['relative_path']
                or not isinstance(row.get('original_sha256'),str)
                or not re.fullmatch('[0-9a-f]{64}',row['original_sha256'])):
            raise ValueError('归整缓存的逐文件恢复审计结构无效。')
        complete=row.get('frame_complete')
        discarded=row.get('discarded_tail_bytes')
        if type(complete) is not bool or type(discarded) is not int or discarded<0:
            raise ValueError('归整缓存缺少有效的压缩帧完整性审计。')
        if complete:
            if row.get('recovery') is not None or discarded!=0:
                raise ValueError('完整帧不能声明恢复或丢弃尾行。')
        else:
            if (row.get('normalization')!='xvm_zstd_utf8_text' or row.get('recovery')!=RECOVERY
                    or type(row.get('bytes')) is not int or row['bytes']<=0):
                raise ValueError('不完整压缩帧的恢复审计无效。')
            issues.append({'kind':'incomplete_zstd_frame','relative_path':row['relative_path'],
                           'source_sha256':row['original_sha256'],'recovery':RECOVERY,
                           'discarded_tail_bytes':discarded})
    return {'status':'partial' if issues else 'ok','issues':issues}


def _audit_digest(result):
    return _digest({key:result[key] for key in
                    ('version','policy','input_sha256','files','xvm_expanded_bytes','quality')})


def prepare_native_inputs(source, destination, manifest, *, allow_incomplete_frames=False):
    """All inputs remain bound to the already-selected window/environment.

    XVM .zst is a text compression format, not an MCU binary decoder. Unknown
    names, invalid UTF-8, NULs and resource limit violations fail explicitly.
    Strict mode rejects incomplete frames. Opt-in recovery accepts only an EOF
    before frame completion, retains complete decoded newline-terminated lines,
    and records partial quality; malformed/checksum-failed frames still fail.
    Completed normalization can be reused after checking every output hash.
    """
    if type(allow_incomplete_frames) is not bool:
        raise ValueError('allow_incomplete_frames 必须明确为布尔值。')
    policy={'allow_incomplete_frames':allow_incomplete_frames,'incomplete_frame_recovery':RECOVERY}
    source=Path(source);destination=Path(destination)
    for root in (source,destination):
        physical=filesystem_path(root)
        if physical.exists() and (physical.is_symlink() or getattr(physical.lstat(),'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400)):
            raise ValueError('原生输入/归整根目录禁止链接或目录联接。')
    source=source.resolve();destination=destination.resolve()
    if (not isinstance(manifest,dict) or not isinstance(manifest.get('files'),list)
            or not manifest['files'] or any(not isinstance(row,dict) for row in manifest['files'])):
        raise ValueError('原生归整需要非空且完整的输入清单。')
    if destination==source or destination.is_relative_to(source):
        raise ValueError('原生归整目录须独立于原始输入。')
    binding=_digest({'source_manifest':manifest,'policy':policy})
    checkpoint=destination/'normalized-manifest.json'
    if filesystem_path(checkpoint).is_file():
        saved=json.loads(filesystem_path(checkpoint).read_text(encoding='utf-8'))
        if saved.get('version')!=VERSION or saved.get('policy')!=policy:
            raise ValueError('归整缓存版本或不完整帧恢复策略不一致。')
        if saved.get('input_sha256')!=binding:raise ValueError('归整缓存不属于当前输入。')
        expected={}
        for item in manifest['files']:
            relative=Path(item['relative_path'])
            if relative.suffix.casefold()=='.zst':relative=relative.with_suffix('.log')
            name=relative.as_posix()
            if name in expected:raise ValueError('归整计划存在重名目标。')
            expected[name]=item
        files=saved.get('files')
        if (not isinstance(files,list) or len(files)!=len(expected)
                or {r.get('relative_path') for r in files if isinstance(r,dict)}!=set(expected)):
            raise ValueError('归整缓存文件清单截断、重复或缺失。')
        quality=_quality(files)
        expanded=sum(row['bytes']+row['discarded_tail_bytes'] for row in files
                     if row.get('normalization')=='xvm_zstd_utf8_text')
        if (quality!=saved.get('quality') or quality['status']=='partial' and not allow_incomplete_frames
                or type(saved.get('xvm_expanded_bytes')) is not int or saved['xvm_expanded_bytes']!=expanded
                or not 0<=expanded<=MAX_EXPANDED_BYTES or saved.get('audit_sha256')!=_audit_digest(saved)):
            raise ValueError('归整缓存的完整性状态、恢复审计或展开字节数不一致。')
        physical_destination=filesystem_path(destination)
        actual={p.relative_to(physical_destination).as_posix() for p in physical_destination.rglob('*')
                if p.is_file() and p!=filesystem_path(checkpoint)}
        if actual!=set(expected):raise ValueError('归整目录含额外或遗漏的文件。')
        for row in files:
            original=expected[row['relative_path']]
            expected_normalization='xvm_zstd_utf8_text' if Path(original['relative_path']).suffix.casefold()=='.zst' else 'unchanged'
            if (row.get('original_sha256')!=original['sha256'] or row.get('log_class')!=original['log_class']
                    or row.get('normalization')!=expected_normalization
                    or (row.get('normalization')=='unchanged' and row.get('sha256')!=original['sha256'])):
                raise ValueError('归整缓存的来源映射不一致。')
            target=destination/row['relative_path']
            physical_target=filesystem_path(target)
            if (not target.resolve().is_relative_to(destination) or not physical_target.is_file() or physical_target.is_symlink()
                    or physical_target.stat().st_size!=row.get('bytes') or sha(target)!=row['sha256']):
                raise ValueError('归整缓存文件缺失或变化。')
            if not row['frame_complete']:
                with physical_target.open('rb') as handle:
                    handle.seek(-1,os.SEEK_END)
                    if handle.read(1)!=b'\n':raise ValueError('恢复缓存没有以完整行结束。')
        if not saved.get('files'):raise ValueError('归整缓存为空。')
        return saved
    if filesystem_path(destination).exists() and any(filesystem_path(destination).iterdir()):
        raise ValueError('归整目录含未完成记录，请保留诊断并选择新任务。')
    filesystem_path(destination).mkdir(parents=True,exist_ok=True)
    rows=[];expanded=0
    for item in manifest['files']:
        original=source/item['relative_path']
        if not original.resolve().is_relative_to(source):
            raise ValueError('归整前输入身份/哈希校验失败。')
        physical_original=filesystem_path(original)
        if not physical_original.is_file() or physical_original.is_symlink() or sha(original)!=item['sha256']:
            raise ValueError('归整前输入身份/哈希校验失败。')
        relative=Path(item['relative_path']);target=destination/relative
        compressed=original.suffix.casefold()=='.zst'
        if compressed:
            if item.get('log_class') not in ('log_xcu_mcu','log_bzcu_eth') or not re.fullmatch(r'XVM_(?:MCU|BZCU)-[^/\\]+\.zst',original.name):
                raise ValueError('完整解析仅支持已核实的 XVM 文本 Zstandard 日志。')
            target=target.with_suffix('.log')
        if not target.resolve().is_relative_to(destination) or filesystem_path(target).exists():raise ValueError('归整目标越界或重名。')
        filesystem_path(target.parent).mkdir(parents=True,exist_ok=True)
        frame_complete=True;recovery=None;discarded_tail_bytes=0
        if compressed:
            import zstandard
            temporary=target.with_suffix('.log.partial')
            decoder=codecs.getincrementaldecoder('utf-8')('strict');count=0;last_newline=0
            try:
                with filesystem_path(original).open('rb') as incoming, filesystem_path(temporary).open('xb') as outgoing:
                    # stream_reader accepts a truncated final frame at EOF. Use
                    # eof per frame, including concatenated frames/checksums.
                    # Small compressed chunks also bound transient expansion.
                    frame=None
                    for compressed_block in iter(lambda:incoming.read(1024),b''):
                        pending=compressed_block
                        while pending:
                            if frame is None:
                                frame=zstandard.ZstdDecompressor().decompressobj(write_size=1024**2)
                            block=frame.decompress(pending)
                            count+=len(block);expanded+=len(block)
                            if expanded>MAX_EXPANDED_BYTES or b'\x00' in block:
                                raise ValueError('XVM 解压超过上限或含二进制内容。')
                            decoder.decode(block);outgoing.write(block)
                            newline=block.rfind(b'\n')
                            if newline>=0:last_newline=count-len(block)+newline+1
                            if frame.eof:
                                pending=frame.unused_data;frame=None
                            else:
                                pending=b''
                    if frame is not None:
                        if not allow_incomplete_frames:
                            raise ValueError('XVM Zstandard 压缩帧不完整，禁止使用截断日志。')
                        if not last_newline:
                            raise ValueError('XVM 不完整帧没有可恢复的完整行。')
                        # decode(..., final=False) has already rejected invalid
                        # body bytes. An unfinished UTF-8 tail is discarded with
                        # the last unterminated line, never turned into text.
                        discarded_tail_bytes=count-last_newline
                        outgoing.truncate(last_newline);count=last_newline
                        frame_complete=False;recovery=RECOVERY
                    else:
                        decoder.decode(b'',final=True)
                if not count:raise ValueError('XVM 解压结果为空。')
                filesystem_path(temporary).replace(filesystem_path(target))
            finally:
                filesystem_path(temporary).unlink(missing_ok=True)
        else:
            try:os.link(filesystem_path(original),filesystem_path(target))
            except OSError:shutil.copyfile(filesystem_path(original),filesystem_path(target))
            count=filesystem_path(target).stat().st_size
        rows.append({'relative_path':target.relative_to(destination).as_posix(),'sha256':sha(target),'bytes':count,
                     'original_sha256':item['sha256'],'log_class':item['log_class'],
                     'normalization':'xvm_zstd_utf8_text' if compressed else 'unchanged',
                     'frame_complete':frame_complete,'recovery':recovery,'discarded_tail_bytes':discarded_tail_bytes})
    result={'version':VERSION,'policy':policy,'input_sha256':binding,'files':rows,
            'xvm_expanded_bytes':expanded,'quality':_quality(rows)}
    result['audit_sha256']=_audit_digest(result)
    temporary=checkpoint.with_suffix('.tmp')
    filesystem_path(temporary).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    filesystem_path(temporary).replace(filesystem_path(checkpoint))
    return result
