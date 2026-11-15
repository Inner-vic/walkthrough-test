"""Host entry for the independent walkthrough skill; local checks precede new jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

WORKSPACE = next(p for p in Path(__file__).resolve().parents if (p/'AGENTS.md').is_file() and (p/'.agents/skills').is_dir())
SKILL = WORKSPACE / ".agents" / "skills" / "walkthrough-test"
MANIFEST = WORKSPACE / "deployment" / "walkthrough-test.installation.json"
ENVIRONMENT_KEYS = {
    "WALKTHROUGH_TEST_ORIGIN",
    "WALKTHROUGH_TEST_DOWNLOAD_HTTPS_HOSTS",
    "WALKTHROUGH_TEST_DOWNLOAD_HTTP_HOSTS",
    "WALKTHROUGH_PROD_ORIGIN",
    "WALKTHROUGH_PROD_DOWNLOAD_HTTPS_HOSTS",
    "WALKTHROUGH_PROD_DOWNLOAD_HTTP_HOSTS",
}
TEST_KEYS = {key for key in ENVIRONMENT_KEYS if "_TEST_" in key}
PROD_KEYS = ENVIRONMENT_KEYS - TEST_KEYS
RETAINED_VERSION = "0.8.1-windows-integration-target.8"
RETAINED_RELEASES = {
    RETAINED_VERSION: ('wtr088-taskenv', 'TARGET8-SOURCE-MANIFEST.json',
        'walkthrough-target8-20261102-candidate.settings.json', None),
    '0.8.1-windows-integration-target.9': ('wtr089-windowenv', 'TARGET9-SOURCE-MANIFEST.json',
        'walkthrough-target9-20261102-windowenv.settings.json', 'walkthrough-target9-windowenv'),
    '0.8.1-windows-integration-target.10': ('wtr090-prodnull', 'TARGET10-SOURCE-MANIFEST.json',
        'walkthrough-target10-20261102-prodnull.settings.json', 'walkthrough-target10-prodnull'),
    '0.8.1-windows-integration-target.11': ('wtr091-resume', 'TARGET11-SOURCE-MANIFEST.json',
        'walkthrough-target11-20261102-resume.settings.json', 'walkthrough-target11-resume'),
    '0.8.1-windows-integration-target.12': ('wtr092-preflight', 'TARGET12-SOURCE-MANIFEST.json',
        'walkthrough-target12-20261102-preflight.settings.json', 'walkthrough-target12-preflight'),
    '0.8.1-windows-integration-target.13': ('wtr093-sources', 'TARGET13-SOURCE-MANIFEST.json',
        'walkthrough-target13-20261102-sources.settings.json', 'walkthrough-target13-sources'),
    '0.8.1-windows-integration-target.14': ('wtr094-performance', 'TARGET14-SOURCE-MANIFEST.json',
        'walkthrough-target14-20261103-performance.settings.json', 'walkthrough-target14-performance'),
    '0.8.1-windows-integration-target.15': ('wtr095-lifecycle', 'TARGET15-SOURCE-MANIFEST.json',
        'walkthrough-target15-20261103-lifecycle.settings.json', 'walkthrough-target15-lifecycle'),
}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _plain(path, *, directory=False):
    """Reject links/junctions before trusting a release or a task path."""
    path = Path(path)
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('untrusted linked path')
        if item == path:
            valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not valid:
                raise ValueError('not a plain file or directory')
    return path.resolve(strict=True)


def exact_record(path, runtime):
    supplied = Path(path)
    runtime = _plain(Path(runtime), directory=True)
    if supplied.parent.resolve() != runtime or supplied.suffix != '.json':
        raise ValueError('foreign task record')
    path = _plain(supplied)
    if path.parent != runtime:
        raise ValueError('foreign task record')
    record = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('kind') != 'walkthrough_test_local'
            or record.get('job_key') != path.stem
            or not re.fullmatch(r'walkthrough-[0-9a-f]{24}', path.stem)):
        raise ValueError('invalid task record')
    return path, record


def validate_resume_source(record, identity_parser):
    identity = identity_parser(record.get('context_header'))
    request = record.get('request') or {}
    key = 'walkthrough-' + hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
    fingerprint = hashlib.sha256(json.dumps([
        identity, request['baseline_metadata'], request['replacement_metadata']], sort_keys=True).encode()).hexdigest()
    if (record.get('job_key') != key or any(record.get(k) != v for k, v in identity.items())
            or record.get('request_sha256') != fingerprint
            or any(request[role+'_metadata'].get('vin') != record.get('vin') for role in ('baseline','replacement'))):
        raise ValueError('task source or request binding differs')


def execution_binding(settings, validate_product):
    return hashlib.sha256(json.dumps([
        validate_product(settings), settings.get('lark_profile'), settings.get('lark_home'),
        settings.get('expected_lark_app_id'), settings.get('expected_operator_open_id'),
        settings.get('local_analysis')], sort_keys=True).encode()).hexdigest()


def validate_active_resume(record, settings, installation, worker):
    release = installation.get('execution_release')
    if (not isinstance(release, str) or not release
            or settings.get('execution_release') != release
            or record.get('execution_release') != release
            or record.get('execution_binding_sha256') != execution_binding(settings, worker.product.validate_config)
            or record.get('analysis_profile') != worker.analysis_profile(settings.get('local_analysis'))):
        raise ValueError('active execution binding differs')


def retained_resume(installation, record_path):
    """Select only a pinned, exact old final. Never migrate or recalculate it."""
    path, record = exact_record(record_path, installation['runtime_state_dir'])
    release = installation.get('execution_release')
    if isinstance(release, str) and release and record.get('execution_release') == release:
        return installation, record, False
    candidates = [entry for entry in installation.get('retained_releases', [])
                  if isinstance(entry, dict)
                  and entry.get('execution_binding_sha256') == record.get('execution_binding_sha256')
                  and entry.get('execution_release') == record.get('execution_release')
                  and entry.get('analysis_profile') == record.get('analysis_profile')]
    if len(candidates) != 1 or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('execution_binding_sha256', ''))):
        raise ValueError('unknown old task binding')
    retained = candidates[0]
    previous = retained.get('installation')
    if not isinstance(previous, dict) or previous.get('version') not in RETAINED_RELEASES:
        raise ValueError('unapproved retained release')
    version = previous['version']
    directory_name, manifest_name, settings_name, old_release = RETAINED_RELEASES[version]
    expected_skill = (WORKSPACE / 'runtime' / directory_name / 'skill').resolve()
    if (Path(previous.get('skill_root', '')).resolve() != expected_skill
            or previous.get('execution_release') != old_release
            or retained.get('execution_release') != old_release
            or Path(previous.get('workspace_root', '')).resolve() != WORKSPACE.resolve()
            or Path(previous.get('runtime_state_dir', '')).resolve() != path.parent
            or Path(previous.get('host_python', '')).resolve() != Path(installation['host_python']).resolve()
            or Path(previous.get('settings_path', '')).resolve().parent != Path(installation['settings_path']).resolve().parent
            or Path(previous.get('settings_path', '')).name != settings_name):
        raise ValueError('retained fixed paths differ')
    settings_path = _plain(Path(previous['settings_path']))
    if sha256(settings_path) != retained.get('settings_sha256'):
        raise ValueError('retained settings changed')
    manifest_path = expected_skill.parent / manifest_name
    if Path(previous.get('source_manifest_path', '')).resolve() != manifest_path.resolve():
        raise ValueError('retained source manifest path differs')
    _plain(expected_skill, directory=True)
    _plain(manifest_path)
    if sha256(manifest_path) != retained.get('source_manifest_file_sha256'):
        raise ValueError('retained source manifest changed')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    files = manifest.get('files')
    if (manifest.get('version') != version or not isinstance(files, dict) or not files
            or manifest.get('source_file_count') != len(files)
            or manifest.get('canonical_source_sha256') != previous.get('source_manifest_sha256')):
        raise ValueError('retained source evidence differs')
    actual_files = {item.relative_to(expected_skill).as_posix()
                    for item in expected_skill.rglob('*') if item.is_file()}
    if actual_files != set(files):
        raise ValueError('retained source inventory changed')
    for name, checksum in files.items():
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                or ':' in name or not re.fullmatch(r'[0-9a-f]{64}', str(checksum))):
            raise ValueError('invalid retained source member')
        source = _plain(expected_skill / relative)
        if not source.is_relative_to(expected_skill) or sha256(source) != checksum:
            raise ValueError('retained source file changed')
    result_path = path.with_suffix('.result.json')
    _plain(result_path)
    if (not re.fullmatch(r'[0-9a-f]{64}', str(record.get('result_sha256', '')))
            or sha256(result_path) != record['result_sha256']):
        raise ValueError('old final absent or changed')
    # The frozen worker still verifies VIN, request, source policy, product,
    # presentation, renderer/template, permissions and send checkpoints.
    return {**previous, '_retained_settings_sha256':retained['settings_sha256']}, record, True


def emit(value):
    print(json.dumps(value, ensure_ascii=False))


def execution_failure_fields(error, worker, input_error_type):
    """Use the worker's bounded auth diagnosis without exposing transport data."""
    download_error = getattr(worker.cloud, 'CloudDownloadError', ())
    preflight_error = getattr(worker, 'PreflightError', ())
    if (isinstance(error, (worker.cloud.CloudAuthError, worker.cloud.CloudResponseContractError))
            or isinstance(error, download_error) or isinstance(error, preflight_error)):
        return worker.safe_failure_fields(error)
    return {"error": str(error) if isinstance(error, input_error_type) else
            "走查测试检查或执行失败，请查看独立配置和任务诊断。"}


