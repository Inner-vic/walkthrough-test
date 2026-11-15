"""Send bounded, credential-free auth notices through the project's report bot.

The maintenance entry point calls this function while holding lark_auth_lock.
Only the fixed dispatcher may receive notices; CLI identity is always explicit.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILENAME = "feishu-user-auth-alert-state.json"
BEIJING = timezone(timedelta(hours=8))


def _now(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("now must be a datetime")
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _result(status: str, reason: str, message_id: str | None = None) -> dict:
    result = {"status": status, "reason": reason}
    if message_id:
        result["message_id"] = message_id
    return result


def _configuration(manifest: dict, settings: dict) -> tuple[dict | None, str]:
    maintenance = manifest.get("auth_maintenance")
    notifications = maintenance.get("notifications") if isinstance(maintenance, dict) else None
    if not isinstance(notifications, dict) or notifications.get("enabled") is not True:
        return None, "notifications_disabled"
    expected = str(manifest.get("expected_dispatch_open_id") or "").strip()
    recipient = str(notifications.get("recipient_open_id") or "").strip()
    configured_expected = str(settings.get("expected_dispatch_open_id") or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", expected) or recipient != expected or configured_expected != expected:
        return None, "recipient_identity_mismatch"
    profiles = manifest.get("profiles")
    report_profile = str(profiles.get("report_bot") or "").strip() if isinstance(profiles, dict) else ""
    dispatch_profile = str(profiles.get("dispatch_user") or "").strip() if isinstance(profiles, dict) else ""
    profile = str(notifications.get("profile") or "").strip()
    if not profile or profile != report_profile or profile != settings.get("report_lark_profile") or profile == dispatch_profile or profile == settings.get("lark_profile"):
        return None, "report_profile_mismatch"
    project = str(manifest.get("project_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project) or settings.get("project_id") != project:
        return None, "project_identity_mismatch"
    app = str(manifest.get("expected_lark_app_id") or "").strip()
    if not app or settings.get("expected_lark_app_id") != app:
        return None, "app_identity_mismatch"
    workspace = Path(str(manifest.get("workspace_root") or "")).expanduser()
    if not workspace.is_absolute():
        return None, "workspace_unavailable"
    cli = str(manifest.get("lark_cli") or "").strip()
    home = str(manifest.get("lark_home") or "").strip()
    if not cli or not home or cli != settings.get("lark_cli") or home != settings.get("lark_home"):
        return None, "project_cli_mismatch"
    try:
        hours = float(notifications.get("reminder_hours", 24))
        if not math.isfinite(hours) or hours < 24 or hours > 8760:
            return None, "reminder_interval_invalid"
    except (TypeError, ValueError):
        return None, "reminder_interval_invalid"
    return {"project": project, "recipient": expected, "profile": profile, "app": app,
            "cli": cli, "home": home, "workspace": workspace, "reminder_hours": hours}, ""


def _kind(health: dict) -> str:
    status = str(health.get("status") or "").casefold()
    if health.get("requiresReauthorization") is True or status in {"reauthorization_required", "reauthorize", "expired"}:
        return "reauthorization_required"
    reason = str(health.get("reason") or "").casefold()
    if status in {"near_refresh_expiry", "near_refresh_warning"} or reason == "refresh_near_expiry":
        return "refresh_near_expiry"
    if health.get("ok") is True or status == "healthy":
        return "healthy"
    if reason in {"identity_mismatch", "app_mismatch", "configuration_mismatch", "scope_missing", "missing_scopes", "required_scopes_missing"}:
        return "configuration_attention"
    return "verification_unavailable"


def _message(config: dict, kind: str, health: dict) -> str:
    prefix = f"底盘智枢（{config['project']}）飞书授权"
    if kind == "healthy":
        return prefix + "已恢复：固定派发账号和业务侧校验已通过，自动维护将继续运行。"
    explanations = {
        "reauthorization_required": "固定派发账号需要本人重新完成授权。",
        "refresh_near_expiry": "固定派发账号的刷新授权接近到期，自动维护将继续校验。",
        "configuration_attention": "固定派发账号的身份或必需权限校验未通过，需要检查项目授权配置。",
        "verification_unavailable": "固定派发账号连续校验未通过，系统将继续自动重试。",
    }
    message = prefix + "提醒：" + explanations[kind]
    expiry = _date(health.get("refreshExpiresAt") or health.get("refresh_expires_at"))
    if expiry is not None:
        message += "刷新授权到期时间：" + expiry.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）。"
    message += f"若提示需要重新授权，请在原业务会话中联系助手，重新完成 {config['project']} OAuth 授权；无需向任何人提供令牌或密钥。"
    return message


def _environment(config: dict) -> dict:
    environment = dict(os.environ)
    environment.update({"USERPROFILE": config["home"], "HOME": config["home"],
                        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"})
    return environment


def _payload(completed) -> dict | None:
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("ok") is not False else None


def _bot_available(config: dict, runner) -> bool:
    command = [config["cli"], "auth", "status", "--profile", config["profile"], "--json"]
    completed = runner(command, capture_output=True, text=True, encoding="utf-8", timeout=30,
                       env=_environment(config), cwd=str(config["workspace"]))
    payload = _payload(completed)
    if payload is None:
        return False
    if isinstance(payload.get("data"), dict) and "appId" not in payload:
        payload = payload["data"]
    identities = payload.get("identities")
    if not isinstance(identities, dict) or payload.get("appId") != config["app"]:
        return False
    bot, user = identities.get("bot"), identities.get("user")
    return (isinstance(bot, dict) and bot.get("available") is True
            and not (isinstance(user, dict) and user.get("available") is True))


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _failure_count(health: dict) -> int:
    try:
        return int(health.get("consecutiveFailures") or health.get("consecutive_failures") or 0)
    except (ValueError, TypeError, OverflowError):
        return 0


def notify_auth_health(manifest: dict, settings: dict, health: dict, now=None, runner=None) -> dict:
    """Notify only the pinned dispatcher; caller must hold the shared auth lock.

    Neither health/CLI error strings nor credential fields enter notices or state.
    Failed/ambiguous sends retain a stable idempotency key for the next retry.
    """
    config, reason = _configuration(manifest, settings)
    if config is None:
        return _result("disabled" if reason == "notifications_disabled" else "failed", reason)
    current = _now(now)
    path = config["workspace"] / "data" / STATE_FILENAME
    try:
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1}
        if not isinstance(state, dict):
            return _result("failed", "alert_state_unreadable")
    except (OSError, ValueError):
        return _result("failed", "alert_state_unreadable")
    kind = _kind(health)
    previous = str(state.get("last_observed_status") or "")
    state["last_observed_status"] = kind
    state["checked_at"] = _stamp(current)
    if kind == "healthy" and state.get("incident_open") is not True:
        state.pop("pending", None)
        decision = _result("suppressed", "healthy_without_previous_alert")
    elif kind == "verification_unavailable" and _failure_count(health) < 3:
        decision = _result("suppressed", "transient_failure_below_threshold")
    else:
        last_time = _date(state.get("last_delivered_at"))
        if (kind == state.get("last_delivered_status") and previous == kind and last_time is not None
                and current - last_time < timedelta(hours=config["reminder_hours"])):
            decision = _result("suppressed", "reminder_interval_not_elapsed")
        else:
            decision = None
    if decision is not None:
        try:
            _write_state(path, state)
        except OSError:
            return _result("failed", "alert_state_write_failed")
        return decision
    pending = state.get("pending")
    if (not isinstance(pending, dict) or pending.get("kind") != kind
            or not re.fullmatch(r"auth-[0-9a-f]{40}", str(pending.get("idempotency_key") or ""))):
        boundary = "\0".join((config["project"], config["recipient"], kind, _stamp(current)))
        key = "auth-" + hashlib.sha256(boundary.encode("utf-8")).hexdigest()[:40]
        pending = {"kind": kind, "idempotency_key": key, "created_at": _stamp(current)}
        state["pending"] = pending
    state["last_attempt_at"] = _stamp(current)
    # Persist the key before the external write so a crash can retry safely.
    try:
        _write_state(path, state)
    except OSError:
        return _result("failed", "alert_state_write_failed")
    invoke = runner or subprocess.run
    decision = _result("failed", "notification_send_failed")
    try:
        if not _bot_available(config, invoke):
            decision = _result("failed", "report_bot_unavailable")
        else:
            command = [config["cli"], "im", "+messages-send", "--user-id", config["recipient"],
                       "--as", "bot", "--text", _message(config, kind, health),
                       "--idempotency-key", pending["idempotency_key"], "--profile", config["profile"], "--format", "json"]
            completed = invoke(command, capture_output=True, text=True, encoding="utf-8", timeout=60,
                               env=_environment(config), cwd=str(config["workspace"]))
            payload = _payload(completed)
            data = payload.get("data") if payload else None
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if payload and payload.get("ok") is True and payload.get("identity") == "bot" and isinstance(message_id, str) and re.fullmatch(r"om_[A-Za-z0-9_-]+", message_id):
                state.update(last_delivered_status=kind, last_delivered_at=_stamp(current),
                             message_id=message_id, incident_open=(kind != "healthy"))
                state.pop("pending", None)
                decision = _result("sent", "recovery" if kind == "healthy" else kind, message_id)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        # Do not serialize exceptions: CLI error text can contain credentials.
        pass
    state["last_attempt_result"] = decision
    try:
        _write_state(path, state)
    except OSError:
        return _result("failed", "alert_state_write_failed", decision.get("message_id"))
    return decision
