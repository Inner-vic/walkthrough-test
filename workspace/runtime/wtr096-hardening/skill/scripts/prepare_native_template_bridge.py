"""Plan a template relay locally. Cloud execution is deliberately unsupported.

No copy, grant, label, scope or ready-manifest action is implemented. A future
authorized process must carry out and independently verify the listed steps;
this plan is never accepted as a ready bridge by the publisher.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from request_model import InputError
from template_reference_access import canonical_sha, manifest_path, settings_binding, _token, _read

PLAN_SCHEMA = 'walkthrough-template-bridge-plan-v1'
UNSUPPORTED_CHECKS = (
    'authorized_user_copy_and_complete_official_receipt',
    'new_relay_only_private_scope_fresh_readback',
    'new_relay_only_report_bot_full_access',
    'source_and_relay_labels_preserved_fresh_readback',
    'complete_template_layout_style_cells_equivalence',
    'actual_same_app_report_bot_read_and_copy_permissions',
)


def prepare(settings, record_path, source_token, source_sheet_id, relay_name, *,
            execute=False, execute_copy=False, grant_report_bot=False):
    if execute or execute_copy or grant_report_bot:
        raise InputError('本助手仅本地plan；不支持云端copy/grant/治理或生成ready。需另获明确授权并使用经审查的流程。')
    if (not _token(source_token) or not _token(source_sheet_id)
            or not isinstance(relay_name, str) or not relay_name.strip()
            or any(c in relay_name for c in ('\x00','\n','\r'))):
        raise InputError('本地模板桥接计划缺少明确源reference或新中继名称。')
    path = manifest_path(settings, record_path, existing=False)
    binding = settings_binding(settings)
    content = {'schema': PLAN_SCHEMA, 'state': 'planned', 'binding': binding,
        'source': {'token': source_token, 'sheet_id': source_sheet_id},
        'relay_request': {'name': relay_name}, 'cloud_executed': False,
        'ready_supported': False, 'unsupported_checks': list(UNSUPPORTED_CHECKS),
        'required_future_order': [
            'verify_fixed_user_and_same_app_report_bot_without_identity_fallback',
            'read_source_and_confirm_copy_auth_result_is_true',
            'persist_copy_pending_before_one_authorized_copy',
            'unknown_copy_result_requires_manual_reconciliation_never_repeat',
            'accept_only_new_token_from_official_user_copy_response_never_source_token',
            'govern_only_that_new_relay_and_grant_report_bot_only_after_authorization',
            'fresh_verify_private_scope_labels_bot_read_copy_and_full_template_equivalence',
            'only_independent_verified_evidence_can_create_ready_manifest']}
    content['plan_sha256'] = canonical_sha(content)
    if path.exists():
        existing, _ = _read(settings, path)
        if not isinstance(existing, dict):
            raise InputError('既存记录不是本助手计划对象；不覆盖。')
        if existing.get('state') in ('copy_pending','pending','unknown'):
            raise InputError('复制可能已发生；本助手不重试、不覆盖checkpoint，须人工核对原回执。')
        if existing != content:
            raise InputError('既存记录不等于本次计划；不覆盖其他schema、ready或身份/来源绑定。')
        return {'status': 'planned', 'idempotent': True, 'cloud_executed': False,
                'ready_manifest_created': False, 'plan': existing}
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path(settings, path, existing=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return {'status': 'planned', 'idempotent': False, 'cloud_executed': False,
            'ready_manifest_created': False, 'plan': content}


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise InputError('模板桥接计划参数无效；本助手不支持云执行或ready操作。')
    parser = Parser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--source-token', required=True)
    parser.add_argument('--source-sheet-id', required=True)
    parser.add_argument('--relay-name', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--execute-copy', action='store_true')
    parser.add_argument('--grant-report-bot', action='store_true')
    try:
        args = parser.parse_args(argv)
        if args.execute or args.execute_copy or args.grant_report_bot:
            raise InputError('本助手仅本地plan；所有云端copy、授权和ready操作均未实现且不会执行。')
        settings = json.loads(args.settings.read_text('utf-8-sig'))
        result = prepare(settings, args.record, args.source_token, args.source_sheet_id, args.relay_name)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (InputError, OSError, ValueError):
        print(json.dumps({'status': 'not_executed', 'cloud_executed': False,
            'ready_manifest_created': False,
            'error': '计划未完成或请求了不支持的云操作；保留原记录。此助手只支持本地plan，不能生成ready。'}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