def start_checked_preflight(check, args, installation, settings, settings_path, worker,
                            decision, *, active_execution_enabled):
    """Only a bound new start may renew an existing fixed-User authorization."""
    kind = getattr(worker, 'PreflightError', ())
    def binding_failure():
        return (kind('product_identity', 'check_failed') if isinstance(kind, type)
                else ValueError('The preflight input or settings binding changed'))
    try:
        original_settings_bytes = settings_path.read_bytes()
        settings_digest = hashlib.sha256(original_settings_bytes).hexdigest()
        original_settings = json.loads(original_settings_bytes.decode('utf-8-sig'))
        if original_settings != settings:
            raise ValueError
        input_binding = (args.text, args.ccconnect_context, args.received_at,
                         json.dumps(args.interpretation, sort_keys=True))
    except Exception:
        raise binding_failure() from None
    def verify_bindings():
        try:
            if (sha256(settings_path) != settings_digest or original_settings != settings
                    or input_binding != (args.text, args.ccconnect_context, args.received_at,
                                         json.dumps(args.interpretation, sort_keys=True))):
                raise ValueError
        except Exception:
            raise binding_failure() from None
    try:
        checked = check(settings_path)
        if not isinstance(checked, dict) or checked.get('status') != 'local_preflight_passed':
            raise ValueError('Preflight did not establish complete local readiness')
        verify_bindings()
        return checked
    except Exception as error:
        if (not isinstance(error, kind) or getattr(error, 'stage', None) != 'product_identity'
                or getattr(error, 'reason', None) != 'user_token_refresh_required'
                or args.action != 'start' or not active_execution_enabled
                or installation.get('execution_enabled') is not True
                or not isinstance(args.ccconnect_context, str) or not args.ccconnect_context
                or not isinstance(decision, dict) or decision.get('status') != 'ready'):
            raise
        # Route and raw transport validation precede all maintenance. The fixed
        # operator remains separate from the requesting source identity.
        worker.request_identity(args.ccconnect_context)
        request = decision.get('normalized') or {}
        roles = [request.get(role+'_metadata') for role in ('baseline','replacement')]
        if (any(not isinstance(value, dict) or not value.get('vin') for value in roles)
                or roles[0]['vin'] != roles[1]['vin']):
            raise
        release = installation.get('execution_release')
        expected_operator = settings.get('expected_operator_open_id')
        if (not isinstance(release, str) or not release or settings.get('execution_release') != release
                or not isinstance(expected_operator, str) or not expected_operator):
            raise
        verify_bindings()
        try:
            authorization = worker.policy.ensure_user_authorization(settings)
        except Exception as maintenance_error:
            reason = ('user_reauthorization_required' if isinstance(maintenance_error,
                getattr(worker.policy, 'UserAuthorizationError', ())) else 'user_token_refresh_failed')
            raise kind('product_identity', reason) from None
        if not isinstance(authorization, dict) or authorization.get('open_id') != expected_operator:
            raise kind('product_identity', 'user_identity_mismatch') from None
        verify_bindings()
        # One complete second pass is mandatory; there is no second renewal or
        # partial readiness shortcut if this check still fails.
        try:
            checked = check(settings_path)
            if not isinstance(checked, dict) or checked.get('status') != 'local_preflight_passed':
                raise ValueError
            verify_bindings()
        except Exception:
            raise kind('product_identity', 'check_failed') from None
        return checked


