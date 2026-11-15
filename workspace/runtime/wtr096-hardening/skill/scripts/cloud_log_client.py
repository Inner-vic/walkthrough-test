#!/usr/bin/env python3
"""Walkthrough-owned cloud pagination and streaming client for log_fsdA_service."""
from __future__ import annotations

import json
import http.client
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from threading import local
from cloud_query_time import bind_collect_time_ms

def _configured_origin(name: str, fallback: str) -> str:
    return (os.environ.get(name) or fallback).strip().rstrip("/")


def _configured_hosts(name: str, fallback: str = "") -> set[str]:
    raw = os.environ.get(name)
    source = raw if raw is not None else fallback
    return {item.strip().lower() for item in source.split(",") if item.strip()}


PRODUCTION_ORIGIN = _configured_origin(
    "WALKTHROUGH_PROD_ORIGIN",
    os.environ.get("WALKTHROUGH_PROD_ORIGIN", "https://vehicle-log-api.prod.example.com"),
)
TEST_ORIGIN = _configured_origin(
    "WALKTHROUGH_TEST_ORIGIN",
    os.environ.get("WALKTHROUGH_TEST_ORIGIN", "https://vehicle-log-api.test.example.com"),
)
PAGINATION_PATH = "/v1-0/msg-parsing/hu-log-files/pagination"
EEA_PLATFORM = "EEA3.0"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 1.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
def _settings_path() -> Path:
    """Locate local deployment credentials without storing them in the Skill."""
    configured = os.environ.get("WALKTHROUGH_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cc-connect" / "secrets" / "walkthrough-cloud.settings.json"


SETTINGS_PATH = _settings_path()
CLOUD_ENVIRONMENTS = {
    "prod": {
        "origin": PRODUCTION_ORIGIN,
        "providerEnvironment": "prod",
        "displayName": "生产环境",
        "trustedDownloadHosts": {
            "https": _configured_hosts(
                "WALKTHROUGH_PROD_DOWNLOAD_HTTPS_HOSTS",
                os.environ.get("WALKTHROUGH_PROD_DOWNLOAD_HTTPS_HOSTS", "vehicle-log-download.prod.example.com"),
            ),
            "http": _configured_hosts(
                "WALKTHROUGH_PROD_DOWNLOAD_HTTP_HOSTS", os.environ.get("WALKTHROUGH_PROD_DOWNLOAD_HTTP_HOSTS", "vehicle-log-download.prod.example.com")
            ),
        },
    },
    "test": {
        "origin": TEST_ORIGIN,
        "providerEnvironment": "testtwo",
        "displayName": "测试环境",
        "trustedDownloadHosts": {
            "https": _configured_hosts(
                "WALKTHROUGH_TEST_DOWNLOAD_HTTPS_HOSTS",
                os.environ.get("WALKTHROUGH_TEST_DOWNLOAD_HTTPS_HOSTS", "vehicle-log-download.test.example.com"),
            ),
            "http": _configured_hosts(
                "WALKTHROUGH_TEST_DOWNLOAD_HTTP_HOSTS", os.environ.get("WALKTHROUGH_TEST_DOWNLOAD_HTTP_HOSTS", "vehicle-log-download.test.example.com")
            ),
        },
    },
}
WALKTHROUGH_LOG_CLASS = "log_fsdA_service"


class CloudQueryError(RuntimeError):
    """Base class for bounded cloud query failures."""


class CloudNoDataError(CloudQueryError):
    """Successful query but target data is empty or incomplete."""


class CloudConfigError(CloudQueryError):
    pass


class CloudAuthError(CloudQueryError):
    """Authentication failure with finite, independently reportable metadata."""

    def __init__(self, *, operation: str, environment: str, http_status: int | None = None,
                 business_code: int | None = None):
        if (operation not in {'query', 'download'} or environment not in {'test', 'prod'}
                or (http_status is not None and (type(http_status) is not int or http_status not in {401, 403}))
                or (business_code is not None and (type(business_code) is not int or business_code not in {401, 403}))
                or (http_status is None and business_code is None)):
            raise ValueError('Invalid bounded cloud authentication failure')
        self.operation = operation
        self.environment = environment
        self.http_status = http_status
        self.business_code = business_code
        super().__init__(auth_failure_message(auth_failure_diagnostic(self)))


def auth_failure_diagnostic(error: CloudAuthError) -> dict:
    """Never stringify an exception, its HTTP body, URL, headers, or cause."""
    if not isinstance(error, CloudAuthError):
        raise TypeError('Expected a bounded cloud authentication failure')
    result = {'category': 'cloud_auth_failed', 'retryable': False}
    for name, allowed in (('operation', {'query', 'download'}), ('environment', {'test', 'prod'})):
        value = getattr(error, name, None)
        if isinstance(value, str) and value in allowed:
            result[name] = value
    for name in ('http_status', 'business_code'):
        value = getattr(error, name, None)
        if type(value) is int and value in {401, 403}:
            result[name] = value
    return result


def auth_failure_message(diagnostic: dict) -> str:
    """Build a public message exclusively from finite diagnostic values."""
    operation = '下载' if diagnostic.get('operation') == 'download' else '列表查询'
    environment = {'test': '测试环境', 'prod': '生产环境'}.get(diagnostic.get('environment'), '当前环境')
    status = diagnostic.get('http_status')
    business = diagnostic.get('business_code')
    detail = (f'（HTTP {status}）' if type(status) is int and status in {401, 403} else
              f'（业务码 {business}）' if type(business) is int and business in {401, 403} else '')
    return f'车云{environment}{operation}认证失败{detail}；已停止取数，不将认证失败视为无日志。'


class CloudNetworkError(CloudQueryError):
    pass


class CloudServerError(CloudQueryError):
    pass


class CloudResponseError(CloudQueryError):
    pass


DOWNLOAD_FAILURE_REASONS = frozenset({'http_bad_request', 'http_rate_limited',
                                      'http_server_error', 'network_error'})
DOWNLOAD_UPSTREAM_CODES = frozenset({'DOWNLOAD_FILE_FAILURE'})
MAX_DOWNLOAD_ERROR_BODY = 16 * 1024


def _valid_download_failure(reason, status):
    if type(reason) is not str or reason not in DOWNLOAD_FAILURE_REASONS:
        return False
    if reason == 'network_error':
        return status is None
    if type(status) is not int:
        return False
    return ((reason == 'http_bad_request' and status == 400)
            or (reason == 'http_rate_limited' and status == 429)
            or (reason == 'http_server_error' and 500 <= status <= 599))


class CloudDownloadError(CloudQueryError):
    """A download failure with finite metadata independent of HTTP text or URLs."""
    def __init__(self, *, reason, environment, http_status=None, attempts=1, upstream_code=None):
        if (not _valid_download_failure(reason, http_status)
                or type(environment) is not str or environment not in ('test', 'prod')
                or type(attempts) is not int or not 1 <= attempts <= DOWNLOAD_ATTEMPTS
                or (upstream_code is not None and (type(upstream_code) is not str
                    or upstream_code not in DOWNLOAD_UPSTREAM_CODES or reason != 'http_bad_request'))):
            raise ValueError('Invalid bounded cloud download failure')
        self.reason = reason
        self.environment = environment
        self.http_status = http_status
        self.attempts = attempts
        self.upstream_code = upstream_code
        super().__init__(download_failure_message(download_failure_diagnostic(self)))


class _CloudDownloadNetworkError(CloudDownloadError, CloudNetworkError):
    pass


class _CloudDownloadServerError(CloudDownloadError, CloudServerError):
    pass


class _CloudDownloadResponseError(CloudDownloadError, CloudResponseError):
    pass


def download_failure_diagnostic(error):
    """Read only the fixed typed fields; never read args, body, URL or causes."""
    if not isinstance(error, CloudDownloadError):
        raise TypeError('Expected a bounded cloud download failure')
    result = {'category': 'cloud_download_failed', 'operation': 'download', 'retryable': False}
    environment = getattr(error, 'environment', None)
    if type(environment) is str and environment in ('test', 'prod'):
        result['environment'] = environment
    reason, status = getattr(error, 'reason', None), getattr(error, 'http_status', None)
    if _valid_download_failure(reason, status):
        result['reason'] = reason
        result['retryable'] = reason != 'http_bad_request'
        if status is not None:
            result['http_status'] = status
        code = getattr(error, 'upstream_code', None)
        if reason == 'http_bad_request' and type(code) is str and code in DOWNLOAD_UPSTREAM_CODES:
            result['upstream_code'] = code
    attempts = getattr(error, 'attempts', None)
    if type(attempts) is int and 1 <= attempts <= DOWNLOAD_ATTEMPTS:
        result['attempts'] = attempts
    return result


def download_failure_message(diagnostic):
    """Build public text from finite values only; do not imply absent logs."""
    source_environment = diagnostic.get('environment')
    environment = ({'test': '测试环境', 'prod': '生产环境'}[source_environment]
                   if type(source_environment) is str and source_environment in ('test', 'prod') else '当前环境')
    reason, status = diagnostic.get('reason'), diagnostic.get('http_status')
    detail = (f'（HTTP {status}）' if _valid_download_failure(reason, status) and status is not None else '')
    return f'车云{environment}日志文件下载失败{detail}；尚未完成分析。'


def _download_failure(reason, environment, *, http_status=None, attempts=1, upstream_code=None):
    error_type = (_CloudDownloadNetworkError if reason == 'network_error' else
                  _CloudDownloadServerError if reason == 'http_server_error' else _CloudDownloadResponseError)
    return error_type(reason=reason, environment=environment, http_status=http_status,
                      attempts=attempts, upstream_code=upstream_code)


def _bounded_download_upstream_code(error):
    """HTTP 400 may carry one known code; never retain its body or message."""
    try:
        declared = error.headers.get('Content-Length') if error.headers is not None else None
        if declared is not None and (not str(declared).isdigit() or int(declared) > MAX_DOWNLOAD_ERROR_BODY):
            return None
        raw = error.read(MAX_DOWNLOAD_ERROR_BODY)
        if not isinstance(raw, bytes):
            return None
        payload = json.loads(raw.decode('utf-8'))
        rows = payload.get('errors') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
            return None
        if any(isinstance(row, dict) and type(row.get('error_code')) is str
               and row['error_code'] in DOWNLOAD_UPSTREAM_CODES for row in rows):
            return 'DOWNLOAD_FILE_FAILURE'
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        pass
    return None


RESPONSE_CONTRACT_REASONS = frozenset({'data_missing', 'data_null', 'data_not_object'})


class CloudResponseContractError(CloudResponseError):
    """A successful list envelope that cannot establish whether logs exist."""

    def __init__(self, *, reason: str, operation: str, environment: str,
                 http_status: int | None = None, business_code: int | None = None):
        if (not isinstance(reason, str) or reason not in RESPONSE_CONTRACT_REASONS
                or not isinstance(operation, str) or operation != 'query'
                or not isinstance(environment, str) or environment not in {'test', 'prod'}
                or (http_status is not None and (type(http_status) is not int or not 200 <= http_status <= 299))
                or (business_code is not None and (type(business_code) is not int or business_code not in {0, 200}))):
            raise ValueError('Invalid bounded cloud response contract failure')
        self.reason = reason
        self.operation = operation
        self.environment = environment
        self.http_status = http_status
        self.business_code = business_code
        super().__init__(response_contract_message(response_contract_diagnostic(self)))


def response_contract_diagnostic(error: CloudResponseContractError) -> dict:
    """Only finite fields are retained, never the payload or exception chain."""
    if not isinstance(error, CloudResponseContractError):
        raise TypeError('Expected a bounded cloud response contract failure')
    result = {'category': 'cloud_response_contract_failed', 'retryable': False}
    for name, allowed in (('reason', RESPONSE_CONTRACT_REASONS), ('operation', {'query'}),
                          ('environment', {'test', 'prod'})):
        value = getattr(error, name, None)
        if isinstance(value, str) and value in allowed:
            result[name] = value
    http_status = getattr(error, 'http_status', None)
    if type(http_status) is int and 200 <= http_status <= 299:
        result['http_status'] = http_status
    business_code = getattr(error, 'business_code', None)
    if type(business_code) is int and business_code in {0, 200}:
        result['business_code'] = business_code
    return result


def response_contract_message(diagnostic: dict) -> str:
    """Explain an invalid list envelope without claiming it is an empty list."""
    environment = {'test': '测试环境', 'prod': '生产环境'}.get(diagnostic.get('environment'), '当前环境')
    reason = {'data_missing': '缺少 data', 'data_null': 'data 为 null',
              'data_not_object': 'data 不是对象'}.get(diagnostic.get('reason'), 'data 格式无效')
    evidence = []
    http_status = diagnostic.get('http_status')
    if type(http_status) is int and 200 <= http_status <= 299:
        evidence.append(f'HTTP {http_status}')
    business_code = diagnostic.get('business_code')
    if type(business_code) is int and business_code in {0, 200}:
        evidence.append(f'业务码 {business_code}')
    detail = '（' + '，'.join(evidence) + '）' if evidence else ''
    return f'车云{environment}列表响应{reason}{detail}；不能判断为无日志，已停止取数。'


@dataclass(frozen=True)
class QueryResult:
    items: list[dict]
    total: int
    environment: str
    provider_environment: str
    origin: str
    log_class: str
    request_host: str
    download_hosts: list[str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_settings(path: Path | None = None) -> dict:
    settings_path = (path or SETTINGS_PATH).resolve()
    if not settings_path.is_file():
        raise CloudConfigError(
            "未找到走查测试的本地凭据配置；请将示例配置保存到受保护位置，并通过 "
            "WALKTHROUGH_SETTINGS_PATH 指定其路径。配置文件不得发送到聊天中。"
        )
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudConfigError("本 Skill 的车云身份 JSON 无法读取") from exc
    if not isinstance(payload, dict):
        raise CloudConfigError("本 Skill 的车云身份 JSON 必须是 JSON 对象")
    username = str(payload.get("username") or "").strip()
    if not username:
        raise CloudConfigError("本 Skill 的车云身份 JSON 缺少非空 username")
    configured = payload.get("cloudOrigins") or {}
    if configured not in ({}, None):
        if not isinstance(configured, dict):
            raise CloudConfigError("本 Skill 的车云身份 JSON cloudOrigins 格式无效")
        for key, value in configured.items():
            if key not in CLOUD_ENVIRONMENTS:
                raise CloudConfigError(f"settings.json 包含不受支持的环境配置: {key}")
            if str(value).strip() != CLOUD_ENVIRONMENTS[key]["origin"]:
                raise CloudConfigError(f"settings.json 中 {key} 环境地址不在白名单中")
    return {
        "username": username,
        "python_path": payload.get("python_path"),
        "last_used": payload.get("last_used"),
        "cloudOrigins": {key: value["origin"] for key, value in CLOUD_ENVIRONMENTS.items()},
    }


def normalize_cloud_env(value: str) -> str:
    env = str(value or "").strip().lower()
    if env not in CLOUD_ENVIRONMENTS:
        raise CloudConfigError(f"不支持的数据环境: {value}")
    return env


def environment_config(environment: str) -> dict:
    env = normalize_cloud_env(environment)
    config = CLOUD_ENVIRONMENTS[env]
    parsed = urllib.parse.urlsplit(config["origin"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise CloudConfigError(f"{env} 查询端点必须是无内嵌凭据的 HTTPS 地址")
    if parsed.hostname.endswith(".example.com"):
        raise CloudConfigError(f"{env} 查询端点尚未通过环境变量配置")
    https_hosts = set(config["trustedDownloadHosts"]["https"])
    http_hosts = set(config["trustedDownloadHosts"]["http"])
    if not (https_hosts or http_hosts) or any(host.endswith(".example.com") for host in https_hosts | http_hosts):
        raise CloudConfigError(f"{env} 下载域名白名单尚未配置；按服务实际返回协议显式设置")
    return {
        "environment": env,
        "origin": config["origin"],
        "providerEnvironment": config["providerEnvironment"],
        "displayName": config["displayName"],
        "trustedDownloadHosts": {
            "https": https_hosts,
            "http": http_hosts,
        },
    }


def provider_environment(environment: str) -> str:
    return environment_config(environment)["providerEnvironment"]


def add_download_host_allowlist(environment: str, hostname: str, scheme: str = "https") -> None:
    env = normalize_cloud_env(environment)
    host = str(hostname or "").strip().lower()
    protocol = str(scheme or "").strip().lower()
    if protocol not in {"http", "https"} or not host:
        raise CloudConfigError("下载 host 白名单参数无效")
    CLOUD_ENVIRONMENTS[env]["trustedDownloadHosts"][protocol].add(host)


def trusted_download_hosts(environment: str, scheme: str = "https") -> set[str]:
    config = environment_config(environment)
    protocol = str(scheme or "").strip().lower()
    if protocol not in {"http", "https"}:
        raise CloudConfigError("下载协议无效")
    return set(config["trustedDownloadHosts"][protocol])


def _safe_service_error(message: object, username: str = "") -> str:
    text = str(message or "unknown error").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://[^\s<>\"']+", "[URL]", text, flags=re.IGNORECASE)
    if username:
        text = text.replace(username, "[USERNAME]")
    return text[:240]


def _read_bounded_json(response) -> dict:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise CloudResponseError("查询响应超过大小限制")
        except ValueError as exc:
            raise CloudResponseError("查询响应长度无效") from exc
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CloudResponseError("查询响应超过大小限制")
    if not raw:
        raise CloudResponseError("查询返回为空")
    return json.loads(raw.decode("utf-8"))


EMPTY_COMPAT_VERSION = 'prod-success-null-list-v1'
QUERY_AUDIT_VERSION = 'cloud-list-query-audit-v1'
QUERY_LOG_CLASSES = ('log_fsdA', 'log_fsdB', 'log_xcu_mcu', 'log_bzcu_eth', 'log_fsdA_service')
QUERY_TIME_KEYS = ('collectTimeMin', 'collectTimeMax', 'collectTimeMinMs', 'collectTimeMaxMs')
QUERY_COMPLETIONS = frozenset({'valid_data_object', 'known_prod_success_null_empty',
    'authentication_failed', 'network_failed', 'server_failed', 'response_contract_failed',
    'response_failed', 'client_failed'})
AUDIT_BUSINESS_CODES = frozenset({0, 200, 400, 401, 403, 404, 500, 502, 503})
_query_audit = local()


def _planned_query(metadata, log_class):
    if (not isinstance(metadata, dict) or not isinstance(metadata.get('vin'), str)
            or not re.fullmatch(r'[A-Z0-9]{17}', metadata['vin'])
            or log_class not in QUERY_LOG_CLASSES):
        raise ValueError('车云查询审计的请求或来源类型无效。')
    try:
        start = metadata['start'].replace('T', ' ')
        end = metadata['end'].replace('T', ' ')
        business = bind_collect_time_ms({'collectTimeMin': start, 'collectTimeMax': end})
        if log_class != 'log_fsdA_service':
            end = (datetime.strptime(end, '%Y-%m-%d %H:%M:%S') + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        return bind_collect_time_ms({'collectTimeMin': business['collectTimeMin'], 'collectTimeMax': end})
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError('车云查询审计的业务时间窗无效。') from None


def validate_empty_response_compat(evidence, metadata, *, environment, role=None):
    keys = {'version', 'classification', 'role', 'environment', 'log_class', 'page_no', 'page_size',
            'query', 'http_status', 'business_code', 'success', 'source_data_type',
            'normalized_data_type', 'source_message_class'}
    if (role not in (None, 'baseline', 'replacement') or environment != 'prod'
            or not isinstance(evidence, list) or not 1 <= len(evidence) <= len(QUERY_LOG_CLASSES)):
        raise ValueError('生产成功空返回兼容证据无效。')
    seen = set()
    for row in evidence:
        if (not isinstance(row, dict) or set(row) != keys
                or row.get('version') != EMPTY_COMPAT_VERSION
                or row.get('classification') != 'known_prod_success_null_empty'
                or row.get('role') != role or row.get('environment') != environment
                or row.get('log_class') not in QUERY_LOG_CLASSES or row['log_class'] in seen
                or type(row.get('page_no')) is not int or row['page_no'] != 1
                or type(row.get('page_size')) is not int or row['page_size'] != 100
                or type(row.get('http_status')) is not int or row['http_status'] != 200
                or type(row.get('business_code')) is not int or row['business_code'] != 200
                or row.get('success') is not True or row.get('source_data_type') != 'null'
                or row.get('normalized_data_type') != 'object'
                or row.get('source_message_class') != 'operation_success'
                or row.get('query') != _planned_query(metadata, row['log_class'])):
            raise ValueError('生产成功空返回兼容证据未绑定角色、来源、时间窗或固定成功信封。')
        # Equality alone would accept True as 1 or an integer millisecond value.
        query = row['query']
        if (not isinstance(query, dict) or set(query) != set(QUERY_TIME_KEYS)
                or any(type(query[key]) is not int for key in QUERY_TIME_KEYS[2:])
                or any(type(query[key]) is not str for key in QUERY_TIME_KEYS[:2])):
            raise ValueError('生产成功空返回兼容证据的查询字段类型无效。')
        seen.add(row['log_class'])
    return evidence


def validate_query_audit_event(event, metadata, *, environment, role=None):
    base = {'version', 'phase', 'sequence', 'role', 'environment', 'log_class', 'page_no',
            'page_size', 'query', 'classification'}
    completed = {'http_status', 'business_code', 'source_data_type', 'success', 'success_type'}
    if (not isinstance(event, dict) or event.get('phase') not in ('dispatched', 'completed')
            or set(event) != (base | completed if event.get('phase') == 'completed' else base)
            or event.get('version') != QUERY_AUDIT_VERSION or role not in (None, 'baseline', 'replacement')
            or environment not in ('test', 'prod') or event.get('role') != role
            or event.get('environment') != environment or event.get('log_class') not in QUERY_LOG_CLASSES
            or type(event.get('sequence')) is not int or not 1 <= event['sequence'] <= 64
            or type(event.get('page_no')) is not int or not 1 <= event['page_no'] <= 10000
            or type(event.get('page_size')) is not int or not 1 <= event['page_size'] <= 1000
            or event.get('query') != _planned_query(metadata, event['log_class'])):
        raise ValueError('车云请求审计事件与当前角色、环境或查询不一致。')
    query = event['query']
    if (not isinstance(query, dict) or set(query) != set(QUERY_TIME_KEYS)
            or any(type(query[key]) is not str for key in QUERY_TIME_KEYS[:2])
            or any(type(query[key]) is not int for key in QUERY_TIME_KEYS[2:])):
        raise ValueError('车云请求审计时间字段类型无效。')
    if event['phase'] == 'dispatched':
        if event['classification'] != 'dispatched':
            raise ValueError('车云请求派发审计分类无效。')
    elif (event['classification'] not in QUERY_COMPLETIONS
            or (event['http_status'] is not None and (type(event['http_status']) is not int or not 100 <= event['http_status'] <= 599))
            or (event['business_code'] is not None and (type(event['business_code']) is not int or event['business_code'] not in AUDIT_BUSINESS_CODES))
            or event['source_data_type'] not in ('object', 'null', 'missing', 'other')
            or event['success_type'] not in ('boolean', 'missing', 'other')
            or (type(event['success']) is not bool if event['success_type'] == 'boolean' else event['success'] is not None)):
        raise ValueError('车云请求完成审计包含未知分类或非有限字段。')
    return event


@contextmanager
def query_audit_context(*, role=None, request, environment, on_event=None):
    """Thread-local, bounded audit; no URLs, credentials or raw response text."""
    if role not in (None, 'baseline', 'replacement') or environment not in ('test', 'prod'):
        raise ValueError('车云查询审计角色或环境无效。')
    _planned_query(request, 'log_fsdA_service')
    if on_event is not None and not callable(on_event):
        raise ValueError('车云查询审计回调无效。')
    if getattr(_query_audit, 'state', None) is not None:
        raise ValueError('车云查询审计上下文不得嵌套。')
    state = {'role': role, 'request': deepcopy(request), 'environment': environment,
             'on_event': on_event, 'sequence': 0, 'compat': []}
    _query_audit.state = state
    try:
        yield state['compat']
    finally:
        _query_audit.state = None


def _audit_begin(params, environment):
    state = getattr(_query_audit, 'state', None)
    if state is None:
        return None
    if params.get('vin') != state['request']['vin'] or environment != state['environment']:
        raise CloudConfigError('车云请求未绑定当前审计的车辆或环境。')
    state['sequence'] += 1
    event = {'version': QUERY_AUDIT_VERSION, 'phase': 'dispatched', 'sequence': state['sequence'],
             'role': state['role'], 'environment': environment, 'log_class': params.get('logClass'),
             'page_no': params.get('pageNo'), 'page_size': params.get('pageSize'),
             'query': {key: params.get(key) for key in QUERY_TIME_KEYS}, 'classification': 'dispatched'}
    try:
        validate_query_audit_event(event, state['request'], environment=environment, role=state['role'])
    except ValueError as error:
        raise CloudConfigError(str(error)) from None
    if state['on_event']:
        state['on_event'](deepcopy(event))
    return state, event


def _audit_complete(ticket, payload, http_status, classification):
    if ticket is None:
        return
    state, event = ticket
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get('code')
    known_code = (int(code) if type(code) in (int, str) and code in tuple(AUDIT_BUSINESS_CODES) + tuple(str(v) for v in AUDIT_BUSINESS_CODES) else None)
    event = {**event, 'phase': 'completed', 'classification': classification,
             'http_status': http_status if type(http_status) is int and 100 <= http_status <= 599 else None,
             'business_code': known_code,
             'source_data_type': ('missing' if 'data' not in payload else 'null' if payload['data'] is None else
                                  'object' if isinstance(payload['data'], dict) else 'other'),
             'success': payload.get('success') if type(payload.get('success')) is bool else None,
             'success_type': ('missing' if 'success' not in payload else 'boolean' if type(payload['success']) is bool else 'other')}
    validate_query_audit_event(event, state['request'], environment=state['environment'], role=state['role'])
    if state['on_event']:
        state['on_event'](deepcopy(event))


class _ValidatedListPayload(dict):
    """Provenance lives in attributes created after validation, never JSON markers."""
    def __init__(self, payload, *, source_data_type, compatibility_evidence=None):
        super().__init__(payload)
        self.source_data_type = source_data_type
        self.compatibility_evidence = deepcopy(compatibility_evidence)


def _known_prod_null(payload, params, username, environment, http_status):
    allowed_params = {'vin', 'logClass', *QUERY_TIME_KEYS, 'remoteMode', 'domain', 'pageNo', 'pageSize',
                      'userName', 'eeaPlatform', 'logLevel', 'categoryCode', 'categoryId', 'keywordCode'}
    return (environment == 'prod' and type(http_status) is int and http_status == 200
        and set(payload) <= {'success', 'code', 'message', 'data', 'timestamp'}
        and payload.get('success') is True and type(payload.get('code')) is int and payload['code'] == 200
        and payload.get('message') == '操作成功！' and 'data' in payload and payload['data'] is None
        and ('timestamp' not in payload or type(payload['timestamp']) is int and payload['timestamp'] >= 0)
        and set(params) <= allowed_params and type(params.get('pageNo')) is int and params['pageNo'] == 1
        and type(params.get('pageSize')) is int and params['pageSize'] == 100
        and isinstance(params.get('vin'), str) and re.fullmatch(r'[A-Z0-9]{17}', params['vin']) is not None
        and params.get('logClass') in QUERY_LOG_CLASSES and params.get('remoteMode') == 'false'
        and params.get('eeaPlatform') == EEA_PLATFORM and type(username) is str and bool(username)
        and params.get('userName') == username and all(key in params for key in QUERY_TIME_KEYS)
        and all(params.get(key, '') == '' for key in ('domain', 'logLevel', 'categoryCode', 'categoryId', 'keywordCode')))


def _compatibility_evidence(params, ticket):
    return {'version': EMPTY_COMPAT_VERSION, 'classification': 'known_prod_success_null_empty',
            'role': ticket[0]['role'] if ticket else None, 'environment': 'prod',
            'log_class': params['logClass'], 'page_no': 1, 'page_size': 100,
            'query': {key: params[key] for key in QUERY_TIME_KEYS}, 'http_status': 200,
            'business_code': 200, 'success': True, 'source_data_type': 'null',
            'normalized_data_type': 'object', 'source_message_class': 'operation_success'}


def _query_url(origin: str, params: dict) -> str:
    return origin + PAGINATION_PATH + "?" + urllib.parse.urlencode(params)


def request_json(params: dict, username: str, *, environment: str) -> dict:
    try:
        params = bind_collect_time_ms(params)
    except ValueError as exc:
        raise CloudConfigError(str(exc)) from None
    config = environment_config(environment)
    environment = config['environment']
    url = _query_url(config["origin"], params)
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(config["origin"])
    if parsed.scheme != "https" or parsed.netloc != expected.netloc or parsed.path != PAGINATION_PATH:
        raise CloudConfigError("列表查询必须使用固定环境端点与路径")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "content-type": "application/json", "oper-path": "msg-search"},
        method="GET",
    )
    ticket = _audit_begin(params, environment)
    http_status = None
    payload = None
    classification = 'client_failed'
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=60) as response:
                response_status = getattr(response, 'status', None)
                if type(response_status) is int and 200 <= response_status <= 299:
                    http_status = response_status
                payload = _read_bounded_json(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            http_status = code
            exc.close()
            if 300 <= code < 400:
                raise CloudResponseError(f"{environment} 查询拒绝 HTTP 重定向 {code}") from exc
            if code in {401, 403}:
                raise CloudAuthError(operation='query', environment=environment, http_status=code) from None
            if 500 <= code < 600:
                raise CloudServerError(f"{environment} 查询服务失败 HTTP {code}") from exc
            raise CloudResponseError(f"{environment} 查询失败 HTTP {code}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, socket.timeout):
                raise CloudNetworkError(f"{environment} 查询网络超时") from exc
            raise CloudNetworkError(f"{environment} 查询网络连接失败") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise CloudNetworkError(f"{environment} 查询网络连接失败") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise CloudResponseError(f"{environment} 查询返回非有效 JSON") from exc
        if not isinstance(payload, dict):
            raise CloudResponseError(f"{environment} 查询返回格式无效")
        business_code = payload.get('code')
        if type(business_code) in (int, str) and business_code in (401, 403, '401', '403'):
            raise CloudAuthError(operation='query', environment=environment,
                                 business_code=int(business_code))
        if 'success' in payload and type(payload['success']) is not bool:
            raise CloudResponseError(f"{environment} 查询 success 字段类型无效")
        if business_code is not None and type(business_code) not in (int, str):
            raise CloudResponseError(f"{environment} 查询 code 字段类型无效")
        if payload.get("success") is False:
            raise CloudResponseError(f"{environment} 查询失败: " + _safe_service_error(payload.get("message"), username))
        if business_code not in (None, 0, "0", 200, "200"):
            raise CloudResponseError(f"{environment} 查询返回非成功业务状态")
        if _known_prod_null(payload, params, username, environment, http_status):
            evidence = _compatibility_evidence(params, ticket)
            if ticket is not None:
                state = ticket[0]
                # Validate the whole finite list before exposing any evidence
                # to the completion callback that persists this request.
                validate_empty_response_compat(state['compat'] + [evidence], state['request'],
                                               environment=environment, role=state['role'])
                state['compat'].append(deepcopy(evidence))
            classification = 'known_prod_success_null_empty'
            return _ValidatedListPayload({**payload, 'data': {'list': [], 'total': 0}},
                source_data_type='null', compatibility_evidence=evidence)
        if not isinstance(payload.get('data'), dict):
            reason = ('data_missing' if 'data' not in payload else
                      'data_null' if payload['data'] is None else 'data_not_object')
            successful_code = (int(business_code) if type(business_code) in (int, str)
                               and business_code in (0, '0', 200, '200') else None)
            raise CloudResponseContractError(reason=reason, operation='query', environment=environment,
                                             http_status=http_status, business_code=successful_code)
        classification = 'valid_data_object'
        return _ValidatedListPayload(payload, source_data_type='object')
    except CloudAuthError:
        classification = 'authentication_failed'
        raise
    except CloudNetworkError:
        classification = 'network_failed'
        raise
    except CloudServerError:
        classification = 'server_failed'
        raise
    except CloudResponseContractError:
        classification = 'response_contract_failed'
        raise
    except CloudResponseError:
        classification = 'response_failed'
        raise
    finally:
        _audit_complete(ticket, payload, http_status, classification)


def build_query_params(*, vin: str, start: str, end: str, log_class: str, username: str,
                       page_no: int = 1, page_size: int = 100, eea_platform: str = EEA_PLATFORM) -> dict:
    if not vin or not log_class or page_no <= 0 or page_size <= 0:
        raise CloudConfigError("查询参数无效")
    if eea_platform != EEA_PLATFORM:
        raise CloudConfigError(f"EEA 平台必须是 {EEA_PLATFORM}")
    params = {
        "vin": vin,
        "logClass": log_class,
        "collectTimeMin": start,
        "collectTimeMax": end,
        "remoteMode": "false",
        "pageNo": page_no,
        "pageSize": page_size,
        "userName": username,
        "eeaPlatform": EEA_PLATFORM,
    }
    if log_class == WALKTHROUGH_LOG_CLASS:
        params.update({
            "logLevel": "",
            "categoryCode": "",
            "categoryId": "",
            "keywordCode": "",
            "domain": "",
        })
    else:
        raise CloudConfigError(f"不支持的 logClass: {log_class}")
    return params


def extract_download_hosts(items: list[dict]) -> list[str]:
    hosts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("downloadURL", "downloadUrl", "url"):
            value = item.get(field)
            if not value:
                continue
            parsed = urllib.parse.urlsplit(str(value))
            host = (parsed.hostname or "").strip().lower()
            if host:
                hosts.append(f"{parsed.scheme.lower() or 'unknown'}://{host}")
            break
    return sorted(dict.fromkeys(hosts))


def query_files(*, vin: str, start: str, end: str, log_class: str, username: str,
                page_size: int = 100, eea_platform: str = EEA_PLATFORM, environment: str = "prod") -> QueryResult:
    env = normalize_cloud_env(environment)
    all_items: list[dict] = []
    page_no = 1
    expected_total: int | None = None
    seen_pages: set[str] = set()
    seen_records: set[str] = set()
    while True:
        params = build_query_params(
            vin=vin, start=start, end=end, log_class=log_class, username=username,
            page_no=page_no, page_size=page_size, eea_platform=eea_platform,
        )
        payload = request_json(params, username, environment=env)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CloudResponseError(f"{env} 查询 data 格式无效")
        items = data.get("list") if "list" in data else data.get("records")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise CloudResponseError(f"{env} 查询文件列表格式无效")
        raw_total = data.get("total")
        if isinstance(raw_total, bool) or not re.fullmatch(r'\d+', str(raw_total)):
            raise CloudResponseError(f"{env} 查询缺少有效 total，不能判断为空")
        total = int(raw_total)
        if total < 0:
            raise CloudResponseError(f"{env} 查询 total 无效")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise CloudResponseError(f"{env} 查询 total 在分页期间不一致")
        if len(all_items) + len(items) > total:
            raise CloudResponseError(f"{env} 查询记录数超过 total")
        page_fingerprint = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if page_fingerprint in seen_pages:
            raise CloudResponseError(f"{env} 查询分页重复且未推进")
        seen_pages.add(page_fingerprint)
        record_keys = {
            json.dumps((item.get("fileKey"), item.get("fileName") or item.get("name"),
                        item.get("collectTime"), item.get("downloadURL") or item.get("downloadUrl") or item.get("url")),
                       ensure_ascii=False, separators=(",", ":"))
            for item in items
        }
        if items and record_keys and record_keys.issubset(seen_records):
            raise CloudResponseError(f"{env} 查询分页没有新增记录")
        seen_records.update(record_keys)
        if not items and len(all_items) < total:
            raise CloudResponseError(f"{env} 查询在达到 total 前返回空页")
        all_items.extend(items)
        if not items or len(all_items) >= total:
            config = environment_config(env)
            return QueryResult(
                items=all_items,
                total=total,
                environment=env,
                provider_environment=config["providerEnvironment"],
                origin=config["origin"],
                log_class=log_class,
                request_host=urllib.parse.urlsplit(config["origin"]).hostname or "",
                download_hosts=extract_download_hosts(all_items),
            )
        page_no += 1


def safe_filename(item: dict, index: int = 0) -> str:
    value = str(item.get("fileName") or item.get("name") or f"source-{index}.zst")
    name = Path(value.replace("\\", "/")).name
    if name in {"", ".", ".."}:
        raise CloudResponseError("文件记录缺少有效文件名")
    return name


def validate_download_url(url: str, *, environment: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(str(url))
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    trusted_https = trusted_download_hosts(environment, "https")
    trusted_http = trusted_download_hosts(environment, "http")
    if scheme == "https" and host in trusted_https and parsed.netloc and not parsed.username and not parsed.password:
        return scheme, host
    if scheme == "http" and host in trusted_http and parsed.netloc and not parsed.username and not parsed.password and parsed.port in {None, 80}:
        return scheme, host
    raise CloudResponseError(f"{environment} 文件下载地址不在白名单中")


def _download_once(url: str, destination: Path, *, environment: str, max_bytes: int, byte_budget=None) -> int:
    environment = normalize_cloud_env(environment)
    scheme, host = validate_download_url(url, environment=environment)
    parsed = urllib.parse.urlsplit(str(url))
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    part.unlink(missing_ok=True)
    request = urllib.request.Request(str(url), method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    total = 0
    charged = 0
    committed = False
    try:
        with opener.open(request, timeout=180) as response, part.open("xb") as output:
            resolved = urllib.parse.urlsplit(response.geturl())
            if (resolved.scheme.lower(), resolved.hostname, resolved.port) != (scheme, host, parsed.port):
                raise CloudResponseError(f"{environment} 文件下载拒绝跨主机重定向")
            declared = response.headers.get("Content-Length")
            declared_bytes = None
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except ValueError as exc:
                    raise CloudResponseError(f"{environment} 文件响应长度无效") from exc
                if declared_bytes < 0:
                    raise CloudResponseError(f"{environment} 文件响应长度无效")
                if declared_bytes > max_bytes:
                    raise CloudResponseError(f"{environment} 文件超过单文件大小限制")
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise CloudResponseError(f"{environment} 文件超过单文件大小限制")
                if byte_budget is not None:
                    byte_budget.consume(len(chunk))
                    charged += len(chunk)
                output.write(chunk)
            if total == 0:
                raise CloudResponseError(f"{environment} 文件下载为空")
            if declared_bytes is not None and total != declared_bytes:
                raise CloudNetworkError(
                    f"{environment} 文件下载连接提前中断（期望 {declared_bytes} 字节，实际 {total} 字节）"
                )
            output.flush()
            os.fsync(output.fileno())
        part.replace(destination)
        committed = True
        return total
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            upstream_code = _bounded_download_upstream_code(exc) if type(code) is int and code == 400 else None
        finally:
            exc.close()
        if type(code) is not int or not 100 <= code <= 599:
            raise CloudResponseError(f"{environment} 文件下载返回无效 HTTP 状态") from None
        if 300 <= code < 400:
            raise CloudResponseError(f"{environment} 文件下载拒绝 HTTP 重定向 {code}") from None
        if code in {401, 403}:
            raise CloudAuthError(operation='download', environment=environment, http_status=code) from None
        if code == 400:
            raise _download_failure('http_bad_request', environment, http_status=code,
                                    upstream_code=upstream_code) from None
        if code == 429:
            raise _download_failure('http_rate_limited', environment, http_status=code) from None
        if 500 <= code <= 599:
            raise _download_failure('http_server_error', environment, http_status=code) from None
        raise CloudResponseError(f"{environment} 文件下载失败 HTTP {code}") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            raise CloudNetworkError(f"{environment} 文件下载网络超时") from exc
        raise CloudNetworkError(f"{environment} 文件下载网络连接失败") from exc
    except (http.client.IncompleteRead, socket.timeout, ConnectionResetError, BrokenPipeError) as exc:
        raise CloudNetworkError(f"{environment} 文件下载连接提前中断") from exc
    finally:
        try:
            part.unlink(missing_ok=True)
        finally:
            if byte_budget is not None and charged and not committed:
                byte_budget.release(charged)


def download_to_file(url: str, destination: Path, *, environment: str, max_bytes: int = MAX_DOWNLOAD_BYTES,
                     attempts: int = DOWNLOAD_ATTEMPTS, byte_budget=None) -> int:
    """Atomically download; retry bounded network, rate-limit and server failures."""
    if type(attempts) is not int or not 1 <= attempts <= DOWNLOAD_ATTEMPTS:
        raise CloudConfigError(f"文件下载重试次数必须为 1 至 {DOWNLOAD_ATTEMPTS} 的整数")
    environment = normalize_cloud_env(environment)
    for attempt in range(1, attempts + 1):
        try:
            extra = {'byte_budget': byte_budget} if byte_budget is not None else {}
            return _download_once(url, destination, environment=environment, max_bytes=max_bytes, **extra)
        except CloudDownloadError as error:
            error.attempts = attempt
            diagnostic = download_failure_diagnostic(error)
            if not diagnostic['retryable'] or attempt == attempts:
                raise
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
        except CloudNetworkError as exc:
            if attempt == attempts:
                raise _download_failure('network_error', environment, attempts=attempt) from None
            time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)


def ordered_files(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (
        str(item.get("collectTime") or ""), str(item.get("fileName") or item.get("name") or ""),
        str(item.get("fileKey") or ""),
    ))
