"""Maintain the pinned project Feishu credentials without starting OAuth or jobs.

Only ``auth status --verify`` may refresh credentials.  A successful remote
verification is followed by a fresh local read and the production worker's
authorization preflight, all under the same cross-process credential lock.
Health files contain a fixed allowlist of metadata, never CLI output or tokens.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from lark_auth_lock import lark_auth_lock

ACCESS_MARGIN_SECONDS = 15 * 60
REFRESH_WARNING_SECONDS = 48 * 60 * 60
VERIFY_ATTEMPTS = 3
RETRY_DELAYS = (2, 4)
HISTORY_LIMIT = 64
REQUIRED_USER_SCOPES = frozenset({
    "offline_access", "docs:secure_label:readonly", "docs:secure_label:write_only",
})
SAFE_REASONS = frozenset({
    "healthy", "configuration_unreadable", "configuration_invalid", "manifest_invalid",
    "isolation_invalid", "manifest_settings_mismatch", "project_runtime_unavailable",
    "cli_version_unavailable", "cli_version_mismatch", "administrator_action_required",
    "reauthorization_required", "required_scopes_missing", "verification_unavailable",
    "identity_mismatch", "refresh_expiry_unknown", "verification_required", "refresh_near_expiry",
    "business_preflight_unavailable", "business_preflight_failed", "credential_lock_timeout",
    "maintenance_unavailable", "health_persistence_failed",
    "maintenance_policy_invalid",
    "credential_replica_invalid",
    "credential_replica_unavailable",
    "health_state_lock_timeout",
})


class MaintenanceError(Exception):
    """An already sanitized failure classification, safe for a health record."""

    def __init__(self, reason: str, *, reauthorize: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.reauthorize = reauthorize


def utc_time(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_expiry(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return utc_time(parsed)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise MaintenanceError("configuration_unreadable") from None
    if not isinstance(value, dict):
        raise MaintenanceError("configuration_invalid")
    return value


def same_path(first, second) -> bool:
    return (isinstance(first, str) and isinstance(second, str)
            and os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second)))


def load_configuration(manifest_path: Path) -> tuple[dict, dict]:
    manifest = read_json(manifest_path)
    required = ("project_id", "workspace_root", "settings_path", "lark_home", "lark_cli",
                "expected_lark_app_id", "expected_dispatch_open_id")
    if any(not isinstance(manifest.get(key), str) or not manifest[key].strip() for key in required):
        raise MaintenanceError("manifest_invalid")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or any(
        not isinstance(profiles.get(key), str) or not profiles[key].strip()
        for key in ("dispatch_user", "report_bot")
    ):
        raise MaintenanceError("manifest_invalid")
    if manifest.get("active_mode") != "project_home" or profiles["dispatch_user"] == profiles["report_bot"]:
        raise MaintenanceError("isolation_invalid")
    if any(not value.startswith(manifest["project_id"] + "-") for value in profiles.values()):
        raise MaintenanceError("isolation_invalid")
    settings = read_json(Path(manifest["settings_path"]))
    pinned = {
        "project_id": manifest["project_id"],
        "lark_profile": profiles["dispatch_user"],
        "report_lark_profile": profiles["report_bot"],
        "expected_lark_app_id": manifest["expected_lark_app_id"],
        "expected_dispatch_open_id": manifest["expected_dispatch_open_id"],
    }
    if settings.get("project_isolation_required") is not True or any(
        settings.get(key) != expected for key, expected in pinned.items()
    ) or any(not same_path(settings.get(key), manifest[key]) for key in ("lark_cli", "lark_home")):
        raise MaintenanceError("manifest_settings_mismatch")
    home = Path(manifest["lark_home"])
    if not home.is_absolute() or same_path(str(home), str(Path.home())):
        raise MaintenanceError("isolation_invalid")
    replica_key = "auth_credential_replica_path"
    if replica_key in manifest or replica_key in settings:
        replica, settings_replica = manifest.get(replica_key), settings.get(replica_key)
        if (not isinstance(replica, str) or not replica.strip()
                or not isinstance(settings_replica, str) or not settings_replica.strip()):
            raise MaintenanceError("credential_replica_invalid")
        if not same_path(replica, settings_replica):
            raise MaintenanceError("manifest_settings_mismatch")
        private_root = (home / ".lark-cli").resolve()
        replica_path = Path(replica)
        resolved_replica = replica_path.resolve()
        if (not replica_path.is_absolute() or resolved_replica == private_root
                or not resolved_replica.is_relative_to(private_root)):
            raise MaintenanceError("credential_replica_invalid")
    if not (home / ".lark-cli" / "config.json").is_file() or not Path(manifest["lark_cli"]).is_file():
        raise MaintenanceError("project_runtime_unavailable")
    scopes = manifest.get("required_user_scopes", [])
    if not isinstance(scopes, list) or any(not isinstance(scope, str) or not scope for scope in scopes):
        raise MaintenanceError("manifest_invalid")
    manifest["required_user_scopes"] = sorted(set(scopes) | REQUIRED_USER_SCOPES)
    return manifest, settings


def load_policy(manifest: dict) -> dict:
    configured = manifest.get("auth_maintenance", {})
    if not isinstance(configured, dict):
        raise MaintenanceError("maintenance_policy_invalid")
    limits = {
        "access_refresh_margin_seconds": (ACCESS_MARGIN_SECONDS, ACCESS_MARGIN_SECONDS, 24 * 60 * 60),
        "refresh_warning_seconds": (REFRESH_WARNING_SECONDS, REFRESH_WARNING_SECONDS, 30 * 24 * 60 * 60),
        "auth_verify_attempts": (VERIFY_ATTEMPTS, 1, VERIFY_ATTEMPTS),
        "retry_delay_seconds": (RETRY_DELAYS[0], 1, 30),
        "lock_timeout_seconds": (60, 1, 120),
    }
    policy = {}
    for key, (default, minimum, maximum) in limits.items():
        value = configured.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise MaintenanceError("maintenance_policy_invalid")
        policy[key] = value
    return policy


def lark_environment(settings: dict) -> dict:
    environment = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    environment["USERPROFILE"] = str(Path(settings["lark_home"]).resolve())
    environment["HOME"] = environment["USERPROFILE"]
    return environment


def cli_version(settings: dict) -> str:
    try:
        completed = subprocess.run([settings["lark_cli"], "--version"], capture_output=True,
                                   text=True, encoding="utf-8", timeout=15, cwd=str(HERE.parent),
                                   env=lark_environment(settings))
    except (OSError, subprocess.TimeoutExpired):
        raise MaintenanceError("cli_version_unavailable") from None
    match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?)\b", completed.stdout)
    if completed.returncode or match is None:
        raise MaintenanceError("cli_version_unavailable")
    return match.group(1)


def credential_store_fingerprint(settings: dict) -> str | None:
    """Correlate the DPAPI-encrypted registry record without exposing its value."""
    if os.name != "nt":
        return None
    try:
        import winreg
        account = settings["expected_lark_app_id"] + ":" + settings["expected_dispatch_open_id"]
        name = base64.urlsafe_b64encode(account.encode("utf-8")).decode("ascii").rstrip("=")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\LarkCli\keychain\lark-cli") as key:
            encrypted, _ = winreg.QueryValueEx(key, name)
        if not isinstance(encrypted, str):
            return None
        return hashlib.sha256(encrypted.encode("utf-8")).hexdigest()
    except (OSError, KeyError, ValueError):
        return None


def classify_cli_error(payload: dict) -> MaintenanceError:
    error = payload.get("error")
    error = error if isinstance(error, dict) else {}
    values = [error.get(key) for key in ("type", "subtype", "code", "message")]
    diagnostic = " ".join(str(value) for value in values if value is not None).casefold()
    normalized = diagnostic.replace("_", " ").replace("-", " ")
    if str(error.get("code")) == "20140":
        return MaintenanceError("administrator_action_required")
    if ("invalid grant" in normalized or "invalid refresh token" in normalized
            or ("refresh" in normalized and any(word in normalized for word in ("expired", "revoked")))):
        return MaintenanceError("reauthorization_required", reauthorize=True)
    if any(word in normalized for word in ("missing scope", "insufficient scope")):
        return MaintenanceError("required_scopes_missing", reauthorize=True)
    return MaintenanceError("verification_unavailable")


def cli_status(settings: dict, profile: str, verify: bool = False) -> dict:
    environment = lark_environment(settings)
    command = [settings["lark_cli"], "auth", "status", "--profile", profile, "--json"]
    if verify:
        command.append("--verify")
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   timeout=30, env=environment, cwd=str(HERE.parent))
    except (OSError, subprocess.TimeoutExpired):
        raise MaintenanceError("verification_unavailable") from None
    raw = (completed.stdout or completed.stderr) if completed.returncode == 0 else (completed.stderr or completed.stdout)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise MaintenanceError("verification_unavailable") from None
    if not isinstance(payload, dict):
        raise MaintenanceError("verification_unavailable")
    if completed.returncode or payload.get("ok") is False:
        raise classify_cli_error(payload)
    if isinstance(payload.get("data"), dict) and "identities" in payload["data"]:
        payload = payload["data"]
    return payload


def user_identity(payload: dict) -> dict:
    identities = payload.get("identities")
    user = identities.get("user") if isinstance(identities, dict) else None
    return user if isinstance(user, dict) else {}


def guard_identity(manifest: dict, payload: dict) -> None:
    app_id = payload.get("appId")
    open_id = user_identity(payload).get("openId")
    if app_id and app_id != manifest["expected_lark_app_id"]:
        raise MaintenanceError("identity_mismatch")
    if open_id and open_id != manifest["expected_dispatch_open_id"]:
        raise MaintenanceError("identity_mismatch")


def local_health(manifest: dict, payload: dict, now: datetime, *, access_margin: int = ACCESS_MARGIN_SECONDS,
                 refresh_warning: int = REFRESH_WARNING_SECONDS) -> dict:
    user = user_identity(payload)
    expiry = parse_expiry(user.get("expiresAt"))
    refresh_expiry = parse_expiry(user.get("refreshExpiresAt") or user.get("refreshTokenExpiresAt"))
    scope = user.get("scope")
    scope_known = isinstance(scope, str) or (isinstance(scope, list) and all(isinstance(item, str) for item in scope))
    granted = set(scope.split()) if isinstance(scope, str) else set(scope) if scope_known else set()
    missing = sorted(set(manifest["required_user_scopes"]) - granted)
    refresh_status = str(user.get("refreshTokenStatus") or "").casefold()
    checks = {
        "appIdMatches": payload.get("appId") == manifest["expected_lark_app_id"],
        "openIdMatches": user.get("openId") == manifest["expected_dispatch_open_id"],
        "userAvailable": user.get("available") is True,
        "accessTokenValid": str(user.get("tokenStatus") or "").casefold() == "valid",
        "accessExpirySafe": expiry is not None and expiry > now + timedelta(seconds=access_margin),
        "refreshExpiryValid": refresh_expiry is not None and refresh_expiry > now,
        "refreshTokenNotRevoked": refresh_status not in {"revoked", "expired", "invalid"},
        "requiredScopesPresent": not missing,
    }
    refresh_near = refresh_expiry is not None and refresh_expiry <= now + timedelta(seconds=refresh_warning)
    reason = "healthy"
    needs_reauth = False
    if refresh_status in {"revoked", "expired", "invalid"} or (refresh_expiry is not None and refresh_expiry <= now):
        reason, needs_reauth = "reauthorization_required", True
    elif missing and scope_known:
        reason, needs_reauth = "required_scopes_missing", True
    elif not checks["refreshExpiryValid"]:
        reason = "refresh_expiry_unknown"
    elif not all(checks.values()):
        reason = "verification_required"
    elif refresh_near:
        reason = "refresh_near_expiry"
    return {
        "checks": checks, "missingScopes": missing,
        "expiresAt": expiry.isoformat() if expiry else None,
        "refreshExpiresAt": refresh_expiry.isoformat() if refresh_expiry else None,
        "refreshNearExpiry": refresh_near, "reason": reason,
        "requiresReauthorization": needs_reauth,
        "locallyValid": all(checks.values()),
    }


def rotation_observed(before: dict, after: dict) -> bool:
    for key in ("expiresAt", "refreshExpiresAt"):
        previous, current = parse_expiry(before.get(key)), parse_expiry(after.get(key))
        if previous is not None and current is not None and current > previous:
            return True
    return False


def load_worker(workspace_root: Path):
    # Standalone export: use the walkthrough policy instead of requiring the
    # unrelated AD P99 execution service. The three preflight APIs are shared.
    workspace_root = Path(workspace_root).resolve()
    try:
        installation = read_json(workspace_root / 'deployment/walkthrough-test.installation.json')
        scripts = workspace_root / 'runtime/wtr096-hardening/skill/scripts'
        if (Path(installation['workspace_root']).resolve() != workspace_root
                or Path(installation['skill_root']).resolve() != scripts.parent
                or installation['execution_release'] != 'walkthrough-target16-hardening'):
            raise ValueError()
        path = scripts / 'report_policy.py'
        for parent in (path, *path.parents):
            info = parent.lstat()
            if parent.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError()
    except (OSError, ValueError, KeyError):
        raise MaintenanceError('business_preflight_unavailable') from None
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("_ccconnect_auth_maintenance_worker", path)
    if spec is None or spec.loader is None:
        raise MaintenanceError("business_preflight_unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        module.WORKSPACE_ROOT = workspace_root
    except Exception:
        raise MaintenanceError("business_preflight_unavailable") from None
    return module


def business_preflight(worker, settings: dict, now: datetime) -> None:
    """Run production authorization checks, without dispatching any business work."""
    try:
        worker.ensure_user_authorization(settings, settings["expected_dispatch_open_id"], now=now)
        worker.ensure_report_bot_authorization(settings)
        worker.ensure_secure_label_authorization(settings)
    except Exception:
        # Worker exceptions may contain arbitrary CLI strings: never persist them.
        raise MaintenanceError("business_preflight_failed") from None


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_previous(path: Path) -> dict:
    try:
        return read_json(path)
    except MaintenanceError:
        return {}


def snapshot(result: dict) -> dict:
    value = {}
    for key in ("checkedAt", "expiresAt", "refreshExpiresAt"):
        parsed = parse_expiry(result.get(key))
        if parsed is not None:
            value[key] = parsed.isoformat()
        elif key in ("expiresAt", "refreshExpiresAt"):
            value[key] = None
    if result.get("status") in {"healthy", "error", "warning", "reauthorization_required"}:
        value["status"] = result["status"]
    if result.get("reason") in SAFE_REASONS:
        value["reason"] = result["reason"]
    if isinstance(result.get("requiresReauthorization"), bool):
        value["requiresReauthorization"] = result["requiresReauthorization"]
    return value


def persist_health(path: Path, result: dict) -> None:
    previous = safe_previous(path)
    for key in ("latestSuccess", "latestFailure"):
        if isinstance(previous.get(key), dict):
            result[key] = snapshot(previous[key])
    result["latestSuccess" if result["ok"] else "latestFailure"] = snapshot(result)
    history_path = path.with_name(path.stem + "-history.json")
    history = safe_previous(history_path).get("transitions", [])
    history = [snapshot(item) for item in history[-HISTORY_LIMIT:] if isinstance(item, dict)] if isinstance(history, list) else []
    if (previous.get("status"), previous.get("reason")) != (result["status"], result["reason"]) or result.get("tokenRotationObserved"):
        history.append(snapshot(result))
    atomic_json(path, result)
    atomic_json(history_path, {"schemaVersion": 1, "transitions": history[-HISTORY_LIMIT:]})
    warning_path = path.with_name(path.stem + "-warning.json")
    warning = {"active": not result["ok"], **snapshot(result)}
    atomic_json(warning_path, warning)


def notify_health(manifest: dict, settings: dict, result: dict, now: datetime) -> dict:
    """The deployment notifier sends only to the separately pinned dispatcher."""
    try:
        from lark_auth_alerts import notify_auth_health
        return notify_auth_health(manifest, settings, result, now=now)
    except Exception:
        return {"status": "failed", "reason": "notification_unavailable"}


def _finish_health_unlocked(path: Path, result: dict, *, manifest: dict | None = None,
                            settings: dict | None = None, now: datetime, notifier: Callable,
                            capture_fingerprint: bool = True) -> dict:
    previous = safe_previous(path)
    failures = previous.get("consecutiveFailures", 0)
    failures = failures if isinstance(failures, int) and not isinstance(failures, bool) and failures >= 0 else 0
    result["consecutiveFailures"] = 0 if result["ok"] else failures + 1
    if settings is not None and capture_fingerprint:
        # The outer shared lock has already imported the canonical encrypted
        # replica.  Capture the current registry record, including any refresh,
        # while that lock still protects the health result and its provenance.
        result.setdefault("executionContext", {})["credentialStoreFingerprint"] = credential_store_fingerprint(settings)
    if result.get("checkOnly"):
        result["notification"] = {"status": "disabled", "reason": "check_only"}
    elif manifest is not None and settings is not None:
        # Normal checks retain the credential lock.  Storage/lock failures may
        # instead use the independently validated report Bot, without user auth.
        notification = notifier(manifest, settings, result, now)
        safe_notification = {"status": "failed", "reason": "notification_unavailable"}
        if isinstance(notification, dict) and notification.get("status") in {"sent", "suppressed", "failed", "disabled"}:
            safe_notification["status"] = notification["status"]
            reason = notification.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,80}", reason):
                safe_notification["reason"] = reason
            message_id = notification.get("message_id")
            if isinstance(message_id, str) and re.fullmatch(r"om_[A-Za-z0-9]+", message_id):
                safe_notification["message_id"] = message_id
        result["notification"] = safe_notification
    try:
        persist_health(path, result)
    except OSError:
        result.update(ok=False, status="error", reason="health_persistence_failed")
    return result


def alert_health_lock(settings: dict):
    """Coordinate health/notice state without importing a user credential replica."""
    lock_settings = {"lark_home": settings["lark_home"],
                     "expected_lark_app_id": settings["expected_lark_app_id"] + "-auth-health-alerts"}
    # The two-key lock settings intentionally omit the replica path, dispatch
    # profile and user ID.  The shared OS lock code performs no credential-store
    # work when replication is disabled for this separate alert-state lock.
    return lark_auth_lock(lock_settings, timeout=10)


def finish_health(path: Path, result: dict, *, manifest: dict | None = None,
                  settings: dict | None = None, now: datetime, notifier: Callable,
                  capture_fingerprint: bool = True) -> dict:
    if settings is None:
        return _finish_health_unlocked(path, result, manifest=manifest, settings=settings,
                                       now=now, notifier=notifier, capture_fingerprint=False)
    try:
        # Lock order is user credentials -> alert state on the normal path;
        # failover takes only alert state and never reacquires user credentials.
        with alert_health_lock(settings):
            return _finish_health_unlocked(path, result, manifest=manifest, settings=settings,
                                           now=now, notifier=notifier, capture_fingerprint=capture_fingerprint)
    except TimeoutError:
        result.update(ok=False, status="error", reason="health_state_lock_timeout",
                      notification={"status": "failed", "reason": "alert_state_lock_timeout"})
        # Another owner controls the health state; do not race its atomic write.
        return result


def commit_credentials(auth_guard, settings: dict) -> None:
    """Commit the canonical encrypted store before any success is observable.

    Legacy test lock factories may yield None while credential replication is
    disabled.  An enabled replica always requires the shared lock commit API.
    """
    commit = getattr(auth_guard, "commit", None)
    if callable(commit):
        commit()
    elif settings.get("auth_credential_replica_path"):
        raise MaintenanceError("credential_replica_unavailable")


def maintain(manifest_path: Path, health_path: Path, *, check_only: bool = False,
             force_verify: bool = False, now: datetime | None = None,
             status_reader: Callable = cli_status, sleeper: Callable = time.sleep,
             worker_loader: Callable = load_worker, lock_factory: Callable = lark_auth_lock,
             version_reader: Callable = cli_version, notifier: Callable = notify_health) -> dict:
    current = utc_time(now)
    lock_entered = False
    configuration_validated = False
    result = {"schemaVersion": 2, "checkedAt": current.isoformat(), "ok": False,
              "status": "error", "reason": "verification_unavailable", "requiresReauthorization": False,
              "verificationAttempts": 0, "verificationTriggered": False, "tokenRotationObserved": False,
              "businessPreflightPassed": False, "checkOnly": check_only,
              "executionContext": {
                  "user": os.environ.get("USERNAME"),
                  "domain": os.environ.get("USERDOMAIN"),
                  "appData": os.environ.get("APPDATA"),
                  "localAppData": os.environ.get("LOCALAPPDATA"),
                  "temp": os.environ.get("TEMP"),
                  "credentialEnvironmentNames": sorted(
                      name for name in os.environ if re.search(r"LARK|FEISHU|KEYRING|KEYCHAIN|CONFIG_DIR", name)
                  ),
              }}
    try:
        manifest, settings = load_configuration(manifest_path)
        policy = load_policy(manifest)
        configuration_validated = True
        result.update({"projectId": manifest["project_id"], "profile": manifest["profiles"]["dispatch_user"],
                       "reportProfile": manifest["profiles"]["report_bot"], "projectLarkHome": manifest["lark_home"],
                       "larkCli": manifest["lark_cli"]})
        result["policy"] = policy
        profile = manifest["profiles"]["dispatch_user"]
        with lock_factory(settings, timeout=policy["lock_timeout_seconds"]) as auth_guard:
            lock_entered = True
            result["executionContext"]["credentialStoreFingerprint"] = credential_store_fingerprint(settings)
            expected_version = manifest.get("expected_cli_version")
            if expected_version:
                result["cliVersion"] = version_reader(settings)
                if result["cliVersion"] != expected_version:
                    raise MaintenanceError("cli_version_mismatch")
            local = {}
            local_error = None
            try:
                local = status_reader(settings, profile, False)
                guard_identity(manifest, local)
            except MaintenanceError as error:
                if error.reason in {"identity_mismatch", "administrator_action_required"}:
                    raise
                local_error = error
            state_args = {"access_margin": policy["access_refresh_margin_seconds"],
                          "refresh_warning": policy["refresh_warning_seconds"]}
            before = state = local_health(manifest, local, current, **state_args)
            should_verify = force_verify or local_error is not None or not state["locallyValid"] or state["refreshNearExpiry"]
            last_error = local_error
            if should_verify and not check_only:
                result["verificationTriggered"] = True
                for attempt in range(policy["auth_verify_attempts"]):
                    result["verificationAttempts"] = attempt + 1
                    try:
                        verified = status_reader(settings, profile, True)
                        guard_identity(manifest, verified)
                        verified_user = user_identity(verified)
                        if (verified_user.get("verified") is not True or verified_user.get("available") is not True
                                or str(verified_user.get("tokenStatus") or "").casefold() != "valid"):
                            raise MaintenanceError("verification_unavailable")
                        local = status_reader(settings, profile, False)
                        guard_identity(manifest, local)
                        state = local_health(manifest, local, current, **state_args)
                        last_error = None
                        result["tokenRotationObserved"] |= rotation_observed(before, state)
                        if state["locallyValid"] and not state["refreshNearExpiry"]:
                            break
                    except MaintenanceError as error:
                        last_error = error
                        if error.reason in {"identity_mismatch", "administrator_action_required"}:
                            raise
                    if attempt + 1 < policy["auth_verify_attempts"]:
                        sleeper(policy["retry_delay_seconds"] * (2 ** attempt))
                # Always inspect persisted credentials after the final attempt, even
                # when verification returned an error after updating local storage.
                try:
                    local = status_reader(settings, profile, False)
                    guard_identity(manifest, local)
                    state = local_health(manifest, local, current, **state_args)
                    result["tokenRotationObserved"] |= rotation_observed(before, state)
                except MaintenanceError as error:
                    if error.reason in {"identity_mismatch", "administrator_action_required"}:
                        raise
                    last_error = error
            result.update({key: value for key, value in state.items() if key != "locallyValid"})
            if state["requiresReauthorization"]:
                raise MaintenanceError(state["reason"], reauthorize=True)
            if last_error is not None:
                raise last_error
            if not state["locallyValid"]:
                raise MaintenanceError(state["reason"], reauthorize=state["requiresReauthorization"])
            if check_only and should_verify:
                result.update(status="warning", reason="verification_required")
            else:
                worker_settings = dict(settings, auth_refresh_margin_seconds=policy["access_refresh_margin_seconds"],
                                       auth_verify_attempts=policy["auth_verify_attempts"])
                business_preflight(worker_loader(Path(manifest["workspace_root"])), worker_settings, current)
                result["businessPreflightPassed"] = True
                if state["refreshNearExpiry"]:
                    result.update(status="warning", reason="refresh_near_expiry")
                else:
                    result.update(ok=True, status="healthy", reason="healthy")
            # A successful worker preflight is insufficient until a refreshed
            # encrypted credential is durable in the shared canonical store.
            # Commit first so failure cannot emit a recovery notification or
            # advance latestSuccess in the health record.
            commit_credentials(auth_guard, settings)
            return finish_health(health_path, result, manifest=manifest, settings=settings,
                                 now=current, notifier=notifier)
    except MaintenanceError as error:
        result.update(ok=False, status="reauthorization_required" if error.reauthorize else "error",
                      reason=error.reason, requiresReauthorization=error.reauthorize)
    except TimeoutError:
        result.update(ok=False, status="error", reason="credential_lock_timeout")
    except Exception as error:
        # No arbitrary exception string is allowed into the health record.
        reason = "credential_replica_unavailable" if type(error).__name__ == "LarkAuthStoreError" else "maintenance_unavailable"
        result.update(ok=False, status="error", reason=reason)
    # Failures leave the first lock context; reacquire for a serialized warning
    # and persistence instead of trusting an inherited lock marker.
    if (lock_entered and result["reason"] not in {"credential_replica_unavailable", "credential_lock_timeout"}
            and "manifest" in locals() and "settings" in locals()):
        try:
            with lock_factory(settings, timeout=policy["lock_timeout_seconds"]) as auth_guard:
                commit_credentials(auth_guard, settings)
                return finish_health(health_path, result, manifest=manifest, settings=settings,
                                     now=current, notifier=notifier)
        except TimeoutError:
            result.update(ok=False, status="error", reason="credential_lock_timeout")
        except Exception as error:
            reason = "credential_replica_unavailable" if type(error).__name__ == "LarkAuthStoreError" else "maintenance_unavailable"
            result.update(ok=False, status="error", reason=reason)
    if configuration_validated and result["reason"] in {"credential_replica_unavailable", "credential_lock_timeout"}:
        # A bad or busy user store must not silence its own failure notices.
        # Version probing is independent of credential storage; the notifier
        # separately requires the fixed App ID and bot-only report profile.
        result["executionContext"].pop("credentialStoreFingerprint", None)
        try:
            expected_version = manifest.get("expected_cli_version")
            if expected_version:
                result["cliVersion"] = version_reader(settings)
                if result["cliVersion"] != expected_version:
                    raise MaintenanceError("cli_version_mismatch")
        except MaintenanceError as error:
            result["notification"] = {"status": "disabled", "reason": error.reason}
            return finish_health(health_path, result, settings=settings, now=current,
                                 notifier=notifier, capture_fingerprint=False)
        return finish_health(health_path, result, manifest=manifest, settings=settings,
                             now=current, notifier=notifier, capture_fingerprint=False)
    return finish_health(health_path, result, now=current, notifier=notifier)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE / "cc-connect-lark-isolation.json")
    parser.add_argument("--health", type=Path, default=HERE.parent / "data" / "feishu-user-auth-health.json")
    parser.add_argument("--check-only", action="store_true", help="Read status without remote verification")
    parser.add_argument("--force-verify", action="store_true", help="Verify even fresh credentials; does not force token rotation")
    args = parser.parse_args(argv)
    if args.check_only and args.force_verify:
        parser.error("--check-only and --force-verify cannot be combined")
    result = maintain(args.manifest, args.health, check_only=args.check_only, force_verify=args.force_verify)
    # Deliberately print only our safe structured health contract.
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