def inspect_static_settings(settings, installation):
    """Check paths and the approved identity without importing a business worker."""
    identity = json.loads((WORKSPACE / "deployment" / "cc-connect-lark-isolation.json").read_text(encoding="utf-8-sig"))
    workspace = Path(settings["workspace_root"]).resolve()
    runtime = Path(settings["runtime_state_dir"]).resolve()
    allowed = {(WORKSPACE/'data/walkthrough-test-fallback/runtime').resolve(),
               (WORKSPACE/'data/walkthrough-test-windows081/runtime').resolve()}
    if workspace != WORKSPACE.resolve() or runtime not in allowed:
        raise ValueError("走查测试必须使用本工作区的独立 runtime。")
    local = settings.get('local_analysis') or {}
    if local.get('backend') == 'native_full' and local.get('execution') == 'windows':
        if runtime != (WORKSPACE/'data/walkthrough-test-windows081/runtime').resolve():
            raise ValueError('Windows 完整任务必须使用新独立 runtime，不能复用旧任务目录。')
    if runtime != Path(installation["runtime_state_dir"]).resolve():
        raise ValueError("走查任务目录与安装清单不一致。")
    for key in ("lark_home", "lark_cli", "auth_credential_replica_path"):
        if Path(settings[key]).resolve() != Path(identity[key]).resolve():
            raise ValueError("走查测试身份路径与项目隔离清单不一致。")
    for key in ("project_id", "expected_lark_app_id"):
        if settings.get(key) != identity[key]:
            raise ValueError("走查测试项目或 App 与隔离清单不一致。")
    if settings.get("expected_operator_open_id") != identity["expected_dispatch_open_id"]:
        raise ValueError("走查测试固定操作用户与隔离清单不一致。")
    if (settings.get("lark_profile") != identity["profiles"]["dispatch_user"]
            or settings.get("report_lark_profile") != identity["profiles"]["report_bot"]
            or settings["lark_profile"] == settings["report_lark_profile"]
            or settings.get("project_isolation_required") is not True):
        raise ValueError("走查测试用户与报告 Bot 未正确隔离。")
    if not Path(settings["lark_cli"]).is_file() or not (Path(settings["lark_home"]) / ".lark-cli/config.json").is_file():
        raise ValueError("本机项目 CLI 或隔离身份目录不存在。")
    cloud_path = Path(settings["cloud_settings_path"]).resolve()
    expected_cloud = Path(installation["settings_path"]).parent / "walkthrough-cloud.settings.json"
    if cloud_path != expected_cloud or not cloud_path.is_file():
        raise ValueError("走查测试必须使用自己的车云身份配置。")
    cloud = json.loads(cloud_path.read_text(encoding="utf-8-sig"))
    if set(cloud) != {"username"} or not isinstance(cloud["username"], str) or not cloud["username"].strip():
        raise ValueError("独立车云配置须仅包含已获准的 username。")
    return {"status": "passed", "runtime_isolated": True, "project_identity_matches_manifest": True,
            "user_and_report_bot_separated": True, "independent_cloud_settings": True,
            "live_auth_verified": False}


