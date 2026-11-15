"""Opt-in canonical replica of one native CLI Windows DPAPI credential.

Only the official CLI's encrypted REG_SZ is copied. This module never decrypts
credentials, handles plaintext tokens, or exposes ciphertext in diagnostics.
Callers hold lark_auth_lock while importing or exporting the pinned account.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None


REGISTRY_PATH = r"Software\LarkCli\keychain\lark-cli"
DPAPI_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")
MAX_REPLICA_BYTES = 256 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]+")
_PROCESS_INSTANCE = uuid.uuid4().hex


class LarkAuthStoreError(RuntimeError):
    """The pinned encrypted replica is missing, invalid, or inaccessible."""


def _configuration(settings: dict) -> tuple[Path, dict] | None:
    configured = str(settings.get("auth_credential_replica_path") or "").strip()
    if not configured:
        return None
    home_text = str(settings.get("lark_home") or "").strip()
    home = Path(home_text).expanduser()
    path = Path(configured).expanduser()
    project = str(settings.get("project_id") or "").strip()
    profile = str(settings.get("lark_profile") or "").strip()
    app = str(settings.get("expected_lark_app_id") or "").strip()
    user = str(settings.get("expected_dispatch_open_id") or "").strip()
    if (settings.get("project_isolation_required") is not True or not home_text
            or not home.is_absolute() or not path.is_absolute()
            or not all(_IDENTIFIER.fullmatch(value) for value in (project, profile, app, user))
            or not profile.startswith(project + "-") or not app.startswith("cli_") or not user.startswith("ou_")):
        raise LarkAuthStoreError("飞书加密凭据副本缺少有效的项目隔离与固定身份配置。")
    home = home.resolve()
    path = path.resolve()
    store_root = (home / ".lark-cli").resolve()
    if (not store_root.is_relative_to(home) or not path.is_relative_to(store_root)
            or path == store_root or path.suffix.casefold() != ".json"):
        raise LarkAuthStoreError("飞书加密凭据副本必须位于项目专属 .lark-cli 目录内。")
    metadata = {"projectId": project, "profile": profile, "appId": app, "openId": user}
    return path, metadata


def _cipher_digest(ciphertext: str) -> str:
    if not isinstance(ciphertext, str) or len(ciphertext) > MAX_REPLICA_BYTES:
        raise LarkAuthStoreError("飞书加密凭据副本格式无效。")
    try:
        encrypted = base64.b64decode(ciphertext, validate=True)
    except (ValueError, TypeError):
        raise LarkAuthStoreError("飞书加密凭据副本格式无效。") from None
    if len(encrypted) < 64 or not encrypted.startswith(DPAPI_HEADER):
        raise LarkAuthStoreError("飞书加密凭据副本不包含有效的 DPAPI 数据头。")
    return hashlib.sha256(encrypted).hexdigest()


def _account_value_name(settings: dict) -> str:
    account = settings["expected_lark_app_id"] + ":" + settings["expected_dispatch_open_id"]
    return base64.urlsafe_b64encode(account.encode("utf-8")).decode("ascii").rstrip("=")


def _read_registry(settings: dict) -> str | None:
    if winreg is None:
        raise LarkAuthStoreError("飞书 DPAPI 凭据副本仅支持 Windows。")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
            ciphertext, value_type = winreg.QueryValueEx(key, _account_value_name(settings))
    except FileNotFoundError:
        return None
    except OSError:
        raise LarkAuthStoreError("当前运行环境无法读取固定派发账号的加密凭据。") from None
    if value_type != winreg.REG_SZ:
        raise LarkAuthStoreError("固定派发账号的加密凭据类型无效。")
    _cipher_digest(ciphertext)
    return ciphertext


def _write_registry(settings: dict, ciphertext: str) -> None:
    if winreg is None:
        raise LarkAuthStoreError("飞书 DPAPI 凭据副本仅支持 Windows。")
    _cipher_digest(ciphertext)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _account_value_name(settings), 0, winreg.REG_SZ, ciphertext)
    except OSError:
        raise LarkAuthStoreError("当前运行环境无法同步固定派发账号的加密凭据。") from None


def _new_record(metadata: dict, ciphertext: str) -> dict:
    return {"schemaVersion": 1, **metadata, "ciphertext": ciphertext,
            "ciphertextSha256": _cipher_digest(ciphertext),
            "updatedAt": datetime.now(timezone.utc).isoformat()}


def _read_record(path: Path, metadata: dict) -> dict:
    try:
        if path.stat().st_size > MAX_REPLICA_BYTES:
            raise LarkAuthStoreError("飞书加密凭据副本格式无效。")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LarkAuthStoreError("飞书项目加密凭据副本尚未初始化，拒绝从当前环境自动覆盖。") from None
    except (OSError, ValueError):
        raise LarkAuthStoreError("飞书项目加密凭据副本无法读取。") from None
    if (not isinstance(record, dict) or record.get("schemaVersion") != 1
            or any(record.get(key) != value for key, value in metadata.items())):
        raise LarkAuthStoreError("飞书加密凭据副本的固定项目或派发身份不匹配。")
    digest = _cipher_digest(record.get("ciphertext"))
    if record.get("ciphertextSha256") != digest:
        raise LarkAuthStoreError("飞书加密凭据副本校验和不匹配。")
    return record


def _atomic_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise LarkAuthStoreError("飞书加密凭据副本无法原子保存。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def seed_replica(settings: dict) -> dict:
    """Initialize once from an account the operator has already validated."""
    configuration = _configuration(settings)
    if configuration is None:
        raise LarkAuthStoreError("未启用飞书项目加密凭据副本。")
    path, metadata = configuration
    # Seeding uses the same OS lock, but cannot import a not-yet-created replica.
    from lark_auth_lock import lark_auth_lock
    without_replica = dict(settings)
    without_replica.pop("auth_credential_replica_path", None)
    with lark_auth_lock(without_replica):
        if path.exists() or _journal_path(path).exists():
            raise LarkAuthStoreError("飞书项目加密凭据副本已存在，拒绝重新初始化。")
        ciphertext = _read_registry(settings)
        if ciphertext is None:
            raise LarkAuthStoreError("固定派发账号尚无可初始化的加密凭据。")
        record = _new_record(metadata, ciphertext)
        _atomic_record(path, record)
        return {"seeded": True, "sha256": record["ciphertextSha256"]}


def _journal_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".pending")


def _read_journal(path: Path, metadata: dict) -> dict | None:
    pending = _journal_path(path)
    if not pending.exists():
        return None
    try:
        if pending.stat().st_size > MAX_REPLICA_BYTES:
            raise LarkAuthStoreError("飞书加密凭据待提交记录格式无效。")
        journal = json.loads(pending.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LarkAuthStoreError("飞书加密凭据待提交记录无法读取。") from None
    if (not isinstance(journal, dict) or journal.get("schemaVersion") != 1
            or any(journal.get(key) != value for key, value in metadata.items())
            or journal.get("state") not in {"active", "prepared"}
            or not re.fullmatch(r"[a-f0-9]{64}", str(journal.get("baseCiphertextSha256") or ""))):
        raise LarkAuthStoreError("飞书加密凭据待提交记录的固定身份或基线无效。")
    if journal["state"] == "prepared":
        if _cipher_digest(journal.get("ciphertext")) != journal.get("ciphertextSha256"):
            raise LarkAuthStoreError("飞书加密凭据待提交记录校验和不匹配。")
    return journal


def _clear_journal(path: Path) -> None:
    try:
        _journal_path(path).unlink(missing_ok=True)
    except OSError:
        raise LarkAuthStoreError("飞书加密凭据待提交记录无法清理，请恢复后重试。") from None


def begin_replica(settings: dict, expected_hash: str | None) -> None:
    """Durably mark the baseline before any command may rotate refresh tokens."""
    configuration = _configuration(settings)
    if configuration is None:
        return
    path, metadata = configuration
    previous = _read_record(path, metadata)
    if previous["ciphertextSha256"] != expected_hash:
        raise LarkAuthStoreError("飞书加密凭据副本在锁外发生变化，拒绝覆盖。")
    if _read_journal(path, metadata) is not None:
        return
    _atomic_record(_journal_path(path), {"schemaVersion": 1, **metadata, "state": "active",
                   "baseCiphertextSha256": expected_hash, "ownerInstance": _PROCESS_INSTANCE,
                   "startedAt": datetime.now(timezone.utc).isoformat()})


def _recover_pending(settings: dict, path: Path, metadata: dict, record: dict) -> dict:
    journal = _read_journal(path, metadata)
    if journal is None:
        return record
    if journal["state"] == "active":
        if journal.get("ownerInstance") == _PROCESS_INSTANCE:
            # Only the originating live process can retry its uncommitted
            # registry update. A new context must never prefer its stale view.
            export_replica(settings, journal["baseCiphertextSha256"])
            return _read_record(path, metadata)
        raise LarkAuthStoreError("飞书凭据刷新存在未完成提交，已停止覆盖；请检查原运行环境并恢复加密凭据。")
    if record["ciphertextSha256"] == journal["baseCiphertextSha256"]:
        _atomic_record(path.with_suffix(path.suffix + ".previous"), record)
        _atomic_record(path, {key: value for key, value in journal.items()
                             if key not in {"state", "baseCiphertextSha256", "ownerInstance"}})
        record = _read_record(path, metadata)
    elif record["ciphertextSha256"] != journal["ciphertextSha256"]:
        raise LarkAuthStoreError("飞书加密凭据待提交记录与当前副本冲突，拒绝覆盖。")
    _clear_journal(path)
    return record


def import_replica(settings: dict) -> str | None:
    """Import validated canonical ciphertext into the current registry view."""
    configuration = _configuration(settings)
    if configuration is None:
        return None
    path, metadata = configuration
    record = _read_record(path, metadata)
    record = _recover_pending(settings, path, metadata, record)
    local = _read_registry(settings)
    if local is None or _cipher_digest(local) != record["ciphertextSha256"]:
        if local is not None:
            # Preserve the current view's encrypted record before migration,
            # independently of the previous canonical refresh generation.
            _atomic_record(path.with_suffix(path.suffix + ".registry.previous"), _new_record(metadata, local))
        _write_registry(settings, record["ciphertext"])
    begin_replica(settings, record["ciphertextSha256"])
    return record["ciphertextSha256"]


def export_replica(settings: dict, expected_hash: str | None) -> dict:
    """Persist a refreshed encrypted record; never replace canonical with missing data."""
    configuration = _configuration(settings)
    if configuration is None:
        return {"changed": False}
    path, metadata = configuration
    previous = _read_record(path, metadata)
    if previous["ciphertextSha256"] != expected_hash:
        raise LarkAuthStoreError("飞书加密凭据副本在锁外发生变化，拒绝覆盖。")
    ciphertext = _read_registry(settings)
    if ciphertext is None:
        _clear_journal(path)
        return {"changed": False, "registryMissing": True}
    digest = _cipher_digest(ciphertext)
    if digest == expected_hash:
        _clear_journal(path)
        return {"changed": False, "sha256": digest}
    prepared = {**_new_record(metadata, ciphertext), "state": "prepared",
                "baseCiphertextSha256": expected_hash, "ownerInstance": _PROCESS_INSTANCE}
    _atomic_record(_journal_path(path), prepared)
    _atomic_record(path.with_suffix(path.suffix + ".previous"), previous)
    _atomic_record(path, _new_record(metadata, ciphertext))
    _clear_journal(path)
    return {"changed": True, "sha256": digest}
