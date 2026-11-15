"""Fetch the complete native sources and run the external analyzer in isolation."""
from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import shutil
import zipfile
from time import perf_counter
from pathlib import Path, PureWindowsPath
from datetime import datetime, timedelta

import cloud_log_client as cloud
from request_model import InputError
from native_selection import POLICY as SELECTION_POLICY, validate_selection_audit
from m100_attachment_worker import ANALYSIS_PROFILE
from analyzer_contract import validate_contract_audit
from runtime_audit import validate_runtime_execution
from native_execution_compat import validate_execution_compat_audit, validate_execution_compat_runtime_audit
from environment_policy import policy as environment_policy, provider
from download_scheduler import DownloadBudget, download_workers, run_bounded
from download_checkpoint import DownloadCheckpoint, PLAN_NAME, plan_entry, verify_file

LOG_CLASSES = ('log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth')
# A valid 95-minute archive discovery window can contain 234 fsdA archives.
# Keep a finite count bound together with the unchanged shared 20 GiB budget.
MAX_FILES_PER_CLASS = 400
MAX_BYTES = 20 * 1024**3
HERE = Path(__file__).resolve().parent
SOURCE_POLICY = 'native-four-classes-end-plus-1h-v2'


def _analysis_profile(cfg=None):
    from native_windows_host import analysis_profile
    return analysis_profile(cfg)


def _selection_policy_for(execution_settings=None):
    if execution_settings and execution_settings.get('execution') == 'windows':
        return 'source-namespace-v3'
    return SELECTION_POLICY


def _validate_source_selection(audit, execution_settings=None):
    if not isinstance(audit,dict) or audit.get('policy') != _selection_policy_for(execution_settings):
        raise ValueError('来源身份协议不符合当前执行配置。')
    if audit['policy'] == 'source-namespace-v3':
        from source_namespace import validate_selection_audit as validate_namespace_selection
        return validate_namespace_selection(audit)
    return validate_selection_audit(audit)


def archive_query_metadata(metadata):
    """Include archives closing after end, as the native end+1h selector does.

    This expands file discovery only. The statistical window stays unchanged.
    """
    return {**metadata,'end':(datetime.fromisoformat(metadata['end'])+timedelta(hours=1)).isoformat(timespec='seconds')}


def portable_filename(item, index):
    name = cloud.safe_filename(item,index)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).rstrip(' .')
    if not name or name.split('.',1)[0].upper() in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}:
        name = 'archive-'+name
    return name


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024**2), b''):
            sha.update(chunk)
    return sha.hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def wsl_path(path):
    text = str(path)
    if text.startswith('/'):
        return text
    value = PureWindowsPath(text)
    if not re.fullmatch('[A-Za-z]:', value.drive) or not value.is_absolute():
        raise InputError('WSL 文件路径必须是本机盘符绝对路径，不能使用 UNC 或相对路径。')
    return '/mnt/'+value.drive[0].lower()+'/'+'/'.join(value.parts[1:])


def host_path(path):
    text = str(path)
    if os.name == 'nt' and re.match(r'^/mnt/[a-z]/', text):
        return Path(text[5].upper()+':/'+text[7:])
    return Path(text)


def validate_config(settings):
    cfg = settings.get('local_analysis')
    if not isinstance(cfg, dict):
        raise InputError('缺少走查测试专用 local_analysis 配置。')
    environment_policy(settings)
    if cfg.get('backend') != 'native_full':
        raise InputError('完整附件版必须使用 native_full，不接受四指标裁剪结果。')
    execution = cfg.get('execution', 'wsl' if os.name == 'nt' else 'linux')
    if execution == 'windows':
        from native_windows_host import validate_windows_config
        try:
            validate_windows_config(cfg)
        except ValueError as error:
            raise InputError(str(error)) from None
    else:
        if not str(cfg.get('analyzer_root') or '').startswith('/'):
            raise InputError('analyzer_root 必须指向 Linux/WSL 内最新版分析源码的绝对路径。')
        if not str(cfg.get('python') or '').startswith('/'):
            raise InputError('python 必须指向 Linux/WSL 分析环境解释器的绝对路径。')
        if execution not in ('wsl', 'linux') or execution == 'linux' and os.name == 'nt':
            raise InputError('完整解析须显式指定有效的 wsl、linux 或 windows 执行环境。')
    if execution == 'wsl' and not re.fullmatch(r'[A-Za-z0-9._-]+', str(cfg.get('distribution') or '')):
        raise InputError('需要明确指定已安装的 WSL distribution。')
    if not cfg.get('rules_json') and not cfg.get('rules_spreadsheet_token'):
        raise InputError('必须配置最新版统计规则的 rules_json 或 rules_spreadsheet_token。')
    if not settings.get('cloud_settings_path'):
        raise InputError('必须指定走查测试可读取的车云身份配置 cloud_settings_path。')
    timeout = cfg.get('timeout_seconds', 7200)
    if type(timeout) is not int or timeout <= 0:
        raise InputError('local_analysis.timeout_seconds 必须是正整数。')
    workers = cfg.get('window_workers', 2)
    if type(workers) is not int or workers not in (1, 2):
        raise InputError('local_analysis.window_workers 只能是 1 或 2。')
    download_workers(settings)
    return cfg


