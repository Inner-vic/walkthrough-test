"""Role-independent processing of ONE complete vehicle-log time window."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from threading import Lock
from time import perf_counter

import native_log_source as ad
import service_log_source as pcm
import environment_selection as selection
from environment_policy import policy, provider
from request_model import InputError

RESULT_VERSION=selection.WINDOW_RESULT_VERSION


class _PcmQueryError(InputError):
    """A renewed discovery failed; it must never become optional missing data."""


def source_plan(metadata,environment=None):
    env_name=provider(environment) if environment is not None else None
    return {
        'ad':{'status':'planned','environment':env_name,'log_classes':list(ad.LOG_CLASSES),
              'query':ad.archive_query_metadata(metadata),'source_policy':ad.SOURCE_POLICY},
        'pcm':{'status':'planned','environment':env_name,'log_classes':[pcm.LOG_CLASS],
               'query':dict(metadata),'source_policy':pcm.POLICY}}


def pcm_failure(environment):
    return {'status':'failed','metrics':{},
            'source':{'kind':pcm.LOG_CLASS,'environment':provider(environment)},
            'error':'同环境 PCM/CPU 日志获取或解析失败；指标保留缺失。'}


@dataclass
class PreparedWindow:
    metadata: dict
    directory: Path
    ad_cache: dict | None
    ad_input: dict | None
    pcm_cache: dict | None
    pcm_input: dict | None
    pcm_error: dict | None
    sources: dict
    environment: str
    environment_selection: dict


def _resolve_window(metadata,settings,directory,rules,*,task_selection=None,role=None,discovery=None):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    if task_selection is None:
        if role is not None or discovery is not None:
            raise InputError('时段环境参数必须绑定完整任务级选择证据。')
        decision,discovery=selection.select_window(metadata,settings,directory)
    else:
        if not isinstance(task_selection,dict) or task_selection.get('version') != selection.PER_WINDOW_TASK_VERSION:
            raise InputError('新时段处理必须绑定本版逐窗环境证据；不迁移旧任务。')
        decision=selection.bind_task_window(task_selection,role,metadata,settings,directory)
    environment=decision['environment']
    native_cache=ad.cached_result(metadata,directory,rules,environment=environment,
                                execution_settings=settings['local_analysis'])
    pcm_cache=pcm.cached_result(metadata,directory,environment=environment)
    return PreparedWindow(dict(metadata),directory,native_cache,None,pcm_cache,None,None,
                          source_plan(metadata,environment),environment,decision),discovery


def _prepare_ad(prepared,settings,discovery):
    if prepared.ad_cache is None:
        prepared.ad_input=ad.fetch_window(prepared.metadata,settings,prepared.directory/'input',
            environment=prepared.environment,selected=discovery['ad'] if discovery is not None else None)


def _prepare_pcm(prepared,settings,discovery):
    metadata=prepared.metadata;directory=prepared.directory;environment=prepared.environment
    pcm_cache=prepared.pcm_cache
    pcm_input=None;pcm_error=None
    if pcm_cache is None:
        if discovery is not None and discovery['pcm_error']:
            pcm_error=pcm_failure(environment)
        else:
            selected=discovery['pcm'] if discovery is not None else None
            # Resuming a selected task can require renewed signed URLs. Keep
            # this query outside the optional download/parse failure path:
            # network, authorization and response errors never mean no data.
            if selected is None and not (directory/'service-input/download-manifest.json').is_file():
                try:selected=pcm.discover(metadata,settings,environment=environment)
                except (ad.cloud.CloudAuthError, ad.cloud.CloudResponseContractError):raise
                except (OSError,RuntimeError,ValueError):
                    raise _PcmQueryError('本时段 PCM/CPU 来源查询失败；已保留环境选择，未切换环境。') from None
            try:pcm_input=pcm.prepare(metadata,settings,directory,environment=environment,
                                     selected=selected)
            except (ad.cloud.CloudAuthError, ad.cloud.CloudResponseContractError):raise
            except ad.cloud.CloudDownloadError as error:
                pcm_error={**pcm_failure(environment),
                           'error_diagnostic':ad.cloud.download_failure_diagnostic(error)}
            except (OSError,RuntimeError,ValueError):pcm_error=pcm_failure(environment)
    prepared.pcm_input=pcm_input;prepared.pcm_error=pcm_error


def _ad_source(prepared):
    cache=prepared.ad_cache;actual=prepared.ad_input
    return {**source_plan(prepared.metadata,prepared.environment)['ad'],
            'status':'cached' if cache is not None else 'ready',
            'counts':(actual if actual is not None else cache.get('download',{})).get('counts',{}),
            'input_sha256':ad.digest(prepared.directory/'input/download-manifest.json') if actual is not None else None,
            'result_source_sha256':cache.get('source',{}).get('sha256') if cache is not None else None}


def _pcm_source(prepared):
    cache=prepared.pcm_cache;actual=prepared.pcm_input;error=prepared.pcm_error
    cached_source=(cache or {}).get('source',{})
    result={**source_plan(prepared.metadata,prepared.environment)['pcm'],
            'status':'cached' if cache is not None else 'failed' if error is not None else 'ready',
            'counts':actual.get('counts',{}) if actual is not None else {pcm.LOG_CLASS:cached_source.get('selected_files',0)},
            'input_sha256':ad.digest(prepared.directory/'service-input/download-manifest.json') if actual is not None else cached_source.get('input_manifest_sha256')}
    if error is not None and 'error_diagnostic' in error:
        result['error_diagnostic']=error['error_diagnostic']
    return result


def prepare_window(metadata,settings,directory,rules,*,task_selection=None,role=None,discovery=None):
    """Compatibility API: synchronously prepare both parsers' local inputs."""
    prepared,discovery=_resolve_window(metadata,settings,directory,rules,
        task_selection=task_selection,role=role,discovery=discovery)
    _prepare_ad(prepared,settings,discovery)
    _prepare_pcm(prepared,settings,discovery)
    prepared.sources={'ad':_ad_source(prepared),'pcm':_pcm_source(prepared)}
    return prepared


