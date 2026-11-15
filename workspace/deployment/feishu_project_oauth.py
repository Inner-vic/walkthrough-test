"""Project-pinned split-flow OAuth using the shared credential replica lock.

Initiate prints a fresh verification URL but never the device code. Complete
is called only after the user confirms authorization; it validates the fixed
dispatcher and saves canonical credentials before removing the active flow.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from feishu_auth_maintenance import MaintenanceError, lark_environment, load_configuration, load_worker
from lark_auth_lock import LarkAuthLockTimeout, lark_auth_lock
from lark_auth_store import LarkAuthStoreError

DEFAULT_MANIFEST = Path(__file__).resolve().with_name("cc-connect-lark-isolation.json")
PENDING_FILENAME = "cc-connect-project-oauth-active.json"
MAX_FLOW_TTL_SECONDS = 600
MAX_PENDING_BYTES = 32 * 1024


class ProjectOAuthError(Exception):
    """Contains a fixed safe classification, never CLI output or credentials."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc(now=None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ProjectOAuthError("invalid_time")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value) -> datetime:
    if not isinstance(value, str):
        raise ProjectOAuthError("active_flow_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise ProjectOAuthError("active_flow_invalid") from None
    if parsed.tzinfo is None:
        raise ProjectOAuthError("active_flow_invalid")
    return parsed.astimezone(timezone.utc)


def _pending_path(manifest: dict, settings: dict) -> Path:
    private = (Path(manifest["lark_home"]) / ".lark-cli").resolve()
    replica = str(settings.get("auth_credential_replica_path") or "").strip()
    if not replica or manifest.get("auth_credential_replica_path") != replica:
        raise ProjectOAuthError("credential_replica_not_configured")
    path = private / PENDING_FILENAME
    if not path.resolve().is_relative_to(private) or path.resolve() == Path(replica).resolve():
        raise ProjectOAuthError("active_flow_path_invalid")
    return path


def _binding(manifest: dict) -> dict:
    return {"project_id": manifest["project_id"], "profile": manifest["profiles"]["dispatch_user"],
            "app_id": manifest["expected_lark_app_id"], "open_id": manifest["expected_dispatch_open_id"],
            "scopes": sorted(set(manifest["required_user_scopes"]))}


def _write_private(path: Path, pending: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(pending, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ProjectOAuthError("active_flow_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_pending(path: Path, manifest: dict, current: datetime) -> dict:
    try:
        if path.stat().st_size > MAX_PENDING_BYTES:
            raise ProjectOAuthError("active_flow_invalid")
        pending = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectOAuthError("active_flow_missing") from None
    except (OSError, ValueError):
        raise ProjectOAuthError("active_flow_invalid") from None
    if not isinstance(pending, dict) or pending.get("schema_version") != 1:
        raise ProjectOAuthError("active_flow_invalid")
    if any(pending.get(key) != value for key, value in _binding(manifest).items()):
        raise ProjectOAuthError("active_flow_identity_mismatch")
    code = pending.get("device_code")
    if not isinstance(code, str) or not code.strip() or len(code) > 8192 or "\0" in code:
        raise ProjectOAuthError("active_flow_invalid")
    created = _date(pending.get("created_at"))
    expiry = _date(pending.get("expires_at"))
    if created > current or expiry <= created or expiry - created > timedelta(seconds=MAX_FLOW_TTL_SECONDS):
        raise ProjectOAuthError("active_flow_invalid")
    if current >= expiry:
        raise ProjectOAuthError("active_flow_expired")
    if not isinstance(pending.get("flow_id"), str) or len(pending["flow_id"]) != 32:
        raise ProjectOAuthError("active_flow_invalid")
    return pending


def _native(settings: dict, args: list[str], runner, *, workspace: Path, timeout: int = 60) -> dict:
    command = [settings["lark_cli"], "auth", "login", *args, "--profile", settings["lark_profile"], "--json"]
    try:
        completed = runner(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                           env=lark_environment(settings), cwd=str(workspace))
        if completed.returncode != 0:
            raise ProjectOAuthError("oauth_command_failed")
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError):
        raise ProjectOAuthError("oauth_command_unavailable") from None
    except (ValueError, TypeError):
        raise ProjectOAuthError("oauth_response_invalid") from None
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise ProjectOAuthError("oauth_command_failed")
    return payload["data"] if isinstance(payload.get("data"), dict) else payload


def _fresh_flow(payload: dict, manifest: dict, current: datetime) -> tuple[dict, str, int]:
    url = payload.get("verification_url") or payload.get("verification_uri_complete")
    code = payload.get("device_code")
    seconds = payload.get("expires_in")
    if (not isinstance(url, str) or not url or len(url) > 10240 or any(character in url for character in "\r\n\0")
            or not isinstance(code, str) or not code.strip() or len(code) > 8192 or "\0" in code
            or isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0):
        raise ProjectOAuthError("oauth_response_invalid")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"accounts.feishu.cn", "accounts.larksuite.com"} or parsed.username or parsed.password:
        raise ProjectOAuthError("oauth_response_invalid")
    ttl = min(seconds, MAX_FLOW_TTL_SECONDS)
    pending = {"schema_version": 1, **_binding(manifest), "flow_id": uuid.uuid4().hex,
               "created_at": _stamp(current), "expires_at": _stamp(current + timedelta(seconds=ttl)),
               "device_code": code}
    return pending, url, ttl


def _confirm_user(worker, settings: dict, manifest: dict, current: datetime) -> None:
    expected = manifest["expected_dispatch_open_id"]
    try:
        identity = worker.ensure_user_authorization(settings, expected, force_verify=True, now=current)
        if not isinstance(identity, dict) or identity.get("open_id") != expected:
            raise ProjectOAuthError("authorized_identity_mismatch")
        status = worker.auth_status(settings, verify=False)
        identities = status.get("identities") if isinstance(status, dict) else None
        user = identities.get("user") if isinstance(identities, dict) else None
        if (not isinstance(user, dict) or status.get("appId") != manifest["expected_lark_app_id"]
                or user.get("openId") != expected or user.get("available") is not True):
            raise ProjectOAuthError("authorized_identity_mismatch")
        scopes = set(str(user.get("scope") or "").split())
        if not set(manifest["required_user_scopes"]).issubset(scopes):
            raise ProjectOAuthError("authorized_scopes_missing")
    except ProjectOAuthError:
        raise
    except Exception:
        raise ProjectOAuthError("authorization_verification_failed") from None


def project_oauth(mode: str, manifest_path: Path = DEFAULT_MANIFEST, *, now=None, runner=None,
                  lock_factory=lark_auth_lock, worker_loader=load_worker,
                  configuration_loader=load_configuration) -> dict:
    """Run exactly one split-flow phase; never dispatch work or send messages."""
    try:
        if mode not in {"initiate", "complete"}:
            raise ProjectOAuthError("invalid_oauth_phase")
        current = _utc(now)
        manifest, settings = configuration_loader(Path(manifest_path))
        path = _pending_path(manifest, settings)
        invoke = runner or subprocess.run
        timeout = 60
        policy = manifest.get("auth_maintenance")
        if isinstance(policy, dict) and isinstance(policy.get("lock_timeout_seconds"), int):
            timeout = policy["lock_timeout_seconds"]
        with lock_factory(settings, timeout=timeout):
            if mode == "initiate":
                # A newly requested authorization must never fall back to an older flow.
                path.unlink(missing_ok=True)
                payload = _native(settings, ["--scope", " ".join(manifest["required_user_scopes"]), "--no-wait"], invoke,
                                  workspace=Path(manifest["workspace_root"]))
                pending, url, ttl = _fresh_flow(payload, manifest, current)
                _write_private(path, pending)
                result = {"ok": True, "status": "authorization_required", "project_id": manifest["project_id"],
                          "profile": manifest["profiles"]["dispatch_user"], "verification_url": url,
                          "expires_in": ttl, "expires_at": pending["expires_at"]}
            else:
                pending = _read_pending(path, manifest, current)
                remaining = max(1, int((_date(pending["expires_at"]) - current).total_seconds()))
                _native(settings, ["--device-code", pending["device_code"]], invoke,
                        workspace=Path(manifest["workspace_root"]), timeout=min(60, remaining))
                _confirm_user(worker_loader(Path(manifest["workspace_root"])), settings, manifest, current)
                result = {"ok": True, "status": "authorized", "project_id": manifest["project_id"],
                          "profile": manifest["profiles"]["dispatch_user"], "open_id": manifest["expected_dispatch_open_id"]}
        # Exiting the credential lock exports the refreshed DPAPI record first.
        # Take the lock again so a concurrent initiation cannot lose its new flow.
        if mode == "complete":
            with lock_factory(settings, timeout=timeout):
                active = _read_pending(path, manifest, current)
                if active.get("flow_id") == pending["flow_id"]:
                    path.unlink()
        return result
    except ProjectOAuthError as error:
        return {"ok": False, "status": "error", "reason": error.reason}
    except LarkAuthStoreError:
        return {"ok": False, "status": "error", "reason": "credential_replica_unavailable"}
    except LarkAuthLockTimeout:
        return {"ok": False, "status": "error", "reason": "credential_lock_timeout"}
    except MaintenanceError:
        return {"ok": False, "status": "error", "reason": "project_configuration_invalid"}
    except Exception:
        return {"ok": False, "status": "error", "reason": "oauth_unavailable"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Project-pinned Feishu OAuth split flow")
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--initiate", action="store_true", help="Create a fresh authorization link; user must authorize before complete")
    phase.add_argument("--complete", action="store_true", help="Complete the active flow after user confirmation")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    arguments = parser.parse_args(argv)
    result = project_oauth("initiate" if arguments.initiate else "complete", arguments.manifest)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