def cleanup_inputs(directory, settings):
    cfg = settings['local_analysis']
    if cfg.get('keep_raw_logs',False):
        return
    from native_cleanup import remove_target, validated_target
    directory = Path(directory)
    try:
        targets = {name:validated_target(directory,name) for name in ('input','native-prepared','analysis')}
        execution = cfg.get('execution','wsl' if os.name=='nt' else 'linux')
        if execution not in ('linux','wsl','windows'):
            raise ValueError('未知清理执行环境。')
        # A WSL-created tree can contain colon filenames and paths Windows
        # cannot enumerate. Let the same Linux runtime remove that tree.
        if execution == 'wsl' and targets['analysis'].exists():
            distribution = str(cfg.get('distribution') or '')
            python = str(cfg.get('python') or '')
            if not re.fullmatch(r'[A-Za-z0-9._-]+',distribution) or not python.startswith('/'):
                raise ValueError('缺少固定的 WSL 清理环境。')
            root = wsl_path(directory)
            command = [str(cfg.get('wsl_executable') or 'wsl.exe'),'-d',distribution,'--exec',python,
                       wsl_path(HERE/'native_cleanup.py'),'--root',root]
            completed = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=180)
            result = json.loads(completed.stdout or '{}')
            if (completed.returncode or result.get('status') != 'cleaned'
                    or result.get('target') != root.rstrip('/')+'/analysis'
                    or targets['analysis'].exists()):
                raise ValueError('WSL 清理未确认完成。')
        elif execution in ('linux','windows'):
            remove_target(directory,'analysis')
        for name in ('input','native-prepared'):
            remove_target(directory,name)
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        raise InputError('临时日志清理失败；统计结果已保存，可修复清理后恢复交付。') from exc


def query_class(metadata, log_class, username, *, environment='test'):
    provider(environment)
    if log_class not in LOG_CLASSES:
        raise InputError('走查原始日志类别不在完整解析来源计划中。')
    found, seen, expected_total = [], set(), None
    for page in range(1, MAX_FILES_PER_CLASS+2):
        params = {'vin':metadata['vin'], 'logClass':log_class,
                  'collectTimeMin':metadata['start'].replace('T',' '),
                  'collectTimeMax':metadata['end'].replace('T',' '),
                  'remoteMode':'false','domain':'','pageNo':page,'pageSize':100,
                  'userName':username,'eeaPlatform':'EEA3.0'}
        payload = cloud.request_json(params, username, environment=environment)
        if payload.get('code') not in (None, 0, '0', 200, '200'):
            raise InputError('车云返回非成功业务状态。')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise InputError('车云返回缺少有效 data。')
        items = data.get('list') if 'list' in data else data.get('records')
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise InputError('车云文件列表格式错误。')
        raw_total = data.get('total')
        if isinstance(raw_total, bool) or not re.fullmatch(r'\d+', str(raw_total)):
            raise InputError('车云查询缺少有效总数，不能判断为空。')
        total = int(raw_total)
        if expected_total is None:
            expected_total = total
        if total != expected_total or not 0 <= total <= MAX_FILES_PER_CLASS:
            raise InputError('分页总数发生变化或超过单类型试运行上限。')
        for item in items:
            if str(item.get('vin') or '').upper() != metadata['vin'].upper():
                raise InputError('车云返回 VIN 缺失或与当前任务不一致。')
            key = json.dumps([item.get('fileKey'),item.get('fileName'),item.get('collectTime')])
            if key in seen:
                raise InputError('车云分页重复，拒绝把不完整列表当作成功。')
            seen.add(key)
            found.append(item)
        if len(found) == total:
            return found
        if not items or len(found) > total:
            raise InputError('车云分页未完整返回。')
    raise InputError('车云分页超过试运行上限。')