def _parse_pcm(prepared,settings,*,on_complete=None):
    metadata=prepared.metadata;directory=prepared.directory;environment=prepared.environment
    if prepared.pcm_cache is not None:
        if on_complete:on_complete()
        return prepared.pcm_cache
    if prepared.pcm_error is not None:return prepared.pcm_error
    try:
        result=pcm.parse(metadata,settings,directory,prepared.pcm_input,environment=environment)
        if on_complete:on_complete()
        return result
    except (ad.cloud.CloudAuthError,ad.cloud.CloudResponseContractError):raise
    except ad.cloud.CloudDownloadError as error:
        return {**pcm_failure(environment),'error_diagnostic':ad.cloud.download_failure_diagnostic(error)}
    except (OSError,RuntimeError,ValueError):return pcm_failure(environment)


def _window_result(prepared,rules,native,service):
    return {
        'version':RESULT_VERSION,'request':dict(prepared.metadata),'environment':provider(prepared.environment),
        'environment_selection':prepared.environment_selection,'rules_sha256':ad.digest(rules),
        'status':'ok' if native.get('status')=='ok' and service.get('status')=='ok' else 'partial',
        'ad':native,'pcm':service,'sources':prepared.sources}


def parse_window(prepared,settings,rules,emit):
    """Consume only prepared local inputs, AD followed by PCM/CPU."""
    metadata=prepared.metadata;directory=prepared.directory;environment=prepared.environment
    emit('parsing_ad',{})
    native=prepared.ad_cache if prepared.ad_cache is not None else ad.analyze_window(
        metadata,settings,directory,rules,prepared=prepared.ad_input,environment=environment)
    emit('parsing_pcm',{})
    service=_parse_pcm(prepared,settings)
    if prepared.ad_cache is not None:ad.cleanup_inputs(directory,settings)
    if prepared.pcm_cache is not None:pcm.cleanup_inputs(directory,settings)
    return _window_result(prepared,rules,native,service)


