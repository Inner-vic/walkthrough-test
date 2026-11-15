"""Read-only host and selected analysis runtime checks; no network or publication."""
import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys
import hashlib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
import report_policy as policy
from product_version import validate_config as validate_product_config
from walkthrough_worker import load_settings
from native_log_source import wsl_path
from native_preflight import check_native_environment, NativePreflightError
import cloud_log_client as cloud
from delivery_config import validate_delivery_config
from preflight_diagnostics import PreflightError, preflight_failure_fields


@contextmanager
def _stage(stage):
    try:
        yield
    except PreflightError:
        raise
    except Exception:
        raise PreflightError(stage, 'check_failed') from None


def check_delivery_configuration(settings):
    cfg = validate_delivery_config(settings)
    template = Path(__file__).resolve().parents[1]/'assets/report-table.xml'
    table = ET.fromstring(template.read_text(encoding='utf-8'))
    rows = table.findall('.//tr')
    if len(rows)!=6 or any(len(row)!=13 for row in rows[-4:]):
        raise ValueError('Invalid packaged walkthrough report template')
    return {'format':cfg['format'],'include_images':False,
            'template_reference_reader':cfg.get('native_template_reference_reader','bot'),
            'template_bridge_online_verified':False,
            'main_template_sha256':hashlib.sha256(template.read_bytes()).hexdigest(),
            'detail_template_source':'configured_compatible_override' if cfg.get('native_template_spreadsheet_token')
                                     else 'approved_external_native_analyzer',
            'online_permissions_verified':False,'network_verified':False}

def check_host_dependencies():
    if sys.version_info[:2] != (3,11):
        raise PreflightError('host_deps', 'host_runtime_unsupported')
    versions={}
    for name,expected in (('numpy','1.24.4'),('pandas','2.0.3'),('zstandard','0.25.0'),('openpyxl','3.1.5')):
        try:
            version=importlib.import_module(name).__version__
        except Exception:
            raise PreflightError('host_deps', 'host_dependency_unavailable') from None
        if version!=expected:raise PreflightError('host_deps', 'host_dependency_version_mismatch')
        versions[name]=version
    return {'python':sys.version.split()[0],**versions}

def check_cloud_configuration(policy):
    """Inspect local endpoint/allowlist configuration; never probe either cloud."""
    if policy not in ('prod_then_test', 'test_then_prod', 'test', 'prod'):
        raise ValueError('Unsupported cloud environment policy')
    required = 'prod' if policy in ('prod', 'prod_then_test') else 'test'
    result = {}
    for environment in ('test', 'prod'):
        try:
            cloud.environment_config(environment)
        except (cloud.CloudConfigError, ValueError):
            if environment == required:
                raise
            configured = False
        else:
            configured = True
        result[environment] = {
            'provider_environment': 'testtwo' if environment == 'test' else 'prod',
            'configuration_status': 'configured' if configured else 'missing_or_invalid',
            'required_at_start': environment == required,
            'network_verified': False,
        }
    return result