def fetch_window(metadata, settings, directory, *, environment='test', selected=None):
    started = perf_counter()
    workers = download_workers(settings)
    env_name = provider(environment)
    directory = Path(directory).absolute()
    manifest = directory/'download-manifest.json'
    if manifest.is_file():
        cached = json.loads(manifest.read_text(encoding='utf-8'))
        if (cached.get('request') != metadata or cached.get('environment') != env_name
                or cached.get('source_policy') != SOURCE_POLICY):
            raise InputError('已有下载记录属于其他请求。')
        if not cached.get('files'):
            raise InputError('已有下载记录为空。')
        for item in cached['files']:
            path = directory/item['relative_path']
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) or digest(path) != item['sha256']:
                raise InputError('已有下载文件缺失或哈希变化。')
        return cached
    if directory.exists() and any(directory.iterdir()) and not (directory/PLAN_NAME).is_file():
        raise InputError('下载目录存在未完成记录；请保留诊断并为重试选择独立目录。')
    discovery = archive_query_metadata(metadata)
    if selected is None:
        username = cloud.load_settings(Path(settings['cloud_settings_path']))['username']
        selected = {name:query_class(discovery,name,username,environment=environment) for name in LOG_CLASSES}
    if (not isinstance(selected,dict) or set(selected)!=set(LOG_CLASSES)
            or any(not isinstance(items,list) or len(items)>MAX_FILES_PER_CLASS for items in selected.values())):
        raise InputError('AD 预查询候选结构无效。')
    for items in selected.values():
        if any(not isinstance(item,dict) or str(item.get('vin') or '').upper()!=metadata['vin'].upper() for item in items):
            raise InputError('AD 预查询 VIN 缺失或不一致。')
    if not any(selected.values()):
        raise InputError(f'{env_name} 缺少本时段所需 AD 原始日志；已选环境不变，不跨环境补齐。')
    declared = sum(int(i.get('fileSize') or 0) for rows in selected.values() for i in rows)
    if declared > MAX_BYTES:
        raise InputError('原始输入超过 20 GiB 试运行上限。')
    jobs = []; entries = []
    for log_class in LOG_CLASSES:
        items = selected[log_class]
        for index,item in enumerate(items,1):
            url = item.get('downloadURL') or item.get('downloadUrl') or item.get('url')
            if not url:
                raise InputError('车云文件记录没有下载地址。')
            name = portable_filename(item,index)
            path = directory/log_class/f'item-{index:04d}'/name
            jobs.append((item, str(url), path, log_class))
            entries.append(plan_entry(item, path.relative_to(directory).as_posix(), log_class))
    budget = DownloadBudget(MAX_BYTES)
    checkpoint = DownloadCheckpoint(directory.absolute(), request=metadata, environment=env_name,
        source_policy=SOURCE_POLICY, entries=entries, execution_release=settings.get('execution_release'),
        analysis_profile=_analysis_profile(settings.get('local_analysis')), byte_budget=budget)
    reused_count = len(checkpoint.verified)

    def transfer(job):
        item, url, path, log_class = job
        relative = path.relative_to(directory).as_posix()
        existing = checkpoint.reuse(relative)
        if existing is not None:
            return existing
        checkpoint.ensure_missing(relative)
        began = perf_counter()
        count = cloud.download_to_file(url,path,environment=environment,max_bytes=2*1024**3,byte_budget=budget)
        downloaded_at = perf_counter()
        if item.get('fileSize') is not None and int(item['fileSize']) != count:
            raise InputError('下载字节数与车云记录不一致。')
        _, checksum = verify_file(path, expected_size=count)
        validated_at = perf_counter()
        record = {'relative_path':relative,
                'bytes':count,'sha256':checksum,'log_class':log_class,
                'timings':{'download_seconds':round(downloaded_at-began,6),
                           'zip_validation_seconds':round(validated_at-downloaded_at,6),
                           'hash_seconds':round(perf_counter()-validated_at,6)}}
        checkpoint.commit(record)
        return record

    files = run_bounded(jobs, transfer, workers)
    total_bytes = sum(item['bytes'] for item in files)
    if total_bytes > MAX_BYTES:
        raise InputError('原始输入超过 20 GiB 试运行上限。')
    result = {'environment':env_name,'request':metadata,'archive_query':discovery,'source_policy':SOURCE_POLICY,
              'counts':{k:len(v) for k,v in selected.items()},
              'bytes':total_bytes,'files':files,
            'timings':{'prepare_seconds':round(perf_counter()-started,6),'download_workers':workers,
                         'reused_files':reused_count,
                         'download_file_seconds':round(sum(f['timings']['download_seconds'] for f in files),6),
                         'zip_validation_file_seconds':round(sum(f['timings']['zip_validation_seconds'] for f in files),6)}}
    write_json(manifest,result)
    return result