def validate_result(result,metadata,rules_sha256,*,environment_policy=None):
    if (not isinstance(result,dict) or result.get('version')!=RESULT_VERSION
            or result.get('request')!=metadata or result.get('environment') not in ('testtwo','prod')
            or result.get('rules_sha256')!=rules_sha256
            or result.get('status') not in ('ok','partial')
            or not isinstance(result.get('ad'),dict) or not isinstance(result.get('pcm'),dict)
            or not isinstance(result.get('sources'),dict)):
        raise InputError('单时段结果的身份、环境、规则或结构不一致，拒绝比较。')
    decision=result.get('environment_selection')
    chosen_policy=environment_policy if environment_policy is not None else (decision or {}).get('policy')
    selection.validate_decision(decision,metadata,chosen_policy)
    if (decision['provider_environment']!=result['environment']
            or result['ad'].get('environment')!=result['environment']
            or result['ad'].get('source',{}).get('environment')!=result['environment']
            or result['pcm'].get('source',{}).get('environment')!=result['environment']):
        raise InputError('同一时段的选择记录、AD 与 PCM/CPU 环境不一致。')
    expected=source_plan(metadata,decision['environment'])
    for name in ('ad','pcm'):
        source=result['sources'].get(name)
        if not isinstance(source,dict) or any(source.get(key)!=expected[name][key] for key in ('environment','log_classes','query','source_policy')):
            raise InputError('单时段结果的来源类型、查询窗口或策略不一致，拒绝比较。')
    if result['ad'].get('status')=='failed':
        raise InputError('单时段必要 AD 计算失败，拒绝比较。')


