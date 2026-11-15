"""Standalone report authorization and publication policy; no upstream dispatch or cards."""
from __future__ import annotations

import json

import os

import re

import subprocess

import time

from datetime import datetime, timedelta, timezone

from pathlib import Path

HERE = Path(__file__).resolve().parent

WORKSPACE_ROOT = HERE.parents[3]

from request_model import InputError

from lark_auth_lock import LarkAuthLockTimeout, lark_auth_lock

AUTH_REFRESH_MARGIN_SECONDS = 15 * 60

AUTH_VERIFY_ATTEMPTS = 2

AUTH_VERIFY_RETRY_DELAY_SECONDS = 1.0

SECURE_LABEL_SCOPES = frozenset({
    "docs:secure_label:readonly",
    "docs:secure_label:write_only",
})

class UserAuthorizationError(InputError):
    """Raised when a Feishu user token must be refreshed or re-authorized."""

def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == 3:
                raise
            time.sleep(0.05 * 2**attempt)

def lark_environment(settings: dict) -> dict[str, str]:
    """Build a subprocess environment pinned to this project's lark-cli home."""
    environment = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    lark_home = str(settings.get("lark_home") or "").strip()
    if lark_home:
        resolved = str(Path(lark_home).expanduser().resolve())
        environment["USERPROFILE"] = resolved
        environment["HOME"] = resolved
    return environment

def activate_lark_environment(settings: dict) -> None:
    """Pin downstream helpers that invoke lark-cli through inherited env vars."""
    lark_home = str(settings.get("lark_home") or "").strip()
    if not lark_home:
        return
    resolved = str(Path(lark_home).expanduser().resolve())
    os.environ["USERPROFILE"] = resolved
    os.environ["HOME"] = resolved

def run_lark(settings: dict, args: list[str], *, timeout: int = 90, profile: str | None = None) -> dict:
    environment = lark_environment(settings)
    command = [settings["lark_cli"], *args]
    selected_profile = str(profile if profile is not None else settings.get("lark_profile") or "").strip()
    if not selected_profile:
        raise InputError("飞书调用必须显式指定 profile，禁止使用默认身份。")
    command.extend(["--profile", selected_profile])
    command.extend(["--format", "json"])
    try:
        with lark_auth_lock(settings, timeout=min(timeout, 60)):
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout, env=environment, cwd=str(WORKSPACE_ROOT))
    except LarkAuthLockTimeout as exc:
        raise InputError(str(exc)) from None
    raw = completed.stdout or completed.stderr
    try:
        payload = json.loads(raw)
    except ValueError:
        raise InputError("飞书命令未返回有效结果。") from None
    if not isinstance(payload, dict):
        raise InputError("飞书命令未返回有效结果。")
    if completed.returncode or payload.get("ok") is not True:
        error = payload.get("error", {})
        message = str(error.get("message") or error.get("type") or "unknown")
        if _authorization_failure(payload) == "reauthorize":
            raise UserAuthorizationError("飞书调用失败: need_user_authorization")
        raise InputError("飞书调用失败: " + message)
    return payload

def _profile_auth_status(settings: dict, profile: str, *, verify: bool = False,
                         timeout: int = 30) -> dict:
    """Return auth status for one explicit profile without changing the active profile."""
    selected_profile = str(profile or "").strip()
    if not selected_profile:
        raise InputError("飞书授权状态检查必须显式指定 profile。")
    command = [settings["lark_cli"], "auth", "status", "--profile", selected_profile]
    if verify:
        command.append("--verify")
    command.append("--json")
    environment = lark_environment(settings)
    try:
        with lark_auth_lock(settings, timeout=min(timeout, 60)):
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                env=environment, cwd=str(WORKSPACE_ROOT),
            )
    except LarkAuthLockTimeout as exc:
        raise InputError(str(exc)) from None
    raw = completed.stdout or completed.stderr
    try:
        payload = json.loads(raw)
    except ValueError:
        raise InputError("飞书授权状态未返回有效结果。") from None
    if not isinstance(payload, dict):
        raise InputError("飞书授权状态未返回有效结果。")
    failure = _authorization_failure(payload)
    if failure == "administrator":
        raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
    if completed.returncode or payload.get("ok") is False:
        if failure == "reauthorize":
            raise UserAuthorizationError("飞书用户刷新授权不可用，需要本人重新完成 OAuth 授权。")
        raise InputError("飞书授权状态校验暂未完成，请稍后重试或检查网络。")
    return payload