def rules_snapshot(settings, directory, run_lark):
    cfg = validate_config(settings)
    path = Path(directory)/'rules.json'
    if path.is_file():
        return path
    if cfg.get('rules_json'):
        source = Path(cfg['rules_json'])
        rules = json.loads(source.read_text(encoding='utf-8-sig'))
    else:
        token = str(cfg['rules_spreadsheet_token'])
        info = run_lark(settings,['sheets','+workbook-info','--spreadsheet-token',token,'--as','user'])['data']
        sheets = info.get('sheets') or []
        if not sheets:
            raise InputError('统计规则表没有工作表。')
        sheet = sheets[0]
        rows = int(sheet.get('row_count') or 0)
        if not 1 < rows <= 10000:
            raise InputError('统计规则表行数无法确认。')
        payload = run_lark(settings,['sheets','+table-get','--spreadsheet-token',token,'--sheet-id',sheet['sheet_id'],
                                     '--range',f'A1:H{rows}','--max-chars','1000000','--as','user'])['data']
        table = payload.get('sheets',[{}])[0]
        if payload.get('truncated') or table.get('truncated'):
            raise InputError('统计规则表读取被截断。')
        expected = ['value_name','theme','soc_type','log_tag','func','input','calculate_type','ps_type']
        if table.get('columns') != expected or not isinstance(table.get('data'),list):
            raise InputError('统计规则表结构变化，需要适配最新版规则读取。')
        rules = {}
        for values in table['data']:
            fields = dict(zip(expected,[str(v).strip() if v is not None else '' for v in values]+['']*8))
            name,theme = fields.pop('value_name'),fields.pop('theme')
            if name or theme:
                rules.setdefault(theme,{})[name] = fields
    if not isinstance(rules,dict) or not rules:
        raise InputError('规则快照必须是完整、非空的 theme→metric→fields 字典。')
    write_json(path,rules)
    return path


def _validate_source_quality(report):
    quality = report.get('source_quality')
    if quality is None:
        # Older strict-frame caches have no quality annotation. Any new
        # normalization audit must keep the top-level annotation with it.
        if report.get('normalization',{}).get('quality') is not None:
            raise InputError('完整统计丢失输入质量标记。')
        return None
    if (not isinstance(quality,dict) or quality.get('status') not in ('ok','partial')
            or not isinstance(quality.get('issues'),list)
            or any(not isinstance(issue,dict) for issue in quality['issues'])
            or bool(quality['issues']) != (quality['status']=='partial')
            or report.get('normalization',{}).get('quality') != quality):
        raise InputError('完整统计的输入质量或归整审计不一致。')
    if quality['status']=='partial':
        if report.get('status') != 'partial':
            raise InputError('来源不完整的统计必须明确标记 partial。')
        for row in report.get('statistics',[]):
            if isinstance(row,dict) and 'XVM_' in str((row.get('rule') or {}).get('soc_type','')):
                if (row.get('source_quality') != quality
                        or not isinstance(row.get('source_quality_diagnostic'),str)
                        or not row['source_quality_diagnostic']):
                    raise InputError('XVM 统计丢失来源不完整的质量标记。')
    return quality


def _attach_source_quality(report, normalized):
    """Keep the complete input audit with the statistics, without editing values."""
    quality = normalized.get('quality')
    if quality is None:
        raise InputError('原生归整缺少输入质量审计。')
    report['source_quality'] = quality
    report['normalization'] = normalized
    if quality.get('status') == 'partial':
        report['status'] = 'partial'
        for row in report['statistics']:
            if 'XVM_' in str((row.get('rule') or {}).get('soc_type','')):
                row['source_quality'] = quality
                row['source_quality_diagnostic'] = (
                    'XVM/MCU 来源存在未结束的压缩帧；仅保留已解码完整行，'
                    '统计结果为 partial。原生统计数值未修改，请结合来源审计核查完整性。')
    _validate_source_quality(report)