def process_window(metadata,settings,directory,rules,*,on_stage=None,task_selection=None,role=None,discovery=None):
    """After AD downloads, overlap native analysis with PCM prepare/parse.

    Each window still has at most one four-file download pool. Parser cleanup
    is deferred to this controller, after the owned future has been drained.
    Only this thread publishes progress; timings retain the real branch spans.
    """
    ad.validate_config(settings)
    def emit(stage,details):
        if on_stage:on_stage(stage,details)
    emit('preparing_sources',{})
    prepared,discovery=_resolve_window(metadata,settings,directory,rules,
        task_selection=task_selection,role=role,discovery=discovery)
    timings={};timing_lock=Lock()

    def timed(name,function,*,cached=False,skipped=False):
        started=perf_counter()
        entry={'started_at':datetime.now(timezone.utc).isoformat(),'status':'running'}
        with timing_lock:timings[name]=entry
        status='failed'
        try:
            value=function()
            status='cached' if cached else 'skipped' if skipped else 'ok'
            return value
        finally:
            with timing_lock:
                entry.update(ended_at=datetime.now(timezone.utc).isoformat(),
                             elapsed_seconds=round(perf_counter()-started,6),status=status)

    def details():
        with timing_lock:observed=deepcopy(timings)
        return {'sources':deepcopy(prepared.sources),'environment':provider(prepared.environment),
                'environment_selection':deepcopy(prepared.environment_selection),'source_timings':observed}

    try:
        timed('ad_fetch',lambda:_prepare_ad(prepared,settings,discovery),cached=prepared.ad_cache is not None)
        # Native analysis may remove its raw input tree on success. Freeze this
        # exact manifest binding before either parser is allowed to clean up.
        prepared.sources['ad']=_ad_source(prepared)
    except Exception:
        emit('sources_failed',details())
        raise

    branch_settings=deepcopy(settings)
    branch_settings['local_analysis']['keep_raw_logs']=True
    pcm_prepared=replace(prepared,sources=deepcopy(prepared.sources))
    pcm_state={'source':{**prepared.sources['pcm'],'status':'preparing'},'parsed':False}
    prepared.sources['pcm']=deepcopy(pcm_state['source'])
    emit('processing_sources',details())

    def service_branch():
        try:
            timed('pcm_prepare',lambda:_prepare_pcm(pcm_prepared,branch_settings,discovery),
                  cached=pcm_prepared.pcm_cache is not None)
            pcm_state['source']=_pcm_source(pcm_prepared)
            def completed():pcm_state['parsed']=True
            result=timed('pcm_parse',lambda:_parse_pcm(pcm_prepared,branch_settings,on_complete=completed),
                         cached=pcm_prepared.pcm_cache is not None,
                         skipped=pcm_prepared.pcm_error is not None)
            return result
        except Exception:
            pcm_state['source']['status']='failed'
            raise

    native=service=None
    native_error=service_error=None
    # Even an AD exception or a controller callback failure cannot leave a
    # writer running after process_window returns. No cleanup occurs here.
    with ThreadPoolExecutor(max_workers=1,thread_name_prefix='walkthrough-pcm') as executor:
        service_future=executor.submit(service_branch)
        try:
            native=timed('ad_analyze',lambda:prepared.ad_cache if prepared.ad_cache is not None else ad.analyze_window(
                metadata,branch_settings,prepared.directory,rules,prepared=prepared.ad_input,
                environment=prepared.environment),cached=prepared.ad_cache is not None)
        except Exception as error:
            native_error=error
        try:service=service_future.result()
        except Exception as error:service_error=error
    prepared.sources['pcm']=pcm_state['source']
    if native_error is not None or service_error is not None:
        failure_details=details()
        failure_details['source_outcomes']={'ad':'failed' if native_error is not None else 'ready',
                                           'pcm':'failed' if service_error is not None else 'ready'}
        for name,error in (('ad',native_error),('pcm',service_error)):
            if isinstance(error,ad.cloud.CloudAuthError):
                failure_details['sources'][name]['error_diagnostic']=ad.cloud.auth_failure_diagnostic(error)
            elif isinstance(error,ad.cloud.CloudResponseContractError):
                failure_details['sources'][name]['error_diagnostic']=ad.cloud.response_contract_diagnostic(error)
            elif isinstance(error,ad.cloud.CloudDownloadError):
                failure_details['sources'][name]['error_diagnostic']=ad.cloud.download_failure_diagnostic(error)
        emit('sources_failed',failure_details)
        errors=[error for error in (native_error,service_error) if error is not None]
        # Optional PCM errors are already data above. Proven authorization,
        # response, or renewed-query failures retain fatal semantics if native
        # analysis also failed at the same time.
        raise next((error for error in errors if isinstance(error,ad.cloud.CloudAuthError)),
                   next((error for error in errors if isinstance(error,ad.cloud.CloudResponseContractError)),
                        next((error for error in errors if isinstance(error,_PcmQueryError)),errors[0])))
    emit('sources_ready',details())
    # Deferred, serial cleanup uses the original retention setting. An optional
    # PCM preparation failure keeps its incomplete inputs for diagnosis.
    ad.cleanup_inputs(prepared.directory,settings)
    if pcm_state['parsed']:pcm.cleanup_inputs(prepared.directory,settings)
    result=_window_result(prepared,rules,native,service)
    validate_result(result,metadata,ad.digest(rules),environment_policy=policy(settings))
    # Diagnostic checkpoint. Resume still validates each parser's own cache,
    # including native HTML, instead of trusting this summary file alone.
    checksum=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    target=Path(directory)/'window-result.json'
    ad.write_json(target,{'version':RESULT_VERSION,'result':result,'result_sha256':checksum})
    emit('metrics_ready',{'status':result['status'],'result_sha256':ad.digest(target)})
    return result
