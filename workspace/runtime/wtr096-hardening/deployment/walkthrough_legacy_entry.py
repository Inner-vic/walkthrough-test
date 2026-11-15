"""Host entry for the independent walkthrough skill; local checks precede new jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

WORKSPACE = Path(__file__).resolve().parent.parent
SKILL = WORKSPACE / "runtime" / "wtl051" / "skill"
MANIFEST = WORKSPACE / "deployment" / "walkthrough-test.legacy051.installation.json"
ENVIRONMENT_KEYS = {
    "WALKTHROUGH_TEST_ORIGIN",
    "WALKTHROUGH_TEST_DOWNLOAD_HTTPS_HOSTS",
    "WALKTHROUGH_TEST_DOWNLOAD_HTTP_HOSTS",
    "WALKTHROUGH_PROD_ORIGIN",
    "WALKTHROUGH_PROD_DOWNLOAD_HTTPS_HOSTS",
    "WALKTHROUGH_PROD_DOWNLOAD_HTTP_HOSTS",
}
TEST_KEYS = {key for key in ENVIRONMENT_KEYS if "_TEST_" in key}


def emit(value):
    print(json.dumps(value, ensure_ascii=False))


def inspect_static_settings(settings, installation):
    """Check paths and the approved identity without importing a business worker."""
    identity = json.loads((WORKSPACE / "deployment" / "cc-connect-lark-isolation.json").read_text(encoding="utf-8-sig"))
    workspace = Path(settings["workspace_root"]).resolve()
    runtime = Path(settings["runtime_state_dir"]).resolve()
    if workspace != WORKSPACE.resolve() or runtime != (WORKSPACE / "data/walkthrough-test-fallback/runtime").resolve():
        raise ValueError("走查测试必须使用本工作区的独立 runtime。")
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
    path = Path(path).resolve()
    if path.parent != Path(settings["runtime_state_dir"]).resolve() or path.suffix != ".json":
        raise ValueError("任务记录必须来自本版独立 runtime。")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("kind") != "walkthrough_test_local" or path.stem != record.get("job_key"):
        raise ValueError("不是本版走查测试记录。")
    return {key: record[key] for key in ("job_key", "status", "error", "report_url", "analysis_stage", "analysis_windows",
                                        "execution_stage", "phase_timings", "startup_before_queue_seconds",
                                        "analysis_elapsed_seconds", "report_reply_message_id", "report_reply_mode") if key in record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("resume", "status", "wait"))
    parser.add_argument("--text")
    parser.add_argument("--ccconnect-context")
    parser.add_argument("--received-at", help="Internal fixed normalization clock; never inferred from message text.")
    parser.add_argument("--interpretation", type=json.loads, help="Validated semantic proposal JSON; file paths are not accepted.")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=45)
    args = parser.parse_args()
    if args.action=='resume':
        final=args.record.with_suffix('.result.json') if args.record is not None else None
        if final is None or not final.is_file():
            emit({'status':'failed','error':'旧任务没有最终结果；不会自动重算或重查制品，请使用新业务消息。'})
            return 1
    installation = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    host_python = Path(installation["host_python"]).resolve()
    if args.action != "route" and Path(sys.executable).resolve() != host_python:
        if not host_python.is_file():
            emit({"status": "deployment_incomplete", "missing": ["独立 CPython 3.11 环境"], "task_created": False})
            return 1
        result = subprocess.run([str(host_python), "-X", "utf8", str(Path(__file__).resolve()), *sys.argv[1:]],
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
    from walkthrough_route import route, source_normalization_anchor
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
                    expected_runtime = (WORKSPACE / "data/walkthrough-test-fallback/runtime").resolve()
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
    if args.action in ("resume", "status", "wait") and args.record is None:
        parser.error("--record is required")
    if not 0 <= args.wait_seconds <= 50:
        parser.error("--wait-seconds must be between 0 and 50")
    settings_path = Path(installation["settings_path"])
    if not settings_path.is_file():
        emit({"status": "deployment_incomplete", "missing": ["走查测试独立配置"], "task_created": False})
        return 1
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
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
    if args.action in ("start", "resume") and installation.get("execution_enabled") is not True:
        emit({"status": "execution_disabled", "static_checks": static,
              "message": "走查测试已暂停执行；请查看当前部署状态。",
              "task_created": False})
        return 1
    if args.action == "start" and not args.ccconnect_context:
        emit({"status": "needs_input", "message": "缺少 CC Connect 原始来源上下文，未创建任务。"})
        return 1
    local = settings.get("local_analysis") or {}
    missing = []
    if local.get("backend") != "memory" or local.get("environment") not in ("test_then_prod", "test", "prod"):
        missing.append("memory 后端及明确环境策略")
    cloud_env = settings.get("cloud_environment") or {}
    # Production settings are optional until a window actually needs fallback.
    if not TEST_KEYS.issubset(cloud_env) or set(cloud_env) - ENVIRONMENT_KEYS or any(not isinstance(v, str) for v in cloud_env.values()):
        missing.append("WALKTHROUGH 专用车云环境配置")
    if missing:
        emit({"status": "deployment_incomplete", "static_checks": static, "missing": missing,
              "message": "走查测试 memory 后端配置不完整，未创建业务任务。",
              "task_created": False})
        return 1
    # Set only this process's walkthrough variables before importing its client.
    for key in ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(cloud_env)
    os.environ["PYTHONUTF8"] = "1"
    import walkthrough_worker as worker
    from request_model import InputError
    try:
        if args.action in ("preflight", "rules-check", "start"):
            from preflight import check
            checked = check(settings_path)
            if args.action == "preflight":
                emit(checked)
                return 0
        if args.action == "rules-check":
            from datetime import datetime
            from direct_ad import compile_rules
            from native_log_source import rules_snapshot, digest
            from tempfile import mkdtemp
            settings = worker.load_settings(settings_path)
            worker.policy.ensure_user_authorization(settings)
            parent = settings_path.parent / "deployment-validation"
            parent.mkdir(exist_ok=True)
            directory = Path(mkdtemp(prefix=datetime.now().strftime("rules-%Y%m%d-%H%M%S-"), dir=parent))
            snapshot = rules_snapshot(settings, directory, worker.policy.run_lark)
            rules = json.loads(snapshot.read_text(encoding="utf-8"))
            _, compiled = compile_rules(rules)
            emit({"status": "rules_validated", "topics": len(rules), "metrics": sum(len(v) for v in rules.values()),
                  "supported_ad_rules": len(compiled), "sha256": digest(snapshot), "task_created": False})
            return 0
        if args.action == "start":
            args.settings = settings_path
            identity = worker.request_identity(args.ccconnect_context)
            key = "walkthrough-" + hashlib.sha256(identity["source_message_id"].encode()).hexdigest()[:24]
            record = Path(settings["runtime_state_dir"]) / (key + ".json")
            result = worker.start_job(args)
            result["record_path"] = str(record)
            emit(result)
            return 0
        code = worker.run_worker(args.record, settings_path)
        emit(record_status(args.record, settings))
        return code
    except Exception as exc:
        message = str(exc) if isinstance(exc, InputError) else "走查测试检查或执行失败，请查看独立配置和任务诊断。"
        emit({"status": "failed", "error": message})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