def _project_native_metrics(report, metadata, *, expected_profile=None):
    """Derive the comparison view from the same full statistics used by XLSX."""
    expected = re.sub(r'\D','',metadata['start'])+'/'+re.sub(r'\D','',metadata['end'])
    if not isinstance(report,dict):
        raise InputError('完整统计不是有效对象。')
    identity, scope = report.get('metadata'), report.get('analysis_scope')
    metrics = report.get('metrics')
    if (not isinstance(identity,dict) or identity.get('vin') != metadata['vin']
            or identity.get('time_range') != expected or not isinstance(scope,dict)
            or scope.get('profile') != (expected_profile or _analysis_profile()) or not isinstance(report.get('statistics'),list)
            or report.get('status') not in ('ok','partial') or not isinstance(metrics,dict) or not metrics):
        raise InputError('完整统计的车辆、时段、状态或解析范围不一致。')
    _validate_source_quality(report)
    projected = {}
    for scenario,row in metrics.items():
        if not isinstance(row,dict) or 'p99_ms' not in row or 'frame_rate_hz' not in row:
            raise InputError('完整统计缺少可验证的六指标投影。')
        projected[scenario] = {**row,'status':'ok' if row['p99_ms'] is not None else 'missing',
                               'frame_rate_status':'ok' if row['frame_rate_hz'] is not None else 'missing'}
    return projected


def _validated_field_presence(report):
    """Accept only target CSV evidence bound to the completed full runtime."""
    presence = report.get('field_presence')
    if presence is None:
        return None
    from m100_attachment_worker import TARGET_METRICS
    from runtime_audit import canonical_sha
    runtime = report.get('runtime_execution') or {}
    if (not isinstance(presence,dict) or set(presence) != set(TARGET_METRICS)
            or presence != report.get('coverage',{}).get('field_presence')):
        raise InputError('原生字段识别证据与完整统计不一致。')
    for name,item in presence.items():
        if not isinstance(item,dict):
            raise InputError('原生字段识别证据结构无效。')
        unsigned = {key:value for key,value in item.items() if key != 'evidence_sha256'}
        numeric, empty, count = (item.get(key) for key in
                                ('numeric_observations','empty_observations','observations'))
        files = item.get('csv_files')
        if (any(type(value) is not int or value < 0 for value in (numeric,empty,count))
                or count != numeric + empty or type(item.get('scan_complete')) is not bool
                or item.get('status') != ('parsed' if item['scan_complete'] and count else 'unparsed')
                or item.get('scope') != 'requested_window'
                or item.get('evidence') != 'audited_native_target_csv_value_and_state'
                or item.get('request_time_range') != runtime.get('request_time_range')
                or item.get('rules_sha256') != runtime.get('rules_sha256')
                or item.get('runtime_execution_sha256') != canonical_sha(runtime)
                or item.get('evidence_sha256') != canonical_sha(unsigned)
                or not isinstance(files,list) or count and not files):
            raise InputError('原生字段识别证据的窗口、规则或摘要无效。')
        matched = [row for row in report.get('statistics',[])
                   if str(row.get('metric','')).casefold() == name]
        if len(matched) > 1:
            raise InputError('原生字段识别证据的目标规则不唯一。')
        if matched:
            row = matched[0]
            binding = {'theme':row.get('theme'),'metric':row.get('metric'),'rule':row.get('rule')}
            if item.get('rule_sha256') != canonical_sha(binding):
                raise InputError('原生字段识别证据与目标规则绑定不一致。')
        elif count:
            raise InputError('原生字段识别证据缺少目标规则。')
        for file in files:
            relative = file.get('relative_path') if isinstance(file,dict) else None
            if (not isinstance(relative,str) or '\\' in relative
                    or any(part in ('','.','..') for part in relative.split('/'))
                    or len(relative.split('/')) != 3 or relative.split('/')[0] != 'logs_traned'
                    or not matched or relative.split('/')[1] != matched[0].get('theme')
                    or Path(relative).stem.split('+')[-1].casefold() != name
                    or type(file.get('bytes')) is not int or file['bytes'] < 0
                    or not re.fullmatch(r'[0-9a-f]{64}',str(file.get('sha256') or ''))):
                raise InputError('原生字段识别证据的CSV内容绑定无效。')
    return presence


