"""Prepare AVM inputs before parsing; never change the delivered PCM algorithm."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

import cloud_log_client as cloud
from native_log_source import digest,write_json,portable_filename
from performance_log_source import filter_avm_files,decompress_zst
from state_performance import extract
from request_model import InputError
from environment_policy import provider
from download_scheduler import DownloadBudget, download_workers, run_bounded

POLICY='avm-exact-query-v1'
LOG_CLASS='log_fsdA_service'


def cleanup_inputs(directory,settings):
    if settings['local_analysis'].get('keep_raw_logs',False):return
    directory=Path(directory).resolve();root=directory/'service-input'
    if root.is_symlink() or not root.resolve().is_relative_to(directory):
        raise InputError('AVM 清理路径越界。')
    if root.exists():shutil.rmtree(root)


def cached_result(metadata,directory,*,environment='test'):
    env_name=provider(environment)
    path=Path(directory)/'pcm-result.json'
    if not path.is_file():return None
    cached=json.loads(path.read_text(encoding='utf-8'))
    result=cached.get('result')
    if (cached.get('request')!=metadata or cached.get('source_policy')!=POLICY
            or cached.get('environment')!=env_name or not isinstance(result,dict)
            or result.get('source',{}).get('environment')!=env_name):
        raise InputError('PCM 结果缓存的请求或来源策略不一致。')
    encoded=json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
    import hashlib
    if hashlib.sha256(encoded).hexdigest()!=cached.get('result_sha256'):
        raise InputError('PCM 缓存内容校验失败。')
    return result


def discover(metadata,settings,*,environment='test'):
    env_name=provider(environment)
    username=cloud.load_settings(Path(settings['cloud_settings_path']))['username']
    query=cloud.query_files(vin=metadata['vin'],start=metadata['start'].replace('T',' '),
                            end=metadata['end'].replace('T',' '),log_class=LOG_CLASS,
                            username=username,environment=environment)
    if query.environment!=environment or query.provider_environment!=env_name:
        raise InputError('AVM 查询返回了其他环境。')
    if any(str(item.get('vin') or '').upper()!=metadata['vin'].upper() for item in query.items):
        raise InputError('车云服务日志列表的 VIN 缺失或与当前时段不一致，不能判断为空。')
    selected=filter_avm_files(query.items)
    validate_candidates(metadata,selected)
    return selected


def validate_candidates(metadata,selected):
    if not isinstance(selected,list) or len(selected)>200:
        raise InputError('AVM 文件候选无效或超过 200 上限，拒绝截断。')
    for item in selected:
        if not isinstance(item,dict) or str(item.get('vin') or '').upper()!=metadata['vin'].upper():
            raise InputError('AVM 日志 VIN 缺失或不一致。')
    if sum(int(i.get('fileSize') or 0) for i in selected)>20*1024**3:
        raise InputError('AVM 输入超过 20 GiB 上限。')


def prepare(metadata,settings,directory,*,environment='test',selected=None):
    began=perf_counter()
    workers=download_workers(settings)
    env_name=provider(environment)
    directory=Path(directory)
    root=directory/'service-input'
    path=root/'download-manifest.json'
    if path.is_file():
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('request')!=metadata or data.get('source_policy')!=POLICY or data.get('environment')!=env_name:
            raise InputError('AVM 来源清单的身份或环境不一致。')
        verify(root,data)
        return data
    if root.exists() and any(root.iterdir()):
        raise InputError('AVM 下载目录存在未完成输入；保留诊断后使用独立目录。')
    if selected is None:selected=discover(metadata,settings,environment=environment)
    validate_candidates(metadata,selected)
    jobs=[]
    for index,item in enumerate(selected,1):
        if str(item.get('vin') or '').upper()!=metadata['vin'].upper():
            raise InputError('AVM 日志 VIN 缺失或不一致。')
        url=item.get('downloadURL') or item.get('downloadUrl') or item.get('url')
        if not url:raise InputError('AVM 文件缺少下载地址。')
        target=root/f'item-{index:04d}'/portable_filename(item,index)
        jobs.append((item,url,target))
    budget=DownloadBudget(20*1024**3)

    def transfer(job):
        item,url,target=job
        started=perf_counter()
        count=cloud.download_to_file(url,target,environment=environment,max_bytes=2*1024**3,byte_budget=budget)
        downloaded=perf_counter()
        if item.get('fileSize') is not None and int(item['fileSize'])!=count:
            raise InputError('AVM 文件字节数与车云记录不一致。')
        checksum=digest(target)
        return {'relative_path':target.relative_to(root).as_posix(),'bytes':count,
                'sha256':checksum,'log_class':LOG_CLASS,
                'timings':{'download_seconds':round(downloaded-started,6),
                           'hash_seconds':round(perf_counter()-downloaded,6)}}

    files=run_bounded(jobs,transfer,workers)
    total=sum(f['bytes'] for f in files)
    if total>20*1024**3:raise InputError('AVM 输入超过 20 GiB 上限。')
    data={'request':metadata,'query':metadata,'environment':env_name,'source_policy':POLICY,
          'counts':{LOG_CLASS:len(files)},'bytes':total,'files':files,
          'timings':{'prepare_seconds':round(perf_counter()-began,6),'download_workers':workers,
                     'download_file_seconds':round(sum(f['timings']['download_seconds'] for f in files),6)}}
    write_json(path,data)
    return data


def verify(root,data):
    root=Path(root).resolve()
    for item in data['files']:
        path=root/item['relative_path']
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file() or digest(path)!=item['sha256']:
            raise InputError('AVM 预备输入缺失或哈希变化。')


def parse(metadata,settings,directory,prepared,*,environment='test'):
    began=perf_counter()
    env_name=provider(environment)
    directory=Path(directory)
    cached=cached_result(metadata,directory,environment=environment)
    if cached is not None:return cached
    root=directory/'service-input'
    actual=json.loads((root/'download-manifest.json').read_text(encoding='utf-8'))
    if (actual!=prepared or actual.get('request')!=metadata or actual.get('source_policy')!=POLICY
            or actual.get('environment')!=env_name):
        raise InputError('AVM 预备输入清单与当前请求不一致。')
    verify(root,actual)
    verified=perf_counter()
    decompression_seconds=extraction_seconds=0
    if actual['files']:
        with tempfile.TemporaryDirectory(prefix='pcm-parse-',dir=directory) as temporary:
            logs=Path(temporary)/'logs'
            for index,item in enumerate(actual['files'],1):
                source=root/item['relative_path']
                decompress_zst(source,logs/f'item-{index:04d}'/source.name[:-4])
            decompressed=perf_counter()
            decompression_seconds=decompressed-verified
            result=extract(logs,metadata['start'],metadata['end'])
            extraction_seconds=perf_counter()-decompressed
    else:
        result={'status':'failed','metrics':{},'coverage':{'files_read':0},'error':'该时段没有 AVM_Service 日志。'}
    result['time_window']={'start':metadata['start'],'end':metadata['end']}
    result['source']={'kind':LOG_CLASS,'environment':env_name,'selected_files':len(actual['files']),
                      'input_manifest_sha256':digest(root/'download-manifest.json')}
    result['timings']={'prepare':actual.get('timings',{}),'verify_seconds':round(verified-began,6),
                       'decompression_seconds':round(decompression_seconds,6),
                       'extract_seconds':round(extraction_seconds,6)}
    import hashlib
    checksum=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()
    write_json(directory/'pcm-result.json',{'request':metadata,'source_policy':POLICY,'environment':env_name,
                                         'result':result,'result_sha256':checksum})
    cleanup_inputs(directory,settings)
    return result
