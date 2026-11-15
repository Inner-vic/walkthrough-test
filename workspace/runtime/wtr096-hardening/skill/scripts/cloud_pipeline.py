"""Schedule complete window jobs; collect both results before comparison."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pathlib import Path
from threading import Lock
from time import perf_counter
from datetime import datetime, timezone

import cloud_log_client as cloud
from native_log_source import digest, write_json, validate_config
from request_model import InputError, comparison_context
from environment_policy import policy
import environment_selection as selection
from window_pipeline import process_window, source_plan, validate_result

VERSION=selection.PER_WINDOW_PIPELINE_VERSION


def run_windows(windows,settings,directory,rules,*,on_stage=None):
    started=perf_counter()
    cfg=validate_config(settings)
    environment_policy=policy(settings)
    if not isinstance(windows,dict) or set(windows)!=set(selection.ROLES):
        raise InputError('统一数据计划的时段角色无效。')
    windows={role:comparison_context(metadata,role) for role,metadata in windows.items()}
    if len({metadata['vin'] for metadata in windows.values()})!=1:
        raise InputError('统一数据计划拒绝跨 VIN。')
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/'pipeline-manifest.json'
    rules_sha256=digest(rules)
    if path.is_file():
        old=json.loads(path.read_text(encoding='utf-8'))
        if (old.get('version')!=VERSION or old.get('request')!=windows
                or old.get('rules_sha256')!=rules_sha256
                or old.get('environment_policy')!=environment_policy):
            raise InputError('统一数据计划的身份、版本或规则不一致。')
    workers=min(len(windows),cfg.get('window_workers',2))
    manifest={'version':VERSION,'environment':'unselected','environment_policy':environment_policy,'request':windows,
              'rules_sha256':rules_sha256,'window_workers':workers,
              'windows':{role:{'request':metadata,'stage':'planned','status':'planned','sources':source_plan(metadata)}
                         for role,metadata in windows.items()}}
    lock=Lock()
    stage_started={}
    def progress(stage,role=None,details=None):
        # Also serializes the task-record callback and its fixed temp path.
        with lock:
            now=perf_counter()
            stamp=datetime.now(timezone.utc).isoformat()
            manifest['elapsed_seconds']=round(now-started,6)
            if role is None:
                manifest.update(deepcopy(details or {}))
                manifest['stage']=stage
                event={'scope':'task','stage':stage,'elapsed_seconds':manifest['elapsed_seconds'],
                       **deepcopy(details or {})}
            else:
                state=manifest['windows'][role]
                history=state.setdefault('stage_timings',[])
                if history:
                    history[-1]['elapsed_seconds']=round(now-stage_started[role],6)
                history.append({'stage':stage,'started_at':stamp})
                stage_started[role]=now
                state.update(deepcopy(details or {}))
                state['stage']=stage
                if stage=='preparing_sources':state['status']='running'
                event={'scope':'window','window':role,**deepcopy(state)}
            write_json(path,manifest)
            if on_stage:on_stage(event)

    def run_one(role):
        try:
            result=process_window(windows[role],settings,directory/role,rules,
                                  on_stage=lambda stage,details:progress(stage,role,details),
                                  task_selection=task_selection,role=role,
                                  discovery=discovery[role] if discovery is not None else None)
            validate_result(result,windows[role],rules_sha256,environment_policy=environment_policy)
            proof=selection.window_decision(task_selection,role)
            if (result.get('environment') != proof['provider_environment']
                    or result.get('environment_selection') != proof):
                raise InputError('单窗结果没有绑定本角色的查询证据及选定环境。')
            return result
        except Exception as error:
            details = {'status':'failed'}
            if isinstance(error,cloud.CloudAuthError):
                details['error_diagnostic'] = cloud.auth_failure_diagnostic(error)
            elif isinstance(error,cloud.CloudResponseContractError):
                details['error_diagnostic'] = cloud.response_contract_diagnostic(error)
            elif isinstance(error,cloud.CloudDownloadError):
                details['error_diagnostic'] = cloud.download_failure_diagnostic(error)
            progress('failed',role,details)
            raise

    progress('selecting_task_environment')
    try:
        task_selection,discovery=selection.select_task(windows,settings,directory)
        if task_selection.get('version') != selection.PER_WINDOW_TASK_VERSION:
            raise InputError('新计算必须使用本版逐窗环境证据；不迁移旧任务。')
        selection.validate_task_decision(task_selection,windows,environment_policy)
    except Exception as error:
        details={}
        if isinstance(error,cloud.CloudAuthError):
            details['error_diagnostic']=cloud.auth_failure_diagnostic(error)
        elif isinstance(error,cloud.CloudResponseContractError):
            details['error_diagnostic']=cloud.response_contract_diagnostic(error)
        elif isinstance(error,cloud.CloudDownloadError):
            details['error_diagnostic']=cloud.download_failure_diagnostic(error)
        progress('failed',details=details)
        raise
    manifest.update(environment='per_window',
                    task_environment_selection=deepcopy(task_selection),
                    task_selection_sha256=selection.task_selection_sha256(task_selection))
    for role in windows:
        proof=selection.window_decision(task_selection,role)
        manifest['windows'][role]['environment']=proof['provider_environment']
        manifest['windows'][role]['sources']=source_plan(windows[role],proof['environment'])
    progress('processing_windows')
    results={};errors=[]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures={executor.submit(run_one,role):role for role in windows}
        for future in as_completed(futures):
            role=futures[future]
            try:results[role]=future.result()
            except Exception as error:errors.append(error)
    if errors:
        progress('failed')
        # Preserve ordinary completion ordering, but do not hide a proven auth
        # failure behind another window's earlier generic source failure.
        raise next((error for error in errors if isinstance(error,cloud.CloudAuthError)),
                   next((error for error in errors if isinstance(error,cloud.CloudResponseContractError)),
                        next((error for error in errors if isinstance(error,cloud.CloudDownloadError)), errors[0])))
    progress('metrics_ready')
    result={'version':VERSION,'environment':'per_window',
            'environment_policy':environment_policy,
            'task_environment_selection':deepcopy(task_selection),
            'task_selection_sha256':selection.task_selection_sha256(task_selection),
            'windows':{role:results[role] for role in windows},
            'source_manifest_sha256':digest(path),'source_manifest':manifest}
    return selection.validate_task_pipeline(result,windows,environment_policy)