def auth_status(settings: dict, *, verify: bool = False, timeout: int = 30) -> dict:
    """Read one explicit profile; only ``verify=True`` may refresh its token."""
    profile = str(settings.get("lark_profile") or "").strip()
    if not profile:
        raise InputError("走查测试报告必须显式配置 lark_profile，禁止使用默认身份。")
    return _profile_auth_status(settings, profile, verify=verify, timeout=timeout)

def ensure_expected_app(settings: dict, payload: dict) -> None:
    expected = str(settings.get("expected_lark_app_id") or "").strip()
    actual = str((payload or {}).get("appId") or "").strip()
    if expected and actual != expected:
        raise InputError("飞书 profile 的 App ID 与项目隔离清单不一致。")

def ensure_report_bot_authorization(settings: dict) -> dict:
    """Require a dedicated bot-only profile for report ownership and delivery."""
    profile = str(settings.get("report_lark_profile") or "").strip()
    user_profile = str(settings.get("lark_profile") or "").strip()
    if not profile:
        raise InputError("报告发布缺少 report_lark_profile。")
    if profile == user_profile:
        raise InputError("报告必须使用独立的 bot-only report_lark_profile，不能复用走查测试用户 profile。")
    payload = _profile_auth_status(settings, profile, verify=False)
    ensure_expected_app(settings, payload)
    identities = payload.get("identities") if isinstance(payload, dict) else None
    bot = identities.get("bot") if isinstance(identities, dict) else None
    user = identities.get("user") if isinstance(identities, dict) else None
    if not isinstance(bot, dict) or bot.get("available") is not True:
        raise InputError("报告 Bot 身份不可用；请为 report_lark_profile 配置获准的 App ID/App Secret 与所需权限。")
    if isinstance(user, dict) and user.get("available") is True:
        raise InputError("report_lark_profile 必须是 bot-only；检测到可用用户身份，可能导致额外 full_access 授权。")
    return {"profile": profile, "available": True}

def _authorization_user(payload: dict) -> dict:
    identities = payload.get("identities") if isinstance(payload, dict) else None
    user = identities.get("user") if isinstance(identities, dict) else None
    return user if isinstance(user, dict) else {}