def check_product_configuration(settings):
    """Validate the explicit query target and the locally pinned User identity.

    Local auth status must not call verify/login, request scopes, send a message,
    or substitute the source chat for the configured query group.
    """
    try:
        cfg = validate_product_config(settings)
    except Exception:
        raise PreflightError('product_identity', 'product_configuration_invalid') from None
    expected = settings.get('expected_operator_open_id')
    expected_app = settings.get('expected_lark_app_id')
    if (type(expected) is not str or not expected.strip()
            or type(expected_app) is not str or not expected_app.strip()):
        raise PreflightError('product_identity', 'fixed_identity_not_configured')
    expected = expected.strip()
    try:
        payload = policy.auth_status(settings, verify=False)
    except Exception:
        raise PreflightError('product_identity', 'auth_status_unavailable') from None
    if not isinstance(payload, dict):
        raise PreflightError('product_identity', 'auth_status_invalid')
    app = payload.get('appId')
    if type(app) is not str or not app.strip():
        raise PreflightError('product_identity', 'project_app_missing')
    if app.strip() != expected_app.strip():
        raise PreflightError('product_identity', 'project_app_mismatch')
    try:
        policy.ensure_expected_app(settings, payload)
    except Exception:
        raise PreflightError('product_identity', 'project_app_mismatch') from None
    identities = payload.get('identities')
    user = identities.get('user') if isinstance(identities, dict) else None
    if not isinstance(user, dict) or type(user.get('openId')) is not str or not user['openId']:
        raise PreflightError('product_identity', 'user_identity_unavailable')
    if user.get('openId') != expected:
        raise PreflightError('product_identity', 'user_identity_mismatch')
    if type(user.get('available')) is not bool:
        raise PreflightError('product_identity', 'auth_status_invalid')
    token_status = user.get('tokenStatus')
    token_status = token_status.casefold() if type(token_status) is str else None
    expiry = policy._authorization_expiry(user.get('expiresAt'))
    # A known App + fixed openId may still be present with available=False
    # after access expiry. Only expiry evidence requests the controlled caller
    # to maintain authorization; this read-only preflight never refreshes it.
    if token_status == 'expired' or expiry is not None and expiry <= datetime.now(timezone.utc):
        raise PreflightError('product_identity', 'user_token_refresh_required')
    if token_status != 'valid':
        raise PreflightError('product_identity', 'user_token_status_invalid')
    if expiry is None:
        raise PreflightError('product_identity', 'user_token_expiry_unavailable')
    if user.get('available') is not True:
        raise PreflightError('product_identity', 'user_identity_unavailable')
    return {
        'configuration_status':'configured', 'query_target':'fixed_group',
        'asgard_bot_id_configured':True, 'asgard_app_id_configured':bool(cfg.get('asgard_app_id')),
        'timeout_seconds':cfg['timeout_seconds'], 'poll_interval_seconds':cfg['poll_interval_seconds'],
        'identity':'user', 'fixed_operator_matched':True, 'project_app_matched':True,
        'local_token_status':'valid', 'network_verified':False,
        'query_sent':False, 'authorization_requested':False,
    }

def check(settings_path):
    with _stage('settings'):
        settings=load_settings(settings_path)
        cfg=settings['local_analysis']
        if not isinstance(cfg, dict):raise ValueError('Invalid local analysis settings')
    with _stage('host_deps'):
        host=check_host_dependencies()
    with _stage('cloud_config'):
        policy=cfg.get('environment','prod_then_test')
        environments=check_cloud_configuration(policy)
    with _stage('cloud_identity'):
        cloud.load_settings(Path(settings['cloud_settings_path']))
    with _stage('product_identity'):
        if not Path(settings['lark_cli']).is_file():
            raise PreflightError('product_identity', 'identity_reader_unavailable')
    with _stage('rules'):
        if cfg.get('rules_json'):
            rules=json.loads(Path(cfg['rules_json']).read_text(encoding='utf-8-sig'))
            if not isinstance(rules,dict) or not rules or any(not isinstance(rows,dict) or not rows for rows in rules.values()):
                raise ValueError('Invalid full rules snapshot')
    with _stage('product_identity'):
        product = check_product_configuration(settings)
    with _stage('native'):
        native=check_native_environment(cfg)
    with _stage('delivery'):
        delivery=check_delivery_configuration(settings)
    return {'status':'local_preflight_passed','host':host,'native':native,'backend':'native_full',
            'cloud_environment_policy':policy,'cloud_environments':environments,
            'product_version':product,
            'report_delivery':delivery,
            'limits':'仅检查本地配置、固定 User 身份、最新版源码路径、所选Windows/WSL/Linux依赖和主报告模板；未验证车云连通性、在线规则、明细模板或专用副本、查询群及三份飞书资产权限；未发送版本查询、发起授权、真实解析或发布报告。首选环境须配置，后备环境在需要查询时校验。'}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings',type=Path,required=True)
    try:print(json.dumps(check(parser.parse_args().settings),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed',**preflight_failure_fields(exc),'task_created':False,
                          'network_or_message_actions_performed':False},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