def cached_result(metadata, directory, rules, *, environment='test', execution_settings=None):
    env_name = provider(environment)
    directory = Path(directory)
    cached = directory/'native-result.json'
    if cached.is_file():
        data = json.loads(cached.read_text(encoding='utf-8'))
        if (data.get('request') != metadata or data.get('rules_sha256') != digest(rules)
                or data.get('environment') != env_name or data.get('source',{}).get('environment') != env_name
                or data.get('source_policy') != SOURCE_POLICY
                or data.get('analysis_scope',{}).get('profile') != _analysis_profile(execution_settings)):
            raise InputError('已有本地结果的请求、规则或解析范围不一致；请使用本版新任务目录。')
        windows_binding = None
        if execution_settings and execution_settings.get('execution') == 'windows':
            from native_windows_host import validate_binding
            try:
                windows_binding = validate_binding(data.get('windows_host_runtime'), execution_settings)
            except ValueError as error:
                raise InputError('Windows 缓存执行环境绑定不一致。') from error
        elif data.get('windows_host_runtime') is not None or data.get('windows_execution') is not None:
            raise InputError('Windows 缓存需要明确的当前 Windows 执行配置，不能作为 Linux/WSL 结果认领。')
        artifacts = (data.get('artifact', {}), data.get('full_statistics', {}))
        if not data.get('metrics') or any(not Path(a.get('path','')).is_file() or
                not Path(a['path']).resolve().is_relative_to(directory.resolve()) or
                Path(a['path']).is_symlink() or digest(a['path']) != a.get('sha256') for a in artifacts):
            raise InputError('已有本地结果不完整。')
        detail = data['full_statistics']
        report = json.loads(Path(detail['path']).read_text(encoding='utf-8'))
        selection=report.get('source_selection')
        if (not isinstance(selection,dict) or selection.get('policy')!=_selection_policy_for(execution_settings)
                or data.get('source_selection')!=selection):
            raise InputError('已有结果没有本版按来源选边界的执行审计；请使用新任务目录。')
        try:
            _validate_source_selection(selection,execution_settings)
        except ValueError as exc:
            raise InputError('已有结果的分源执行审计无效；请使用新任务目录。') from exc
        projected = _project_native_metrics(report,metadata,expected_profile=_analysis_profile(execution_settings))
        _validate_native_execution(report,metadata,rules,windows_binding=windows_binding)
        if (data.get('runtime_execution')!=report.get('runtime_execution')
                or data.get('analyzer_contract')!=report.get('analyzer_contract')
                or data.get('execution_compat')!=report.get('execution_compat')
                or data.get('execution_compat_runtime')!=report.get('execution_compat_runtime')):
            raise InputError('已有结果的实际执行审计与完整统计不一致。')
        if data.get('field_presence') != _validated_field_presence(report):
            raise InputError('已有结果的字段识别证据与完整统计不一致。')
        if windows_binding is not None and (data.get('windows_execution') != report.get('windows_execution')
                or report.get('windows_host_runtime') != windows_binding):
            raise InputError('Windows 缓存工具审计与完整统计不一致。')
        quality = report.get('source_quality')
        if (data.get('source',{}).get('sha256') != detail['sha256']
                or detail.get('bytes') != Path(detail['path']).stat().st_size
                or data.get('metrics') != projected or data.get('status') != report['status']
                or data.get('analysis_scope') != report['analysis_scope']
                or data.get('source_quality') != quality
                or data.get('normalization',{}).get('quality') != quality
                or data.get('metric_input_diagnostics') != report.get('metric_input_diagnostics')
                or quality is not None and data.get('normalization') != report.get('normalization')):
            raise InputError('已有本地结果的指标、状态、解析范围或来源摘要与完整统计不一致。')
        return data
    return None