def _authorization_expiry(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

def _authorization_failure(payload: dict, now: datetime | None = None) -> str | None:
    """Classify only explicit invalid-refresh evidence, never generic missing state."""
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    fields = [error.get(key) for key in ("code", "type", "message")]
    fields.extend(payload.get(key) for key in ("code", "message"))
    user = _authorization_user(payload)
    fields.extend(user.get(key) for key in ("error", "errorCode", "errorMessage"))
    text = " ".join(str(value) for value in fields if isinstance(value, (str, int))).casefold()
    if "20140" in text or "auth method is not supported" in text:
        return "administrator"
    if "need_user_authorization" in text or "invalid_grant" in text:
        return "reauthorize"
    if any(phrase in text for phrase in (
        "refresh_token_expired", "refresh_token_invalid", "invalid refresh token",
        "refresh token expired", "refresh token has expired", "refresh token revoked",
        "authorization revoked", "authorization has been revoked",
    )):
        return "reauthorize"
    refresh_status = str(user.get("refreshTokenStatus") or user.get("refreshStatus") or "").casefold()
    if refresh_status in {"expired", "revoked", "invalid", "used"}:
        return "reauthorize"
    refresh_expiry = _authorization_expiry(user.get("refreshExpiresAt") or user.get("refreshTokenExpiresAt"))
    if refresh_expiry is not None and refresh_expiry <= (now or datetime.now(timezone.utc)):
        return "reauthorize"
    return None

def _authorization_metadata(user: dict) -> dict:
    expiry = _authorization_expiry(user.get("expiresAt"))
    refresh_expiry = _authorization_expiry(user.get("refreshExpiresAt") or user.get("refreshTokenExpiresAt"))
    return {
        "expires_at": expiry.isoformat() if expiry else None,
        "refresh_expires_at": refresh_expiry.isoformat() if refresh_expiry else None,
    }

def ensure_user_authorization(settings: dict, expected_open_id: str | None = None, *, force_verify: bool = False,
                              now: datetime | None = None) -> dict:
    """Validate the explicit dispatch profile and optionally pin its user identity.

    The authenticated user is the configured developer/dispatcher, not the
    requester from the CC Connect context.  ``expected_open_id`` is used only
    to prevent that explicit profile from changing identity between start and
    worker/retry phases.
    """
    current_time = now or datetime.now(timezone.utc)
    configured_expected = str(settings.get("expected_operator_open_id") or "").strip()
    expected = str(expected_open_id or configured_expected).strip()
    if expected_open_id and configured_expected and str(expected_open_id).strip() != configured_expected:
        raise InputError("任务固定的派发用户与项目隔离清单不一致。")
    margin = int(settings.get("auth_refresh_margin_seconds", AUTH_REFRESH_MARGIN_SECONDS))
    if margin < 0:
        raise InputError("auth_refresh_margin_seconds 不能为负数。")

    local = None
    reauthorization_required = False
    if not force_verify:
        try:
            local = auth_status(settings, verify=False)
        except UserAuthorizationError:
            reauthorization_required = True
        except (InputError, subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, InputError) and "20140" in str(exc):
                raise
            # A failed local CLI read is diagnostic uncertainty, not evidence
            # that the developer must log in again.
            pass
    if local is not None and (local.get("appId") or not settings.get("expected_lark_app_id")):
        ensure_expected_app(settings, local)
    local_user = _authorization_user(local or {})
    local_open_id = str(local_user.get("openId") or "").strip()
    if expected and local_open_id and local_open_id != expected:
        raise InputError("cc-connect-chassis 派发 profile 的授权用户已变化，拒绝切换开发者身份。")
    expiry = _authorization_expiry(local_user.get("expiresAt"))
    locally_valid = (
        not force_verify
        and local_user.get("available") is True
        and str(local_user.get("tokenStatus") or "").casefold() == "valid"
        and bool(local_open_id)
        and (not expected or local_open_id == expected)
        and expiry is not None
        and (not settings.get("expected_lark_app_id") or local.get("appId"))
        and expiry.astimezone(timezone.utc) > current_time.astimezone(timezone.utc) + timedelta(seconds=margin)
    )
    if locally_valid:
        return {"refreshed": False, "open_id": local_open_id, **_authorization_metadata(local_user)}

    local_failure = _authorization_failure(local or {}, current_time)
    if local_failure == "administrator":
        raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
    if local_failure == "reauthorize":
        reauthorization_required = True

    attempts = int(settings.get("auth_verify_attempts", AUTH_VERIFY_ATTEMPTS))
    if attempts < 1 or attempts > 3:
        raise InputError("auth_verify_attempts 必须在 1 到 3 之间。")
    user: dict = {}
    for attempt in range(attempts):
        try:
            verified = auth_status(settings, verify=True)
        except UserAuthorizationError:
            reauthorization_required = True
            verified = {}
        except (InputError, subprocess.TimeoutExpired, OSError) as exc:
            if "20140" in str(exc):
                raise
            verified = {}
        if verified.get("appId") or not settings.get("expected_lark_app_id"):
            ensure_expected_app(settings, verified)
        user = _authorization_user(verified)
        failure = _authorization_failure(verified, current_time)
        if failure == "administrator":
            raise InputError("飞书授权方式不受支持（20140），请应用管理员检查 OAuth 配置。")
        if failure == "reauthorize":
            reauthorization_required = True
        open_id = str(user.get("openId") or "").strip()
        if expected and open_id and open_id != expected:
            raise InputError("cc-connect-chassis 派发 profile 的授权用户已变化，拒绝切换开发者身份。")
        verified_expiry = _authorization_expiry(user.get("expiresAt"))
        if (
            bool(open_id)
            and (not expected or open_id == expected)
            and user.get("available") is True
            and user.get("verified") is True
            and str(user.get("tokenStatus") or "").casefold() == "valid"
            and (not settings.get("expected_lark_app_id") or verified.get("appId"))
            and verified_expiry is not None
            and verified_expiry > current_time
        ):
            return {
                "refreshed": True,
                "open_id": open_id,
                **_authorization_metadata(user),
            }
        if attempt + 1 < attempts:
            time.sleep(AUTH_VERIFY_RETRY_DELAY_SECONDS)
    if reauthorization_required:
        raise UserAuthorizationError(
            "飞书用户刷新授权不可用且连续校验失败，请本人重新完成 cc-connect-chassis OAuth 授权。"
        )
    raise InputError("飞书授权连续校验暂未完成；请稍后重试并检查 CLI 或网络，当前不能判定需要重新 OAuth。")

def ensure_secure_label_authorization(settings: dict) -> None:
    """Require only the two user scopes used to resolve and apply L3.

    Do not suggest ``--recommend`` here: enterprise policy can forbid an
    unrelated recommended scope (for example document export), which would
    make the otherwise valid incremental authorization fail.
    """
    payload = auth_status(settings, verify=False)
    user = _authorization_user(payload)
    granted = {item for item in str(user.get("scope") or "").split() if item}
    missing = sorted(SECURE_LABEL_SCOPES - granted)
    if not missing:
        return
    if settings.get("auth_credential_replica_path"):
        raise UserAuthorizationError(
            "新报告必须设置 L3，但用户 profile 缺少密级权限: " + ", ".join(missing)
            + "。请使用项目受控授权入口 deployment/feishu_project_oauth.py --initiate，"
            + "本人完成授权后执行 --complete；项目清单已包含密级最小 scope。"
        )
    profile = str(settings.get("lark_profile") or "").strip()
    raise UserAuthorizationError(
        "新报告必须设置 L3，但用户 profile 缺少密级权限: "
        + ", ".join(missing)
        + "。请仅增量授权这两个 scope（不要使用 --recommend）："
        + f" lark-cli auth login --profile {profile} --scope \""
        + " ".join(sorted(SECURE_LABEL_SCOPES))
        + "\""
    )

def grant_source_chat_view(settings: dict, report_url: str, source_chat_id: str,
                           requester_open_id: str | None = None) -> dict:
    """Resolve the source chat with the report Bot before granting view access."""
    chat_id = str(source_chat_id or "").removeprefix("chat:").strip()
    requester = str(requester_open_id or "").strip()
    profile = str(settings.get("report_lark_profile") or "").strip()
    if not re.fullmatch(r"oc_[A-Za-z0-9]+", chat_id):
        raise InputError("报告分享缺少有效的来源 chat_id。")
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", requester):
        raise InputError("报告分享缺少有效的任务请求者 open_id。")
    if not profile:
        raise InputError("报告分享必须使用固定的 report_lark_profile。")
    response = run_lark(settings, ["im", "chats", "get", "--chat-id", chat_id, "--as", "bot"],
                        profile=profile)
    data = response.get("data") if isinstance(response, dict) else None
    if (not isinstance(response, dict) or response.get("ok") is not True
            or response.get("identity") != "bot" or not isinstance(data, dict)):
        raise InputError("无法由报告 Bot 确认来源会话类型，未授权报告访问。")
    mode = data.get("chat_mode")
    if mode in ("group", "topic"):
        member_type, member_id = "openchat", chat_id
    elif mode == "p2p":
        member_type, member_id = "openid", requester
    else:
        raise InputError("来源会话类型未知，未授权报告访问。")
    run_lark(settings, ["drive", "+member-add", "--as", "bot", "--token", report_url,
                        "--member-id", member_id, "--member-type", member_type, "--perm", "view", "--yes"],
             profile=profile)
    return {"chat_mode": mode, "member_type": member_type, "member_id": member_id, "perm": "view"}

def _resource_identity(report_url: str) -> tuple[str, str]:
    """Resolve the two output resource kinds without treating arbitrary URLs as tokens."""
    from urllib.parse import urlsplit
    parsed = urlsplit(str(report_url))
    matched = re.fullmatch(r"/(docx|sheets)/([A-Za-z0-9_-]+)", parsed.path)
    if (parsed.scheme != "https" or not parsed.hostname or
            not parsed.hostname.endswith(".feishu.cn") or not matched or
            parsed.username or parsed.password or parsed.port not in (None,443)):
        raise InputError("报告治理缺少有效的飞书 Docx/Sheets 链接。")
    return matched.group(2), "docx" if matched.group(1) == "docx" else "sheet"


def restrict_collaborator_management(settings: dict, report_url: str) -> None:
    """Allow only full-access members to add or manage collaborators."""
    token, resource_type = _resource_identity(report_url)
    run_lark(
        settings,
        [
            "drive", "permission.public", "patch",
            "--token", token, "--type", resource_type,
            "--data", json.dumps({"share_entity": "only_full_access"}, ensure_ascii=False),
            "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )

def transfer_report_owner(settings: dict, report_url: str, requester_open_id: str) -> None:
    """Transfer ownership to the requester while the report Bot keeps full access."""
    open_id = str(requester_open_id or "").strip()
    if not open_id.startswith("ou_"):
        raise InputError("报告所有权转移缺少有效的请求者 open_id。")
    token, resource_type = _resource_identity(report_url)
    run_lark(
        settings,
        [
            "drive", "permission.members", "transfer_owner",
            "--token", token, "--type", resource_type,
            "--old-owner-perm", "full_access", "--stay-put",
            "--data", json.dumps({"member_id": open_id, "member_type": "openid"}, ensure_ascii=False),
            "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )

def grant_secure_label_operator(settings: dict, report_url: str, operator_open_id: str) -> None:
    """Temporarily give the pinned user identity enough access to set L3."""
    open_id = str(operator_open_id or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", open_id):
        raise InputError("报告密级操作缺少有效的固定开发者 open_id。")
    run_lark(
        settings,
        [
            "drive", "+member-add", "--as", "bot", "--token", report_url,
            "--member-id", open_id, "--member-type", "openid",
            "--perm", "full_access", "--yes",
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )

def revoke_secure_label_operator(settings: dict, report_url: str, operator_open_id: str) -> None:
    """Remove the pinned user's temporary direct permission after L3 is set."""
    open_id = str(operator_open_id or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", open_id):
        raise InputError("报告密级操作缺少有效的固定开发者 open_id。")
    token, resource_type = _resource_identity(report_url)
    run_lark(
        settings,
        [
            "api", "DELETE",
            f"/open-apis/drive/v1/permissions/{token}/members/{open_id}",
            "--params", json.dumps({"type": resource_type, "member_type": "openid"}),
        ],
        profile=str(settings["report_lark_profile"]).strip(),
    )

def _secure_label_candidates(value) -> list[dict]:
    candidates: list[dict] = []
    if isinstance(value, dict):
        label_id = value.get("id") or value.get("label_id") or value.get("labelId")
        name = value.get("name") or value.get("label_name") or value.get("display_name")
        if label_id is not None and isinstance(name, str) and name.strip():
            candidates.append({"id": str(label_id), "name": name.strip()})
        for child in value.values():
            candidates.extend(_secure_label_candidates(child))
    elif isinstance(value, list):
        for child in value:
            candidates.extend(_secure_label_candidates(child))
    return candidates

def _secure_label_name_matches(name: str, expected: str) -> bool:
    actual = str(name or "").strip()
    configured = str(expected or "").strip()
    if actual.casefold() == configured.casefold():
        return True
    if not re.fullmatch(r"L\d+", configured, re.I):
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(configured)}(?![A-Za-z0-9])", actual, re.I) is not None

def resolve_report_secure_label(settings: dict) -> dict:
    """Resolve the tenant-specific ID for the configured report label."""
    expected_name = str(settings.get("report_secure_label_name") or "L3").strip()
    if not expected_name:
        raise InputError("报告密级名称不能为空。")
    response = run_lark(
        settings,
        ["drive", "+secure-label-list", "--as", "user", "--page-size", "10", "--lang", "zh"],
        profile=str(settings["lark_profile"]).strip(),
    )
    candidates = _secure_label_candidates(response.get("data") or response)
    matches = [item for item in candidates if _secure_label_name_matches(item["name"], expected_name)]
    unique = {(item["id"], item["name"]) for item in matches}
    if len(unique) != 1:
        raise InputError(f"企业密级中无法唯一确定 {expected_name}，请检查密级配置。")
    label_id, name = next(iter(unique))
    return {"id": label_id, "name": name}

def apply_report_secure_label(settings: dict, report_url: str, label: dict) -> None:
    label_id = str((label or {}).get("id") or "").strip()
    label_name = str((label or {}).get("name") or "").strip()
    if not label_id or not _secure_label_name_matches(
        label_name, str(settings.get("report_secure_label_name") or "L3")
    ):
        raise InputError("报告密级信息无效。")
    run_lark(
        settings,
        ["drive", "+secure-label-update", "--as", "user", "--token", report_url, "--label-id", label_id],
        profile=str(settings["lark_profile"]).strip(),
    )

def report_requires_secure_label(record: dict) -> bool:
    """Enforce L3 only for records created after the policy was introduced."""
    return record.get("report_secure_label_required") is True

def configure_report_secure_label(
    settings: dict,
    record_path: Path,
    record: dict,
    report_url: str,
    permissions: dict,
    *,
    persist_callback=None,
) -> None:
    """Set L3 with least-privilege temporary access when owner and operator differ."""
    if not report_requires_secure_label(record):
        return

    operator_open_id = str(record.get("report_operator_open_id") or "").strip()
    owner_open_id = str(record.get("sender_id") or "").strip()
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", operator_open_id):
        raise InputError("任务记录缺少有效的固定开发者 open_id，不能设置报告密级。")
    if not re.fullmatch(r"ou_[A-Za-z0-9]+", owner_open_id):
        raise InputError("任务记录缺少有效的报告所有者 open_id，不能设置报告密级。")

    temporary_access = operator_open_id != owner_open_id
    access = dict(permissions.get("secure_label_operator") or {})
    recorded_operator = str(access.get("open_id") or "").strip()
    if recorded_operator and recorded_operator != operator_open_id:
        raise InputError("报告密级临时权限身份与任务固定报告操作身份不一致。")

    def persist() -> None:
        if persist_callback is not None:
            persist_callback()
        else:
            record["report_permissions"] = permissions
            atomic_json(record_path, record)

    if permissions.get("secure_label"):
        if temporary_access and access.get("active") is True:
            revoke_secure_label_operator(settings, report_url, operator_open_id)
            access.update({"active": False, "revoked_at": datetime.now(timezone.utc).isoformat()})
            permissions["secure_label_operator"] = access
            persist()
        return

    if temporary_access and access.get("active") is not True:
        grant_secure_label_operator(settings, report_url, operator_open_id)
        access = {
            "open_id": operator_open_id,
            "perm": "full_access",
            "temporary": True,
            "active": True,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        permissions["secure_label_operator"] = access
        persist()

    try:
        label = record.get("report_secure_label") or resolve_report_secure_label(settings)
        try:
            apply_report_secure_label(settings, report_url, label)
        except InputError as exc:
            if not temporary_access or "lacks permission" not in str(exc).casefold():
                raise
            time.sleep(1.0)
            apply_report_secure_label(settings, report_url, label)
        permissions["secure_label"] = {
            "id": label["id"],
            "name": label["name"],
            "configured_at": datetime.now(timezone.utc).isoformat(),
        }
        record["report_secure_label"] = label
        persist()
    finally:
        if temporary_access and access.get("active") is True:
            revoke_secure_label_operator(settings, report_url, operator_open_id)
            access.update({"active": False, "revoked_at": datetime.now(timezone.utc).isoformat()})
            permissions["secure_label_operator"] = access
            persist()
