"""Standalone 走查测试: local full analysis and a fixed-user product query."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from copy import deepcopy
from time import perf_counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import report_policy as policy
import cloud_log_client as cloud
from request_model import InputError
from walkthrough_route import route, request_identity, source_normalization_anchor
from native_log_source import rules_snapshot, validate_config, digest, SOURCE_POLICY
from cloud_pipeline import run_windows
from environment_policy import policy as environment_policy
import environment_selection as selection
import native_lifecycle as lifecycle
from performance_comparison import compare_performance
from feishu_report import build_report_document, build_report_notification
from request_context import followup_identity_from_context, RequestContextError
from full_workbook import export_full_workbook, verify_full_workbook
import product_version as product
from native_preflight import check_native_environment
from delivery_config import DELIVERY_FORMAT, validate_delivery_config
from m100_attachment_worker import ANALYSIS_PROFILE
from native_windows_host import analysis_profile
from preflight_diagnostics import PreflightError, preflight_failure_fields

HERE = Path(__file__).resolve().parent
PRESENTATION_VERSION = 'walkthrough-presentation-v2'
DETAIL_LINKS_STYLE = 'source_paragraph'
METRIC_HEADER_STYLE = 'no_ad'


def safe_failure_fields(error):
    """One bounded mapping for persisted task errors and the CLI response."""
    if isinstance(error,PreflightError):
        return preflight_failure_fields(error)
    if isinstance(error,cloud.CloudAuthError):
        diagnostic = cloud.auth_failure_diagnostic(error)
        return {'error':cloud.auth_failure_message(diagnostic),'error_diagnostic':diagnostic}
    if isinstance(error,cloud.CloudResponseContractError):
        diagnostic = cloud.response_contract_diagnostic(error)
        return {'error':cloud.response_contract_message(diagnostic),'error_diagnostic':diagnostic}
    if isinstance(error,cloud.CloudDownloadError):
        diagnostic = cloud.download_failure_diagnostic(error)
        return {'error':cloud.download_failure_message(diagnostic),'error_diagnostic':diagnostic}
    return {'error':str(error) if isinstance(error,InputError) else
            '走查测试处理失败；请查看私有诊断。'}


def load_settings(path):
    settings = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    release = settings.get('execution_release')
    if release in ('walkthrough-target15-lifecycle', 'walkthrough-target16-hardening'):
        lifecycle.validate_policy(settings)
    if release is not None and (not isinstance(release, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', release)):
        raise InputError('走查执行版本标记无效。')
    required = ('workspace_root','runtime_state_dir','cloud_settings_path','lark_cli','lark_home',
                'project_id','lark_profile','report_lark_profile','expected_lark_app_id','expected_operator_open_id')
    if any(not isinstance(settings.get(key),str) or not settings[key].strip() for key in required):
        raise InputError('走查测试独立配置不完整；请从 settings.json.example 创建本机配置。')
    workspace = Path(settings['workspace_root']).resolve()
    runtime = Path(settings['runtime_state_dir']).resolve()
    if not runtime.is_relative_to(workspace) or runtime == workspace:
        raise InputError('走查 runtime 必须位于自己的工作区子目录内。')
    if 'ad-p99-asgard' in runtime.parts or 'ad-p99-analysis' in runtime.parts:
        raise InputError('不能使用旧时延分析的运行目录。')
    home = Path(settings['lark_home']).expanduser()
    if not home.is_absolute() or home.resolve() == Path.home().resolve():
        raise InputError('必须使用项目隔离的飞书身份目录。')
    if not (home/'.lark-cli/config.json').is_file():
        raise InputError('项目隔离飞书身份尚未初始化。')
    prefix = settings['project_id']+'-'
    if any(not settings[key].startswith(prefix) for key in ('lark_profile','report_lark_profile')):
        raise InputError('飞书 profile 必须使用项目命名空间。')
    if settings['lark_profile'] == settings['report_lark_profile']:
        raise InputError('规则读取/L3 用户与报告 Bot 必须隔离。')
    settings['project_isolation_required'] = True
    # The copied credential-replica module retains this compatibility field.
    settings['expected_dispatch_open_id'] = settings['expected_operator_open_id']
    policy.WORKSPACE_ROOT = workspace
    validate_config(settings)
    product.validate_config(settings)
    settings['report_delivery'] = validate_delivery_config(settings)
    return settings


@contextmanager
def phase(path, record, name):
    """Keep sanitized duration evidence even when a phase fails or is resumed."""
    began = perf_counter()
    entry = {'stage': name, 'started_at': datetime.now(timezone.utc).isoformat()}
    record.setdefault('phase_timings', []).append(entry)
    record['execution_stage'] = name
    policy.atomic_json(path, record)
    try:
        yield
    except Exception:
        entry['outcome'] = 'failed'
        raise
    else:
        entry['outcome'] = 'ok'
    finally:
        entry['elapsed_seconds'] = round(perf_counter() - began, 6)
        policy.atomic_json(path, record)


def execution_binding(settings):
    """Bind a task to its fixed identity and complete local analysis settings."""
    return hashlib.sha256(json.dumps([
        product.validate_config(settings), settings.get('lark_profile'), settings.get('lark_home'),
        settings.get('expected_lark_app_id'), settings.get('expected_operator_open_id'),
        settings.get('local_analysis')], sort_keys=True).encode()).hexdigest()


def validate_execution_release(settings, record):
    """Reject old or changed execution before authorization, locks or writes."""
    release = settings.get('execution_release')
    if release is None:
        return  # Frozen releases retain their original entry and result contracts.
    if (not isinstance(release, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', release)
            or record.get('execution_release') != release
            or record.get('execution_binding_sha256') != execution_binding(settings)
            or record.get('analysis_profile') != analysis_profile(settings.get('local_analysis'))):
        raise InputError('任务执行版本或固定配置不一致；保留原任务，不自动重查或重算。')


def start_job(args):
    startup_started = perf_counter()
    identity = request_identity(args.ccconnect_context)
    settings = load_settings(args.settings)
    received_at = source_normalization_anchor(settings,args.ccconnect_context,getattr(args,'received_at',None),identity=identity)
    decision = route(args.text,received_at=received_at,interpretation=getattr(args,'interpretation',None))
    if decision['status'] != 'ready':
        raise InputError(decision['message'])
    request = decision['normalized']
    material = json.dumps([identity,request['baseline_metadata'],request['replacement_metadata']],sort_keys=True)
    fingerprint = hashlib.sha256(material.encode()).hexdigest()
    key = 'walkthrough-'+hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
    runtime = Path(settings['runtime_state_dir'])
    path = runtime/(key+'.json')
    if path.is_file():
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('request_sha256') != fingerprint:
            raise InputError('同一来源消息对应的走查请求已变化，拒绝串用记录。')
        if record.get('execution_binding_sha256'):
            current_binding = execution_binding(settings)
            if record['execution_binding_sha256'] != current_binding:
                raise InputError('同一来源消息的固定执行身份或配置已变化，拒绝串用记录。')
        return {'status':record['status'],'job_key':key,'idempotent':True,'report_url':record.get('report_url')}
    binding = None
    if settings.get('product_version') is not None:
        binding = execution_binding(settings)
    native_readiness = None
    if lifecycle.enabled(settings):
        lifecycle.task_service(settings)
    if settings.get('local_analysis'):
        native_readiness = check_native_environment(settings['local_analysis'])
    worker_env = dict(os.environ, OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    policy.activate_lark_environment(settings)
    policy.ensure_report_bot_authorization(settings)
    authorization = policy.ensure_user_authorization(settings)
    policy.ensure_secure_label_authorization(settings)
    label = policy.resolve_report_secure_label(settings)
    runtime.mkdir(parents=True,exist_ok=True)
    record = {'job_key':key,'kind':'walkthrough_test_local','status':'queued','request_sha256':fingerprint,
              'request':request,'vin':request['vin'],**identity,
              'presentation_version':PRESENTATION_VERSION,
              'detail_links_style':DETAIL_LINKS_STYLE,
              'metric_header_style':METRIC_HEADER_STYLE,
              'report_operator_open_id':authorization['open_id'],'report_secure_label_required':True,
              'report_secure_label':label,'created_at':datetime.now(timezone.utc).isoformat(),
              'startup_before_queue_seconds':round(perf_counter()-startup_started,6)}
    if native_readiness is not None:
        record.update(native_readiness=native_readiness,
                      analysis_profile=analysis_profile(settings['local_analysis']))
    if binding is not None:
        record['execution_binding_sha256'] = binding
    if settings.get('execution_release') is not None:
        record['execution_release'] = settings['execution_release']
        validate_execution_release(settings, record)
    # Keep the exact trusted transport header locally; never expose it in status.
    record['context_header'] = args.ccconnect_context
    if lifecycle.enabled(settings):
        record.update(scheduler_managed=True,execution_policy=deepcopy(settings['execution_policy']))
        with lifecycle.advisory_lock(path.with_suffix('.admission.oslock'),timeout=10):
            if path.exists():
                old=json.loads(path.read_text(encoding='utf-8'))
                if old.get('request_sha256')!=fingerprint or old.get('execution_release')!=settings['execution_release']:
                    raise InputError('同一来源消息的走查输入或执行版本发生冲突。')
                return {'status':old['status'],'job_key':key,'idempotent':True,'record_path':str(path)}
            policy.atomic_json(path,record)
        try:
            service=lifecycle.task_service(settings,wake=True)
        except InputError:
            service={'started':False,'method':'scheduled_task'}
        return {'status':'queued','job_key':key,'idempotent':False,'record_path':str(path),
                'worker':service,'message':('任务已排队，执行服务将自动处理。' if service['started'] else
                    '任务已排队，立即唤起未成功，等待定时执行服务；任务已保留。')}
    fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:
        json.dump(record,handle,ensure_ascii=False,indent=2)
    try:
        with path.with_suffix('.log').open('ab',buffering=0) as log:
            subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'worker','--record',str(path),'--settings',str(args.settings.resolve())],
                             cwd=settings['workspace_root'],env=worker_env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                             start_new_session=True,close_fds=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except OSError:
        # This call exclusively created the record. A failed launch must not
        # leave it queued forever; do not expose OS paths or command arguments.
        message = '后台走查进程启动失败；请检查独立运行环境后恢复此任务。'
        record.update(status='failed',error=message,failed_at=datetime.now(timezone.utc).isoformat())
        policy.atomic_json(path,record)
        raise InputError(message) from None
    return {'status':'queued','job_key':key,'idempotent':False}


def _prepare_local_analysis(settings, record, directory, *, on_stage=None):
    request = record['request']
    rules = rules_snapshot(settings,directory,policy.run_lark)
    pipeline = run_windows({role:request[role+'_metadata'] for role in ('baseline','replacement')},
                           settings,directory,rules,on_stage=on_stage)
    return rules, pipeline


def _finalize_local_analysis(settings, record, directory, rules, pipeline, *, product_version=None):
    request = record['request']
    selection.validate_task_pipeline(pipeline,
        {role:request[role+'_metadata'] for role in selection.ROLES},environment_policy(settings))
    windows = pipeline['windows']
    result = compare_performance(windows['baseline']['ad'],windows['replacement']['ad'],
                                 baseline_metadata=request['baseline_metadata'],replacement_metadata=request['replacement_metadata'],
                                 baseline_state=windows['baseline']['pcm'],replacement_state=windows['replacement']['pcm'],
                                 vehicle_label=None, validated_product_version=product_version)
    result['kind'] = 'walkthrough_test_performance_comparison'
    result['vin'] = request['vin']
    result['request_sha256'] = record['request_sha256']
    result['source_policy'] = SOURCE_POLICY
    if record.get('presentation_version') == PRESENTATION_VERSION:
        result['presentation_version'] = PRESENTATION_VERSION
    if 'detail_links_style' in record:
        if record['detail_links_style'] != DETAIL_LINKS_STYLE or result.get('presentation_version') != PRESENTATION_VERSION:
            raise InputError('任务明细入口样式无效；保留原任务，不重新呈现。')
        result['detail_links_style'] = DETAIL_LINKS_STYLE
    if 'metric_header_style' in record:
        if record['metric_header_style'] != METRIC_HEADER_STYLE or result.get('presentation_version') != PRESENTATION_VERSION:
            raise InputError('任务主报告指标表头样式无效；保留原任务，不重新呈现。')
        result['metric_header_style'] = METRIC_HEADER_STYLE
    result['pipeline'] = {'version':pipeline['version'],'environment':pipeline['environment'],
                          'environment_policy':environment_policy(settings),
                          'task_environment_selection':deepcopy(pipeline['task_environment_selection']),
                          'task_selection_sha256':pipeline['task_selection_sha256'],
                          'manifest_sha256':pipeline['source_manifest_sha256'],
                          'sources':{role:window['sources'] for role,window in windows.items()},
                          'windows':{role:{key:window[key] for key in ('version','request','status','rules_sha256',
                              'environment','environment_selection')} for role,window in windows.items()}}
    result['metric_rules']['p99_infer_delay_ms'] = 'native-equivalent header_delay P99 per state; lower is better'
    result['metric_rules']['frame_rate_hz'] = 'native-equivalent VlaModel/VlaParkingDLM FPS mean per state; higher is better'
    result['local_sources'] = {role:{key:value for key,value in window['ad'].items() if key != 'metrics'} for role,window in windows.items()}
    if any(window['ad'].get('source_quality',{}).get('status')=='partial' for window in windows.values()):
        result['status']='partial'
    if product_version is not None:
        result['product_version'] = product_version
        if product_version['status'] != 'ok':
            result['status'] = 'partial'
            result.setdefault('issues',[]).append({'kind':'product_version_unavailable',
                'message':'相关制品信息未获取：'+product_version['reason']})
    sheet_data = {}
    for role,window in windows.items():
        source = window['ad']
        detail = source.get('full_statistics') or {}
        data_path = Path(detail.get('path',''))
        expected_root = (Path(directory)/role).resolve()
        if (not data_path.is_file() or data_path.is_symlink() or not data_path.resolve().is_relative_to(expected_root)
                or digest(data_path) != detail.get('sha256')):
            raise InputError('完整附件缺少可验证的全量原生统计，不能只交付六指标。')
        report = json.loads(data_path.read_text(encoding='utf-8'))
        report['pcm_result'] = window['pcm']
        if product_version is not None:
            report['product_version'] = product_version
        scope = {'log_counts':source.get('download',{}).get('counts',{}),
                 'missing_log_classes':source.get('missing_log_classes',[]),
                 'native_analysis':source.get('analysis_scope',{}),
                 'source_quality':source.get('source_quality',{}),
                 'normalization':source.get('normalization',{})}
        hashes = {'native_report':detail['sha256'],'rules':digest(rules),
                  'pcm_result':hashlib.sha256(json.dumps(window['pcm'],sort_keys=True,ensure_ascii=False,
                      separators=(',',':'),allow_nan=False).encode()).hexdigest(),
                  'native_input':source['normalization']['input_sha256']}
        if product_version is not None:
            hashes['product_version'] = hashlib.sha256(json.dumps(product_version,sort_keys=True,
                ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        from native_sheet_data import validate_native_sheet_data, bind_native_sheet_metadata, SCHEMA
        payload = report.get('native_sheet_data')
        if not isinstance(payload,dict):
            raise InputError('完整统计缺少本版无图原生明细数据；请使用新任务生成。')
        payload = json.loads(json.dumps(payload,ensure_ascii=False,allow_nan=False))
        window_hash = hashlib.sha256(json.dumps(window['request'],sort_keys=True,
            ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        payload = bind_native_sheet_metadata(payload,{'request':window['request'],'role':role,
            'environment':window['environment'],'rules_sha256':digest(rules),'request_sha256':window_hash},
            delivery_context={'pcm_result':window['pcm'],'source_hashes':hashes,
                              'scope':scope,'product_version':product_version},
            statistics=report['statistics'])
        validate_native_sheet_data(payload)
        output = Path(directory)/role/'native-sheet-data.json'
        policy.atomic_json(output,payload)
        sheet_data[role] = {'path':str(output.resolve()),'sha256':digest(output),
            'bytes':output.stat().st_size,'schema':SCHEMA,'role':role,'request':window['request'],
            'request_sha256':window_hash,'environment':window['environment'],'rules_sha256':digest(rules)}
    result['native_sheet_data'] = sheet_data
    result['delivery_format'] = DELIVERY_FORMAT
    return result


def product_context(settings, record):
    return {'vin':record['request']['vin'],'task_key':record['job_key'],
            'operator_open_id':record['report_operator_open_id']}


def analyze_local_task(settings, record, directory, *, on_stage=None):
    """Offline local regression entry; never invokes the product query."""
    rules,pipeline = _prepare_local_analysis(settings,record,directory,on_stage=on_stage)
    return _finalize_local_analysis(settings,record,directory,rules,pipeline)


def analyze_task(settings, record, directory, *, on_stage=None):
    """Run the two independent branches, then bind both to one report snapshot."""
    product.validate_config(settings)
    context = product_context(settings,record)
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='walkthrough') as pool:
        local = pool.submit(_prepare_local_analysis,settings,record,directory,on_stage=on_stage)
        version = pool.submit(product.get_product_version,settings,context,Path(directory)/'product-version',on_stage=on_stage)
        rules,pipeline = local.result()
        product_result = version.result()
    product.validate_result(settings,context,product_result)
    return _finalize_local_analysis(settings,record,directory,rules,pipeline,product_version=product_result)


def create_report(settings, result, record_path):
    document = build_report_document(result)
    path = record_path.with_suffix('.report.xml')
    path.write_text(document['xml'],encoding='utf-8')
    relative = path.resolve().relative_to(Path(settings['workspace_root']).resolve())
    args = ['docs','+create','--as','bot','--doc-format','xml','--content','@./'+relative.as_posix()]
    parent = str(settings.get('report_parent_token') or '').strip()
    args += ['--parent-token',parent] if parent else ['--parent-position',str(settings.get('report_parent_position','my_library'))]
    data = policy.run_lark(settings,args,timeout=120,profile=settings['report_lark_profile'])['data']
    policy.atomic_json(record_path.with_suffix('.create-response.json'),data)
    urls = re.findall(r'https://[^\s"\']+\.feishu\.cn/(?:docx|docs)/[A-Za-z0-9_-]+',json.dumps(data))
    if not urls:
        # Do not retry a create after an uncertain outcome.
        raise InputError('文档创建响应没有可验证 URL；保留响应核查，禁止自动再次创建。')
    return urls[0]


def _native_reply_message_id(receipt):
    """Only a successful Bot response with an actual message ID proves delivery."""
    if (not isinstance(receipt,dict) or receipt.get('ok') is not True
            or receipt.get('identity') != 'bot' or not isinstance(receipt.get('data'),dict)):
        raise InputError('来源回复回执未确认固定 Bot 成功送达；保留待核查状态，不自动重发。')
    message_id = receipt['data'].get('message_id')
    if not isinstance(message_id,str) or not re.fullmatch(r'om_[A-Za-z0-9]+',message_id):
        raise InputError('来源回复回执缺少有效消息 ID；保留待核查状态，不自动重发。')
    return message_id


def _native_source_reply(settings, path, record, content, key, *, msg_type='post'):
    """Checkpoint one send intent; an unknown outcome requires reconciliation."""
    if msg_type not in ('post','text') or (
            msg_type == 'text' and record.get('presentation_version') != PRESENTATION_VERSION) or (
            msg_type == 'post' and record.get('presentation_version') is not None):
        raise InputError('来源回复类型与任务呈现版本不一致；不自动重发。')
    delivery_version = 'native-source-reply-v2' if msg_type == 'text' else 'native-source-reply-v1'
    intent = {'source_message_id':record['source_message_id'],
              'profile':settings['report_lark_profile'],'idempotency_key':key,
              'content_sha256':hashlib.sha256(content.encode('utf-8')).hexdigest()}
    if msg_type == 'text':
        intent['msg_type'] = 'text'
    delivery = record.get('report_reply_delivery')
    if delivery is not None:
        if (not isinstance(delivery,dict) or delivery.get('version') != delivery_version
                or delivery.get('intent') != intent):
            raise InputError('来源回复断点与当前来源、Bot或交付内容不一致；不自动重发。')
        receipt = delivery.get('receipt')
        if receipt is None:
            raise InputError('已有来源回复请求但送达回执未知；先核查原请求，不自动重发。')
        message_id = _native_reply_message_id(receipt)
        if delivery.get('pending') is not False or record.get('report_reply_message_id') != message_id:
            raise InputError('来源回复成功回执与本地断点不一致；不自动重发。')
        return message_id
    delivery = {'version':delivery_version,'intent':intent,'pending':True}
    record['report_reply_delivery'] = delivery
    policy.atomic_json(path,record)  # Save before the only send, including on timeout.
    with phase(path,record,'reply_to_source_in_chat'):
        receipt = policy.run_lark(settings,['im','+messages-reply','--as','bot',
            '--message-id',record['source_message_id'],'--msg-type',msg_type,'--content',content,
            '--idempotency-key',key],profile=settings['report_lark_profile'])
        message_id = _native_reply_message_id(receipt)
        # Keep only the finite delivery proof, never a response body or profile data.
        delivery['receipt'] = {'ok':True,'identity':'bot','data':{'message_id':message_id}}
        delivery['pending'] = False
        record['report_reply_message_id'] = message_id
        policy.atomic_json(path,record)
    return message_id


def publish(settings, path, record, result, *, before_delivery=None):
    native_delivery = result.get('delivery_format') == DELIVERY_FORMAT
    compact_presentation = False
    if native_delivery:
        record_version = record.get('presentation_version')
        result_version = result.get('presentation_version')
        if record_version is not None or result_version is not None:
            if record_version != PRESENTATION_VERSION or result_version != PRESENTATION_VERSION:
                raise InputError('任务与最终结果的呈现版本不一致；保留原结果，不重新渲染或交付。')
            compact_presentation = True
        if 'detail_links_style' in record or 'detail_links_style' in result:
            if (not compact_presentation or record.get('detail_links_style') != DETAIL_LINKS_STYLE
                    or result.get('detail_links_style') != DETAIL_LINKS_STYLE):
                raise InputError('任务与最终结果的明细入口样式不一致；不重新渲染或交付。')
        if 'metric_header_style' in record or 'metric_header_style' in result:
            if (not compact_presentation or record.get('metric_header_style') != METRIC_HEADER_STYLE
                    or result.get('metric_header_style') != METRIC_HEADER_STYLE):
                raise InputError('任务与最终结果的指标表头样式不一致；不重新渲染或交付。')
        # Statistics and the query outcome have already been checkpointed. A
        # missing product code must stop before creating or governing any asset;
        # resuming this snapshot must not silently query or analyze again.
        version = result.get('product_version')
        if not isinstance(version,dict) or version.get('status') != 'ok':
            raise InputError('未取得可信四位制品码；已保留统计与查询记录，暂停交付，不自动重查或重算。')
        product.validate_result(settings,product_context(settings,record),version)
    with phase(path,record,'report_bot_authorization'):
        policy.ensure_report_bot_authorization(settings)
    document_result = result
    if native_delivery:
        from native_sheet_delivery import ensure_native_sheets
        details = ensure_native_sheets(settings,path,record,result)
        document_result = {**result,'native_details':details}
    elif result.get('delivery_format') not in (None,'docx_with_full_xlsx_attachments'):
        raise InputError('未知最终交付格式，不能猜测创建报告。')
    url = str(record.get('report_url') or '')
    if not url:
        if record.get('report_create_pending'):
            raise InputError('已有未确认的创建请求，先人工核对，不能重复建报告。')
        record['report_create_pending'] = True
        policy.atomic_json(path,record)
        with phase(path,record,'create_report'):
            url = create_report(settings,document_result,path)
        record['report_url'] = url
        record.pop('report_create_pending',None)
        policy.atomic_json(path,record)
    if not native_delivery and isinstance(result.get('full_workbooks'),dict):
        from report_attachments import ensure_attachments
        ensure_attachments(settings,path,record,result,url)
    if native_delivery:
        from feishu_report import verify_main_report_document
        expected_document = build_report_document(document_result)
        response = policy.run_lark(settings,['docs','+fetch','--as','bot','--doc',url,
            '--doc-format','xml','--detail','full'],profile=settings['report_lark_profile'])
        if response.get('identity') != 'bot':
            raise InputError('主报告回读未确认固定 Bot 身份。')
        remote_document = (response.get('data') or {}).get('document') or {}
        validation = verify_main_report_document(remote_document.get('content',''),expected_document)
        record['report_content_validation'] = {**validation,'revision_id':remote_document.get('revision_id'),
            'document_id':remote_document.get('document_id')}
        policy.atomic_json(path,record)
    permissions = record.setdefault('report_permissions',{})
    if not permissions.get('collaborator_management'):
        with phase(path,record,'restrict_collaborator_management'):
            policy.restrict_collaborator_management(settings,url)
        permissions['collaborator_management'] = True
        policy.atomic_json(path,record)
    if not permissions.get('source_chat_view'):
        with phase(path,record,'source_access'):
            access = policy.grant_source_chat_view(settings,url,record['source_chat_id'],record.get('sender_id'))
        permissions['source_chat_access'] = access
        permissions['source_chat_view'] = True
        policy.atomic_json(path,record)
    if not permissions.get('owner_transfer'):
        if permissions.get('owner_transfer_pending'):
            raise InputError('主报告所有权交接回执未知，需先核查，不会自动重复转移。')
        permissions['owner_transfer_pending'] = True
        policy.atomic_json(path,record)
        with phase(path,record,'owner_transfer'):
            policy.transfer_report_owner(settings,url,record['sender_id'])
        permissions['owner_transfer'] = True
        permissions.pop('owner_transfer_pending',None)
        policy.atomic_json(path,record)
    with phase(path,record,'secure_label_and_temporary_access_cleanup'):
        policy.configure_report_secure_label(settings,path,record,url,permissions)
    key = hashlib.sha256((record['source_message_id']+':walkthrough-report').encode()).hexdigest()[:32]
    notification = build_report_notification(document_result,vin=record['vin'],url=url)
    if compact_presentation and (not isinstance(notification,dict) or set(notification) != {'text'}
            or not isinstance(notification['text'],str) or not notification['text'].strip()):
        raise InputError('新呈现通知缺少受检纯文本内容；未发送来源回复。')
    content = json.dumps(notification,ensure_ascii=False)
    record['status'] = 'delivering'
    policy.atomic_json(path,record)
    if before_delivery is not None:before_delivery()
    if native_delivery:
        _native_source_reply(settings,path,record,content,key,
                             msg_type='text' if compact_presentation else 'post')
    else:
        # Preserve legacy final-result recovery and its historical CLI response contract.
        with phase(path,record,'reply_to_source_in_chat'):
            delivered = policy.run_lark(settings,['im','+messages-reply','--as','bot','--message-id',record['source_message_id'],
                                      '--msg-type','post','--content',content,'--idempotency-key',key],
                            profile=settings['report_lark_profile'])
        delivered_data = delivered.get('data')
        message_id = delivered_data.get('message_id') if isinstance(delivered_data,dict) else None
        if isinstance(message_id,str) and re.fullmatch(r'om_[A-Za-z0-9]+',message_id):
            record['report_reply_message_id'] = message_id
    record.update(status='completed',execution_stage='completed',report_reply_mode='source_message_in_chat',
                  completed_at=datetime.now(timezone.utc).isoformat())
    record.pop('error',None)
    record.pop('error_diagnostic',None)
    record.pop('failed_at',None)
    policy.atomic_json(path,record)


def run_worker(path, settings_path):
    settings = load_settings(settings_path)
    input_path = Path(path)
    path = input_path.resolve()
    runtime = Path(settings['runtime_state_dir']).resolve()
    if path.parent != runtime:
        raise InputError('任务记录必须来自走查测试独立 runtime。')
    if settings.get('execution_release') is not None:
        if input_path.is_symlink() or path.suffix != '.json':
            raise InputError('任务记录必须是本版独立 runtime 的普通 JSON 文件。')
        original = json.loads(path.read_text(encoding='utf-8'))
        if (original.get('kind') != 'walkthrough_test_local'
                or original.get('job_key') != path.stem):
            raise InputError('任务记录身份不一致；未恢复任务。')
        validate_execution_release(settings, original)
    if lifecycle.enabled(settings):
        lifecycle.validate_policy(settings)
        if path.with_suffix('.worker.lock').exists():
            raise InputError('原执行锁尚未核验，不自动接管旧进程。')
        with lifecycle.advisory_lock(path.with_suffix('.worker.oslock')):
            from native_feedback import monitor
            with monitor(path,settings,policy) as before_delivery:
                return _run_owned(path,settings,before_delivery=before_delivery)
    with _legacy_worker_lock(path):
        return _run_owned(path,settings)


@contextmanager
def _legacy_worker_lock(path):
    lock = path.with_suffix('.worker.lock')
    try:
        fd = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        raise InputError('已有走查 worker；确认旧进程停止后再处理遗留锁。') from None
    os.close(fd)
    try:yield
    finally:lock.unlink(missing_ok=True)


def _run_owned(path,settings,*,before_delivery=None):
    runtime=Path(settings['runtime_state_dir']).resolve()
    record = None
    record_validated = False
    try:
        # A waiting caller must observe the previous worker's final persisted state.
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record,dict) or record.get('kind') != 'walkthrough_test_local':
            raise InputError('拒绝恢复其他 Skill 的任务。')
        if lifecycle.enabled(settings):
            lifecycle.validate_source(record)
            if record.get('execution_policy')!=settings.get('execution_policy'):
                raise InputError('原任务执行策略不匹配，不自动接管。')
        record_validated = True
        if record.get('status') == 'completed':
            return 0
        if lifecycle.enabled(settings):
            record.update(worker_pid=os.getpid(),status='running')
            policy.atomic_json(path,record)
        policy.activate_lark_environment(settings)
        with phase(path,record,'worker_user_authorization'):
            policy.ensure_user_authorization(settings,record['report_operator_open_id'])
        result_path = path.with_suffix('.result.json')
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding='utf-8'))
            if (digest(result_path) != record.get('result_sha256') or result.get('vin') != record['vin']
                    or result.get('request_sha256') != record['request_sha256']
                    or result.get('source_policy') != SOURCE_POLICY):
                raise InputError('已计算结果的身份或指纹不一致。')
            if result.get('delivery_format') == DELIVERY_FORMAT:
                selection.validate_cached_pipeline(result.get('pipeline'),
                    {role:record['request'][role+'_metadata'] for role in selection.ROLES},
                    environment_policy(settings))
                product.validate_result(settings,product_context(settings,record),result.get('product_version'))
        else:
            if record.get('report_url'):
                raise InputError('已有报告但结果记录缺失，禁止重新分析后覆盖旧报告。')
            if record.get('analysis_profile') != analysis_profile(settings.get('local_analysis')):
                raise InputError('旧任务没有本版最终快照；请用新来源消息创建任务，不会自动重查或重算。')
            record['native_readiness'] = check_native_environment(settings['local_analysis'])
            directory = path.with_suffix('.work')
            if directory.is_symlink() or not directory.resolve().is_relative_to(runtime):
                raise InputError('走查任务目录不能使用越界路径或链接。')
            directory.mkdir(exist_ok=True)
            record['status'] = 'analyzing'
            policy.atomic_json(path,record)
            stage_lock = threading.RLock()
            def update_stage(event):
                with stage_lock:
                    record['analysis_stage']=event['window']+':'+event['stage'] if event['scope']=='window' else event['stage']
                    if event['scope']=='window':
                        record.setdefault('analysis_windows',{})[event['window']]={
                            key:value for key,value in event.items() if key not in ('scope','window')}
                    elif event['scope']=='product_version':
                        record['product_version_stage']=event['stage']
                    else:
                        record['analysis_task_stage']=event['stage']
                        record['analysis_elapsed_seconds']=event.get('elapsed_seconds')
                    policy.atomic_json(path,record)
            with lifecycle.capacity(settings,path,record,policy.atomic_json):
                with phase(path,record,'analysis'):
                    result = analyze_task(settings,record,directory,on_stage=update_stage)
            if result.get('status') not in ('ok','partial'):
                raise InputError('没有可用对比数据，拒绝生成空成功报告。')
            policy.atomic_json(result_path,result)
            record['result_sha256'] = digest(result_path)
            record['result_path'] = str(result_path)
            policy.atomic_json(path,record)
        if before_delivery is not None:
            publish(settings,path,record,result,before_delivery=before_delivery)
        else:
            publish(settings,path,record,result)
        return 0
    except Exception as exc:
        failure = safe_failure_fields(exc)
        if record_validated:
            record.pop('error_diagnostic',None)
            record.update(status='failed',**failure,failed_at=datetime.now(timezone.utc).isoformat())
            policy.atomic_json(path,record)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command',required=True)
    start = subs.add_parser('start')
    start.add_argument('--text',required=True)
    start.add_argument('--ccconnect-context',required=True)
    start.add_argument('--settings',type=Path,required=True)
    start.add_argument('--received-at')
    start.add_argument('--interpretation',type=json.loads)
    for name in ('worker','resume'):
        part = subs.add_parser(name)
        part.add_argument('--record',type=Path,required=True)
        part.add_argument('--settings',type=Path,required=True)
    args = parser.parse_args()
    try:
        if args.command == 'start':
            print(json.dumps(start_job(args),ensure_ascii=False))
            return 0
        return run_worker(args.record,args.settings)
    except Exception as exc:
        # Network exceptions must not leak a credential-bearing URL.
        print(json.dumps({'status':'failed',**safe_failure_fields(exc)},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