def _validate_native_execution(report,metadata,rules,*,windows_binding=None):
    expected=re.sub(r'\D','',metadata['start'])+'/'+re.sub(r'\D','',metadata['end'])
    inventory=(report.get('source_selection') or {}).get('input_inventory')
    try:
        validate_contract_audit(report.get('analyzer_contract'))
        validate_execution_compat_audit(report.get('execution_compat'))
        validate_execution_compat_runtime_audit(report.get('execution_compat_runtime'),
            request_time_range=expected,
            contract_fingerprint=report['analyzer_contract']['contract_fingerprint'])
        validate_runtime_execution(report.get('runtime_execution'),request_time_range=expected,
                                   inventory=inventory,
                                   rules=json.loads(Path(rules).read_text(encoding='utf-8-sig')))
    except (ValueError,TypeError,KeyError) as exc:
        raise InputError('原生实际执行审计无效；禁止接受仅有正确标签的统计结果。') from exc
    from native_windows_host import validate_execution_audit, validate_parsing_scheduler
    if windows_binding is not None:
        try:
            if report.get('windows_host_runtime') != windows_binding:
                raise ValueError('Windows runtime binding mismatch')
            validate_execution_audit(report.get('windows_execution'), windows_binding)
            validate_parsing_scheduler(report['windows_execution'].get('parsing_scheduler'),
                (report.get('execution_compat_runtime') or {}).get('session',{}).get('pools'))
        except (ValueError,TypeError,KeyError) as error:
            raise InputError('Windows 实际工具执行审计无效。') from error
    elif report.get('windows_host_runtime') is not None or report.get('windows_execution') is not None:
        raise InputError('Windows 执行审计不能用于未声明的执行环境。')
    from native_sheet_data import validate_native_sheet_data
    try:
        sheet = report.get('native_sheet_data')
        validate_native_sheet_data(sheet)
        identity = sheet.get('metadata') or {}
        if identity.get('request') != metadata or identity.get('rules_sha256') != digest(rules):
            raise ValueError('native sheet request/rules binding mismatch')
    except (ValueError,TypeError,KeyError) as exc:
        raise InputError('无图原生明细数据与本次窗口或规则不一致。') from exc
    _validated_field_presence(report)