def record_status(path, settings):
    path, record = exact_record(path, settings['runtime_state_dir'])
    result = {key: record[key] for key in ("job_key", "status", "error", "report_url", "analysis_stage", "analysis_windows",
                                        "execution_stage", "phase_timings", "startup_before_queue_seconds",
                                        "analysis_elapsed_seconds", "report_reply_message_id", "report_reply_mode") if key in record}
    if (record.get('execution_policy') or {}).get('version')=='walkthrough-lifecycle-v1':
        from native_feedback import business_status
        result['message']=business_status(record)
        if record.get('status') in ('queued','waiting_capacity'):
            ahead=0
            for other_path in path.parent.glob('walkthrough-*.json'):
                if other_path==path or re.fullmatch(r'walkthrough-[0-9a-f]{24}',other_path.stem) is None:continue
                try:
                    other=json.loads(other_path.read_text(encoding='utf-8'))
                    if other.get('scheduler_managed') is True and other.get('status') not in ('completed','failed') and str(other.get('created_at',''))<=str(record.get('created_at','')):ahead+=1
                except (OSError,ValueError):continue
            result['queue_ahead']=ahead
        feedback=path.with_suffix('.feedback.json')
        if feedback.is_file():
            try:
                sent=json.loads(feedback.read_text(encoding='utf-8'))
                if sent.get('source_message_id')==record.get('source_message_id') and sent.get('pending') is False:
                    result['progress_sent']=True
            except (OSError,ValueError):pass
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("route", "preflight", "rules-check", "start", "resume", "status", "wait"))
    parser.add_argument("--text")
    parser.add_argument("--ccconnect-context")
    parser.add_argument("--received-at", help="Internal fixed normalization clock; never inferred from message text.")
    parser.add_argument("--interpretation", type=json.loads, help="Validated semantic proposal JSON; file paths are not accepted.")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=45)
    args = parser.parse_args()
    if args.action in ('resume','status','wait') and args.record is None:
        parser.error('--record is required')
    # Importing a retained release must not create or refresh files in its
    # frozen source snapshot, even if the caller omitted Python's -B flag.
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    global SKILL
    installation = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    active_execution_enabled = installation.get('execution_enabled') is True
    SKILL = Path(installation.get('skill_root',str(SKILL))).resolve()
    if not SKILL.is_relative_to(WORKSPACE):
        emit({'status':'failed','error':'走查代码入口不在本工作区内。'})
        return 1
    legacy = installation.get('legacy') or {}
    legacy_runtime = Path(legacy['runtime_state_dir']).resolve() if legacy.get('runtime_state_dir') else None
    if legacy_runtime is not None and (
            legacy_runtime != (WORKSPACE/'data/walkthrough-test-fallback/runtime').resolve()
            or legacy_runtime == Path(installation['runtime_state_dir']).resolve()):
        emit({'status':'failed','error':'旧任务入口目录绑定无效；未执行或创建任务。'})
        return 1
    if args.action in ('resume','status','wait') and args.record is not None and legacy_runtime is not None and args.record.resolve().parent == legacy_runtime:
        command = [legacy['host_python'],'-B','-X','utf8',str(WORKSPACE/'deployment/walkthrough_legacy_entry.py'),*sys.argv[1:]]
        completed = subprocess.run(command,cwd=WORKSPACE,capture_output=True,text=True,encoding='utf-8',
            env=dict(os.environ,OPENBLAS_NUM_THREADS='1'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if completed.stdout:sys.stdout.write(completed.stdout)
        if completed.stderr:sys.stderr.write(completed.stderr)
        return completed.returncode
    resume_record = None
    frozen_resume = False
    if args.action == 'resume' and args.record is not None:
        try:
            installation, resume_record, frozen_resume = retained_resume(installation, args.record)
            SKILL = Path(installation['skill_root']).resolve()
        except (OSError, KeyError, TypeError, ValueError):
            emit({'status':'failed','error':'原任务执行版本或最终结果无法核验；保留原记录，不自动重查或重算。',
                  'task_created':False})
            return 1
    host_python = Path(installation["host_python"]).resolve()
    if args.action != "route" and Path(sys.executable).resolve() != host_python:
        if not host_python.is_file():
            emit({"status": "deployment_incomplete", "missing": ["独立 CPython 3.11 环境"], "task_created": False})
            return 1
        result = subprocess.run([str(host_python), "-B", "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]],
                                cwd=WORKSPACE, capture_output=True, text=True, encoding="utf-8",
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        # Windows CREATE_NO_WINDOW does not reliably inherit captured standard
        # handles through a second process. Forward both streams explicitly.
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode
    sys.path.insert(0, str(SKILL / "scripts"))
    from walkthrough_route import route, source_normalization_anchor, request_identity
    if args.action == 'resume':
        try:
            validate_resume_source(resume_record, request_identity)
        except (KeyError, TypeError, ValueError):
            emit({'status':'failed','error':'原任务来源或请求绑定无法核验；保留原记录，不自动恢复。','task_created':False})
            return 1
    if args.action == 'start' and args.ccconnect_context:
        try:
            identity = request_identity(args.ccconnect_context)
            key = 'walkthrough-' + hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
            existing_path = Path(installation['runtime_state_dir']) / (key + '.json')
            if existing_path.exists() or existing_path.is_symlink():
                existing_path, old = exact_record(existing_path, installation['runtime_state_dir'])
                if any(old.get(k) != v for k, v in identity.items()):
                    raise ValueError('source mismatch')
                anchor = source_normalization_anchor(
                    {'runtime_state_dir':installation['runtime_state_dir']}, args.ccconnect_context,
                    args.received_at, identity=identity)
                decision = route(args.text, received_at=anchor, interpretation=args.interpretation)
                request = decision.get('normalized') or {}
                if (decision.get('status') != 'ready'
                        or any(request.get(role+'_metadata') != (old.get('request') or {}).get(role+'_metadata')
                               for role in ('baseline','replacement'))):
                    raise ValueError('request mismatch')
                fingerprint = hashlib.sha256(json.dumps([
                    identity,request['baseline_metadata'],request['replacement_metadata']], sort_keys=True).encode()).hexdigest()
                if old.get('request_sha256') != fingerprint:
                    raise ValueError('request fingerprint mismatch')
                value = record_status(existing_path, {'runtime_state_dir':installation['runtime_state_dir']})
                value.update(idempotent=True, record_path=str(existing_path))
                emit(value)
                return 0
        except (OSError, KeyError, TypeError, ValueError):
            emit({'status':'failed','error':'无法验证原走查任务的来源和请求；未创建新任务。','task_created':False})
            return 1
    if args.action=='start' and args.ccconnect_context and legacy_runtime is not None:
        try:
            identity=request_identity(args.ccconnect_context)
            key='walkthrough-'+hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
            old_path=legacy_runtime/(key+'.json')
            if old_path.is_file():
                old=json.loads(old_path.read_text(encoding='utf-8'))
                if old.get('kind')!='walkthrough_test_local' or old.get('job_key')!=key or any(old.get(k)!=v for k,v in identity.items()):
                    raise ValueError('source mismatch')
                anchor=source_normalization_anchor({'runtime_state_dir':str(legacy_runtime)},
                    args.ccconnect_context,args.received_at,identity=identity)
                decision=route(args.text,received_at=anchor,interpretation=args.interpretation)
                if decision.get('status')!='ready' or any(decision['normalized'].get(r+'_metadata')!=(old.get('request') or {}).get(r+'_metadata') for r in ('baseline','replacement')):
                    raise ValueError('request mismatch')
                value=record_status(old_path,{'runtime_state_dir':str(legacy_runtime)})
                value.update(idempotent=True,record_path=str(old_path),legacy_record=True)
                emit(value)
                return 0
        except Exception:
            emit({'status':'failed','error':'无法验证原走查任务的来源和请求；未创建新任务。','task_created':False})
            return 1
    if args.action in ("route", "start"):
        if not args.text:
            parser.error("--text is required")
        # Recover only the exact transport-source record's anchor before any
        # preflight. Relative dates must not change on a next-day retry.
        if args.action == "start" and args.ccconnect_context:
            early_settings_path = Path(installation["settings_path"])
            if early_settings_path.is_file():
                try:
                    early_settings = json.loads(early_settings_path.read_text(encoding="utf-8-sig"))
                    expected_runtime = Path(installation["runtime_state_dir"]).resolve()
                    if (Path(early_settings["runtime_state_dir"]).resolve() != expected_runtime
                            or Path(installation["runtime_state_dir"]).resolve() != expected_runtime):
                        raise ValueError("untrusted runtime")
                    args.received_at = source_normalization_anchor(
                        early_settings,args.ccconnect_context,args.received_at)
                except Exception:
                    emit({"status":"failed","error":"无法验证本次来源与原任务的时间绑定，未创建任务。",
                          "task_created":False})
                    return 1
        decision = route(args.text,received_at=args.received_at,interpretation=args.interpretation)
        if args.action == "route" or decision["status"] != "ready":
            emit(decision)
            return 0
        # The worker receives raw text and this exact clock, not a second now().
        args.received_at = decision["normalized"].get("normalization",{}).get("received_at",args.received_at)
    if not 0 <= args.wait_seconds <= 50:
        parser.error("--wait-seconds must be between 0 and 50")
    settings_path = Path(installation["settings_path"])
    if not settings_path.is_file():
        emit({"status": "deployment_incomplete", "missing": ["走查测试独立配置"], "task_created": False})
        return 1
    settings_bytes = settings_path.read_bytes()
    settings = json.loads(settings_bytes.decode('utf-8-sig'))
    if frozen_resume and hashlib.sha256(settings_bytes).hexdigest() != installation['_retained_settings_sha256']:
        emit({'status':'failed','error':'原任务冻结配置发生变化；保留原记录，不自动恢复。','task_created':False})
        return 1
    try:
        static = inspect_static_settings(settings, installation)
    except (KeyError, OSError, TypeError, ValueError):
        emit({"status": "failed", "error": "走查测试静态隔离配置校验失败，请检查独立配置与项目清单。", "task_created": False})
        return 1
    if args.action in ("status", "wait"):
        try:
            deadline = time.monotonic() + (args.wait_seconds if args.action == "wait" else 0)
            while True:
                value = record_status(args.record, settings)
                if value.get("status") in ("completed", "failed") or time.monotonic() >= deadline:
                    emit(value)
                    return 0
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        except (OSError, ValueError):
            emit({"status": "failed", "error": "无法读取指定的本版走查任务记录；不按最近任务匹配。"})
            return 1
    if args.action in ("start", "resume") and (
            not active_execution_enabled or installation.get("execution_enabled") is not True):
        emit({"status": "execution_disabled", "static_checks": static,
              "message": "走查测试已暂停执行；请查看当前部署状态。",
              "task_created": False})
        return 1
    if args.action == "start" and not args.ccconnect_context:
        emit({"status": "needs_input", "message": "缺少 CC Connect 原始来源上下文，未创建任务。"})
        return 1
    local = settings.get("local_analysis") or {}
    missing = []
    if (local.get("backend") != "native_full" or local.get("execution") != "windows"
            or local.get("environment") not in ("prod_then_test", "test_then_prod", "test", "prod")):
        missing.append("Windows 完整后端及明确环境策略")
    cloud_env = settings.get("cloud_environment") or {}
    # Require the first queried environment; the fallback is checked only when needed.
    required_keys = PROD_KEYS if local.get('environment') in ('prod_then_test','prod') else TEST_KEYS
    if not required_keys.issubset(cloud_env) or set(cloud_env) - ENVIRONMENT_KEYS or any(not isinstance(v, str) for v in cloud_env.values()):
        missing.append("WALKTHROUGH 专用车云环境配置")
    if args.action in ('preflight','rules-check','start') and (
            not isinstance(installation.get('execution_release'), str) or not installation['execution_release']
            or settings.get('execution_release') != installation['execution_release']):
        missing.append('走查测试执行版本绑定')
    if missing:
        emit({"status": "deployment_incomplete", "static_checks": static, "missing": missing,
              "message": "走查测试 Windows 完整后端配置不完整，未创建业务任务。",
              "task_created": False})
        return 1
    # Set only this process's walkthrough variables before importing its client.
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(cloud_env)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    import walkthrough_worker as worker
    from request_model import InputError
    start_job_entered = False
    try:
        if args.action == 'resume':
            if frozen_resume:
                if (execution_binding(settings, worker.product.validate_config) != resume_record.get('execution_binding_sha256')
                        or settings.get('execution_release') != resume_record.get('execution_release')
                        or worker.analysis_profile(settings.get('local_analysis')) != resume_record.get('analysis_profile')):
                    raise InputError('原任务冻结配置与执行绑定不同；保留原记录，不自动恢复。')
            else:
                validate_active_resume(resume_record, settings, installation, worker)
        if args.action in ("preflight", "rules-check", "start"):
            from preflight import check
            checked = start_checked_preflight(check, args, installation, settings, settings_path,
                worker, decision if args.action == 'start' else None,
                active_execution_enabled=active_execution_enabled)
            if args.action == "preflight":
                emit(checked)
                return 0
        if args.action == "rules-check":
            from datetime import datetime
            from m100_attachment_worker import validate_full_rules
            from native_log_source import rules_snapshot, digest
            from tempfile import mkdtemp
            settings = worker.load_settings(settings_path)
            worker.policy.ensure_user_authorization(settings)
            parent = settings_path.parent / "deployment-validation"
            parent.mkdir(exist_ok=True)
            directory = Path(mkdtemp(prefix=datetime.now().strftime("rules-%Y%m%d-%H%M%S-"), dir=parent))
            snapshot = rules_snapshot(settings, directory, worker.policy.run_lark)
            rules = json.loads(snapshot.read_text(encoding="utf-8"))
            validate_full_rules(rules)
            emit({"status": "rules_validated", "topics": len(rules), "metrics": sum(len(v) for v in rules.values()),
                  "full_rule_count": sum(len(v) for v in rules.values()), "sha256": digest(snapshot), "task_created": False})
            return 0
        if args.action == "start":
            args.settings = settings_path
            identity = worker.request_identity(args.ccconnect_context)
            key = "walkthrough-" + hashlib.sha256(identity["source_message_id"].encode()).hexdigest()[:24]
            record = Path(settings["runtime_state_dir"]) / (key + ".json")
            start_job_entered = True
            result = worker.start_job(args)
            result["record_path"] = str(record)
            emit(result)
            return 0
        if (settings.get('execution_policy') or {}).get('version')=='walkthrough-lifecycle-v1':
            from native_lifecycle import enqueue_resume
            emit(enqueue_resume(settings,args.record,worker.policy.atomic_json))
            return 0
        code = worker.run_worker(args.record, settings_path)
        emit(record_status(args.record, settings))
        return code
    except Exception as exc:
        failure = {"status": "failed", **execution_failure_fields(exc, worker, InputError)}
        if (args.action in ('start', 'preflight', 'rules-check') and not start_job_entered
                and isinstance(exc, getattr(worker, 'PreflightError', ()))):
            failure['task_created'] = False
        emit(failure)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
