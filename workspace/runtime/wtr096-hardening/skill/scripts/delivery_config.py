"""Output contract for new walkthrough jobs; legacy snapshots retain their format."""
from __future__ import annotations
from pathlib import Path
import re
from request_model import InputError

DELIVERY_FORMAT = "docx_with_native_sheet_details"


def validate_delivery_config(settings):
    raw = settings.get("report_delivery", {})
    if not isinstance(raw, dict):
        raise InputError("report_delivery 必须是对象。")
    result = {"format": "native_sheets", "include_images": False, **raw}
    if result["format"] != "native_sheets" or result["include_images"] is not False:
        raise InputError("本版新任务只交付主报告与两份无图原生飞书明细。")
    token = result.get("native_template_spreadsheet_token")
    if token is not None and (not isinstance(token, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]+", token)):
        raise InputError("原生明细模板 token 无效。")
    reader = result.get("native_template_reference_reader", "bot")
    if reader not in ("bot", "fixed_user"):
        raise InputError("原生模板 reference reader 必须明确为 bot 或 fixed_user。")
    if reader == "bot" and result.get("native_template_bridge_manifest") not in (None, ""):
        raise InputError("bot 模式不能配置非空 bridge manifest；请显式选择 fixed_user。")
    if reader == "fixed_user":
        manifest = result.get("native_template_bridge_manifest")
        if not token or not isinstance(manifest, str) or not manifest.strip() or not Path(manifest).is_absolute():
            raise InputError("fixed_user 模式必须指定中继模板和本机绝对路径的 ready bridge manifest。")
    return result