def analyze_window(metadata, settings, directory, rules, *, prepared=None, environment='test'):
    began = perf_counter()
    env_name = provider(environment)
    cfg = validate_config(settings)
    windows_binding = None
    if cfg.get('execution') == 'windows':
        from native_windows_host import runtime_binding
        windows_binding = runtime_binding(cfg)
    directory = Path(directory)
    cached = directory/'native-result.json'
    data = cached_result(metadata,directory,rules,environment=environment,execution_settings=cfg)
    if data is not None:
        cleanup_inputs(directory,settings)
        return data
    if prepared is None:
        downloaded = fetch_window(metadata,settings,directory/'input',environment=environment)
    else:
        # The cloud-first pipeline owns fetching. This branch never queries.
        downloaded = json.loads((directory/'input/download-manifest.json').read_text(encoding='utf-8'))
        if (downloaded != prepared or downloaded.get('request') != metadata
                or downloaded.get('source_policy') != SOURCE_POLICY or downloaded.get('environment') != env_name):
            raise InputError('预备 AD 输入清单与当前请求不一致。')
        for item in downloaded['files']:
            path = directory/'input'/item['relative_path']
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to((directory/'input').resolve()) or digest(path)!=item['sha256']:
                raise InputError('预备 AD 输入缺失或哈希变化。')
    from native_inputs import prepare_native_inputs
    verified_at = perf_counter()
    normalized = prepare_native_inputs(directory/'input',directory/'native-prepared',downloaded,
                                       allow_incomplete_frames=True)
    normalized_at = perf_counter()
    execution = cfg.get('execution', 'wsl' if os.name == 'nt' else 'linux')
    convert = wsl_path if execution == 'wsl' else lambda value: str(Path(value).resolve())
    arguments = [cfg['python'], '-X', 'utf8', '-B', convert(HERE/'run_m100.py'),
                 '--analyzer-root', cfg['analyzer_root'], '--log-dir', convert(directory/'native-prepared'),
                 '--start', re.sub(r'\D','',metadata['start']), '--end', re.sub(r'\D','',metadata['end']),
                 '--expected-vin', metadata['vin'], '--rules-json', convert(rules),
                 '--work-root', convert(directory/'analysis'), '--timeout', str(cfg.get('timeout_seconds',7200)), '--execute']
    if execution == 'windows':
        arguments += ['--native-execution','windows','--source-identity','namespace_v3']
    command = ([str(cfg.get('wsl_executable') or 'wsl.exe'), '-d', cfg['distribution'], '--exec', *arguments]
               if execution == 'wsl' else arguments)
    if execution == 'windows':
        from native_windows_host import execution_environment, validate_binding
        from native_windows_process import run_windows_tree
        completed = run_windows_tree(command, env=execution_environment(cfg),
            timeout=cfg.get('timeout_seconds',7200)+120)
        validate_binding(windows_binding,cfg)
    else:
        completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                   timeout=cfg.get('timeout_seconds',7200)+120)
    executed_at = perf_counter()
    (directory/'native-stdout.json').write_text(completed.stdout or '',encoding='utf-8')
    (directory/'native-stderr.log').write_text(completed.stderr or '',encoding='utf-8')
    try:
        native = json.loads(completed.stdout)
    except ValueError:
        raise InputError('完整分析器未返回有效 JSON；查看本任务 native-stderr.log。') from None
    if not isinstance(native,dict):
        raise InputError('完整分析器返回的结果结构无效，未生成报告。')
    if completed.returncode not in (0,2) or native.get('status') not in ('ok','partial'):
        from native_analysis_failure import describe
        message = describe(native)
        if message:
            raise InputError(message)
        raise InputError('完整原生解析失败；保留诊断，不改用四指标结果。')
    artifact = native.get('artifact') or {}
    artifact_path = host_path(artifact.get('path') or '')
    analysis_root = (directory/'analysis').resolve()
    if (not artifact_path.is_file() or artifact_path.is_symlink()
            or not artifact_path.resolve().is_relative_to(analysis_root)
            or digest(artifact_path) != artifact.get('sha256')):
        raise InputError('完整分析器返回的工件路径或 SHA256 无效。')
    detail_path = artifact_path.with_name('report-data.json')
    data = json.loads(detail_path.read_text(encoding='utf-8'))
    selection=data.get('source_selection')
    if not isinstance(selection,dict) or selection.get('policy')!=_selection_policy_for(cfg):
        raise InputError('完整分析器未确认按来源选边界，禁止退回旧执行方式。')
    try:
        _validate_source_selection(selection,cfg)
    except ValueError as exc:
        raise InputError('完整分析器的分源执行审计校验失败。') from exc
    if windows_binding is not None:
        data['windows_host_runtime'] = windows_binding
    projected = _project_native_metrics(data,metadata,expected_profile=_analysis_profile(cfg))
    _validate_native_execution(data,metadata,rules,windows_binding=windows_binding)
    _attach_source_quality(data,normalized)
    from native_sheet_data import bind_native_sheet_metadata
    data['native_sheet_data'] = bind_native_sheet_metadata(
        data['native_sheet_data'],{'request':metadata,'environment':env_name,'rules_sha256':digest(rules)},
        statistics=data['statistics'])
    scope = data['analysis_scope']
    compact_html = directory/'native-report.html'
    compact_data = directory/'report-data.json'
    shutil.copyfile(artifact_path,compact_html)
    write_json(compact_data,data)
    if artifact_path.parent.joinpath('upstream.log').is_file():
        shutil.copyfile(artifact_path.parent/'upstream.log',directory/'native-upstream.log')
    missing_sources = [name for name in LOG_CLASSES if not downloaded['counts'].get(name)]
    result={'status':data['status'],'request':metadata,'metrics':projected,'environment':env_name,
            'source_policy':SOURCE_POLICY,'archive_query':downloaded['archive_query'],
            'source':{'kind':'full_native_statistics','environment':env_name,'sha256':digest(compact_data)},
            'rules_sha256':digest(rules),'artifact':{'path':str(compact_html),'sha256':digest(compact_html)},
            'full_statistics':{'path':str(compact_data),'sha256':digest(compact_data),'bytes':compact_data.stat().st_size},
            'analysis_scope':scope,'native_provenance':native.get('upstream_source_sha256',{}),
            'source_selection':data.get('source_selection'),
            'runtime_execution':data.get('runtime_execution'),
            'analyzer_contract':data.get('analyzer_contract'),
            'execution_compat':data.get('execution_compat'),
            'execution_compat_runtime':data.get('execution_compat_runtime'),
            'metric_input_diagnostics':data.get('metric_input_diagnostics'),
            'normalization':normalized,'source_quality':data['source_quality'],
            'coverage':data.get('coverage',{}),
            'timings':{'prepare_and_verify_seconds':round(verified_at-began,6),
                       'normalization_seconds':round(normalized_at-verified_at,6),
                       'native_execution_seconds':round(executed_at-normalized_at,6),
                       'parse_and_statistics_seconds':round(perf_counter()-verified_at,6)},
            'download':{'counts':downloaded['counts'],'bytes':downloaded['bytes'],
                        'timings':downloaded.get('timings',{})},
            'missing_log_classes':missing_sources}
    if data.get('field_presence') is not None:
        result['field_presence'] = data['field_presence']
    if windows_binding is not None:
        result.update(windows_host_runtime=windows_binding,windows_execution=data['windows_execution'])
    write_json(cached,result)
    cleanup_inputs(directory,settings)
    return result
