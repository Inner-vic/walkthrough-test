"""Query only the platform product label in a fixed group; never dispatch logs.

The returned label is a query-time platform value, not a historical-window
version. All transport is through the existing pinned user-profile policy.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from request_model import InputError
import report_policy as policy

VERSION = 'product-version-v1'
SEMANTICS = 'query_time_platform_value'
VIN = re.compile(r'[A-HJ-NPR-Z0-9]{17}')
CODE = re.compile(r'[0-9A-F]{4}')
MESSAGE = re.compile(r'om_[A-Za-z0-9]+')
THREAD = re.compile(r'omt?_[A-Za-z0-9]+')
ARTIFACT = re.compile(r'(?:\*\*)?相关制品信息\s*[:：]\s*(?:\*\*)?\s*([^\n<]+)')


class DiscoveryWindowExpired(ValueError):
    pass


def validate_config(settings):
    """Validate product configuration only, without filesystem or network IO."""
    cfg = settings.get('product_version') if isinstance(settings, dict) else None
    if not isinstance(cfg, dict):
        raise InputError('缺少 product_version 固定群版本查询配置。')
    out = {}
    for name, pattern, optional in (
            ('query_chat_id', r'oc_[A-Za-z0-9]+', False),
            ('asgard_bot_id', r'ou_[A-Za-z0-9]+', False),
            ('asgard_app_id', r'cli_[A-Za-z0-9]+', True)):
        value = cfg.get(name)
        if optional and value in (None, ''):
            out[name] = None
        elif not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise InputError('product_version 的 '+name+' 格式无效。')
        else:
            out[name] = value
    for name, default, low, high in (
            ('timeout_seconds', 120, 1, 600), ('poll_interval_seconds', 2, 0.1, 30)):
        value = cfg.get(name, default)
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise InputError('product_version 的 '+name+' 超出允许范围。')
        out[name] = value
    if out['poll_interval_seconds'] > out['timeout_seconds']:
        raise InputError('制品查询轮询间隔不能超过超时。')
    return out


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _binding(settings, context, cfg):
    if not isinstance(context, dict) or VIN.fullmatch(str(context.get('vin', ''))) is None:
        raise InputError('制品查询需要有效 VIN。')
    operator = context.get('operator_open_id')
    if not isinstance(operator, str) or re.fullmatch(r'ou_[A-Za-z0-9]+', operator) is None:
        raise InputError('制品查询需要固定的操作用户。')
    task = context.get('task_key')
    if not isinstance(task, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', task) is None:
        raise InputError('制品查询需要有效任务标识。')
    configured = settings.get('expected_dispatch_open_id')
    if configured and configured != operator:
        raise InputError('制品查询任务用户与固定操作用户不一致。')
    profile = settings.get('lark_profile')
    home = settings.get('lark_home')
    app = settings.get('expected_lark_app_id')
    if not isinstance(profile, str) or not profile.strip() or not isinstance(home, str) or not home.strip():
        raise InputError('制品查询必须绑定显式 User profile 与独立 lark_home。')
    if not isinstance(app, str) or re.fullmatch(r'cli_[A-Za-z0-9]+', app) is None:
        raise InputError('制品查询必须绑定固定飞书 App ID。')
    return {'vin':context['vin'], 'task_key':task, 'operator_open_id':operator,
            'query_chat_id':cfg['query_chat_id'], 'asgard_bot_id':cfg['asgard_bot_id'],
            'asgard_app_id':cfg['asgard_app_id'], 'lark_profile':profile,
            'lark_home':os.path.normcase(os.path.abspath(os.path.expanduser(home))),
            'expected_lark_app_id':app}


def _plain(path):
    if path.exists() and (path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0)
                         & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
        raise InputError('制品查询状态路径不能使用链接或目录联接。')


@contextmanager
def _job_lock(directory):
    """A crash-released lock prevents concurrent callers sending the same task."""
    path = directory/'product-version.lock'
    _plain(path)
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            raise InputError('同一任务的制品查询正在执行，请沿用原任务等待。') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save(path, record):
    value = dict(record)
    value['checkpoint_sha256'] = _digest(record)
    policy.atomic_json(path, value)


def _load(path, binding):
    _plain(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        checksum = data.pop('checkpoint_sha256')
    except (ValueError, KeyError, TypeError):
        raise InputError('制品查询 checkpoint 无法验证，禁止重新派发。') from None
    if (checksum != _digest(data) or data.get('version') != VERSION or data.get('binding') != binding):
        raise InputError('制品查询 checkpoint 的任务、身份或固定群绑定不一致，禁止复用。')
    return data


def _result(record, status, reason, evidence=None):
    query = dict(record.get('query') or {})
    result = {'status':status, 'reason':reason, 'code':None, 'label':None, 'binding':dict(record['binding']),
              'vin':record['binding']['vin'], 'raw_related_product':None, 'artifact_info':None,
              'query':query, 'response':None, 'query_message_id':query.get('message_id'),
              'response_message_id':None, 'queried_at':query.get('sent_at'), 'response_at':None,
              'semantics':SEMANTICS, 'historical_version_verified':False,
              'source_reported_at':None, 'source_versions':{}, 'source_environment':None,
              'quality':{'status':'reference_only', 'issues':[
                  {'kind':'historical_version_unverified',
                   'message':'查询时平台返回的相关制品信息，未证明属于任一历史测试时段。'}]}}
    if evidence:
        result.update(evidence)
    return result


def validate_result(settings, context, result):
    """Validate saved result identity and meaning without IO or a new query."""
    binding = _binding(settings, context, validate_config(settings))
    if (not isinstance(result, dict) or result.get('binding') != binding
            or result.get('vin') != binding['vin']
            or result.get('semantics') != SEMANTICS or result.get('historical_version_verified') is not False
            or result.get('status') not in ('ok', 'missing', 'failed', 'unknown', 'timeout')
            or not isinstance(result.get('reason'), str) or not result['reason']
            or not isinstance(result.get('query'), dict)):
        raise InputError('制品查询结果的任务、身份、固定群或时间语义不一致。')
    query = result['query']
    if query.get('command') != '查询车辆版本 '+binding['vin']+' CCUFSD':
        raise InputError('制品查询结果命令与任务不一致。')
    query_id = result.get('query_message_id')
    if query_id != query.get('message_id') or query_id is not None and MESSAGE.fullmatch(str(query_id)) is None:
        raise InputError('制品查询结果丢失原查询消息关联。')
    if query.get('thread_id') is not None and THREAD.fullmatch(str(query['thread_id'])) is None:
        raise InputError('制品查询结果话题格式无效。')
    if result.get('queried_at') != query.get('sent_at'):
        raise InputError('制品查询时间证据不一致。')
    if result['status'] == 'ok':
        raw = result.get('raw_related_product')
        response = result.get('response')
        if (not isinstance(raw, str) or not raw or len(raw) > 4096
                or CODE.fullmatch(str(result.get('code') or '')) is None
                or re.split(r'[,，]', raw, maxsplit=1)[0].strip() != result['code']
                or result.get('artifact_info') != raw or result.get('label') != result['code']
                or not query_id or not isinstance(response, dict)
                or MESSAGE.fullmatch(str(result.get('response_message_id') or '')) is None
                or result['response_message_id'] != response.get('message_id')
                or result.get('response_at') != response.get('create_time')
                or response.get('asgard_bot_id') != binding['asgard_bot_id']
                or response.get('asgard_app_id') != binding['asgard_app_id']
                or result.get('quality', {}).get('status') != 'reference_only'):
            raise InputError('制品码、原字段或指定机器人回复证据不一致。')
        correlation = response.get('correlation')
        if not isinstance(correlation, dict) or correlation.get('query_message_id') != query_id:
            raise InputError('制品回复缺少准确的原查询消息关联证据。')
        if correlation.get('kind') == 'reply_to':
            if response.get('reply_to') != query_id:
                raise InputError('制品回复的 reply_to 与原查询消息不一致。')
        elif correlation.get('kind') == 'thread':
            if (not query.get('thread_id') or correlation.get('thread_id') != query['thread_id']
                    or response.get('thread_id') != query['thread_id']
                    or response.get('reply_to') not in (None, '', query_id)
                    or not response.get('reply_to') and correlation.get('query_message_ids') != [query_id]):
                raise InputError('制品回复的话题与原查询话题不一致。')
        else:
            raise InputError('制品回复使用了未知的消息关联方式。')
    elif any(result.get(name) is not None for name in ('code', 'label', 'raw_related_product', 'artifact_info', 'response_message_id')):
        raise InputError('不可用制品查询不能保留未经验证的制品码。')
    return result


def _data(payload):
    if (not isinstance(payload, dict) or payload.get('ok') is not True
            or payload.get('identity') != 'user' or not isinstance(payload.get('data'), dict)):
        raise ValueError('CLI envelope invalid')
    return payload['data']


def _complete(payload):
    data = _data(payload)
    pagination = (payload.get('meta') or {}).get('pagination') or {}
    if data.get('has_more') is True or pagination.get('complete') is False:
        raise ValueError('Reply list is incomplete')
    return data


def _sender_user(sender):
    if not isinstance(sender, dict):
        return None
    return sender.get('open_id') or (sender.get('id') if sender.get('id_type') == 'open_id' else None)


def _root_from_payload(payload, record):
    messages = _data(payload).get('messages')
    if not isinstance(messages, list):
        raise ValueError('Missing query message')
    roots = [x for x in messages if isinstance(x, dict) and x.get('message_id') == record['query']['message_id']]
    if len(roots) != 1:
        raise ValueError('Query root is not unique')
    root = roots[0]; binding = record['binding']
    if (root.get('chat_id') != binding['query_chat_id'] or root.get('deleted') is True
            or _sender_user(root.get('sender')) != binding['operator_open_id']):
        raise ValueError('Query root identity mismatch')
    content = root.get('content')
    commands = re.findall(r'查询车辆版本\s+([A-HJ-NPR-Z0-9]{17})\s+CCUFSD\b', str(content or ''))
    if commands != [binding['vin']]:
        raise ValueError('Query root command mismatch')
    if root.get('thread_id') is not None and THREAD.fullmatch(str(root['thread_id'])) is None:
        raise ValueError('Invalid query thread')
    return root


def _field(content, name):
    match = re.search(r'(?:\*\*)?'+re.escape(name)+r'\s*[:：]\s*(?:\*\*)?\s*([^\n<]+)', content)
    return match.group(1).strip() if match else None


def _extract_replies(messages, record, thread_id=None):
    if not isinstance(messages, list):
        raise ValueError('Invalid reply list')
    binding = record['binding']; candidates = []
    query_ids = sorted({message.get('message_id') for message in messages
        if isinstance(message, dict) and isinstance(message.get('message_id'), str)
        and re.search(r'查询车辆版本\s+[A-HJ-NPR-Z0-9]{17}\s+CCUFSD\b', str(message.get('content') or ''))})
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Invalid reply')
        if message.get('chat_id') not in (None, binding['query_chat_id']):
            raise ValueError('Reply chat mismatch')
        if thread_id and message.get('thread_id') not in (None, thread_id):
            raise ValueError('Reply thread mismatch')
        if message.get('message_id') == record['query']['message_id'] or message.get('deleted') is True:
            continue
        sender = message.get('sender') or {}
        if sender.get('open_bot_id') != binding['asgard_bot_id']:
            continue
        if thread_id and message.get('reply_to') not in (None, '', record['query']['message_id']):
            continue  # Explicit reply ownership overrides a shared thread.
        if not thread_id and message.get('reply_to') != record['query']['message_id']:
            raise ValueError('Reply is not bound to this query')
        if binding['asgard_app_id'] and (sender.get('id_type') != 'app_id' or sender.get('id') != binding['asgard_app_id']):
            raise ValueError('Asgard application mismatch')
        content = message.get('content')
        if not isinstance(content, str) or len(content) > 256000:
            raise ValueError('Invalid reply content')
        matches = ARTIFACT.findall(content)
        if not matches:
            continue
        if thread_id and not message.get('reply_to') and query_ids != [record['query']['message_id']]:
            raise ValueError('Thread does not contain exactly this one version query')
        if MESSAGE.fullmatch(str(message.get('message_id') or '')) is None:
            raise ValueError('Reply missing identity')
        for raw in matches:
            raw = raw.strip()
            code = re.split(r'[,，]', raw, maxsplit=1)[0].strip()
            if CODE.fullmatch(code) is None:
                raise ValueError('Product code is not four hexadecimal characters')
            candidates.append((code, raw, message, content))
    if len({(c[0], c[1]) for c in candidates}) > 1:
        raise ValueError('Conflicting product replies')
    if not candidates:
        return None
    code, raw, message, content = candidates[0]
    versions = {key:value for key in ('CCUFSD_SOC', 'CCUBFSD_SOC', 'CCUFSD_MCU', 'CCUBFSD_MCU', '软件版本')
                if (value := _field(content, key)) is not None}
    product_versions = re.findall(r'\d+\.\d+\.\d+-\d+', raw)
    soc_versions = {v for key, text in versions.items() if key.endswith('_SOC')
                    for v in re.findall(r'\d+\.\d+\.\d+-\d+', text)}
    quality = {'status':'reference_only', 'issues':[
        {'kind':'historical_version_unverified', 'message':'查询时平台返回值，未验证历史测试时段的制品版本。'}]}
    if product_versions and soc_versions and not set(product_versions).intersection(soc_versions):
        quality['issues'].append({'kind':'product_soc_version_mismatch',
                                 'message':'相关制品版本与卡片 SOC 版本不一致，不能据此映射日志时段。'})
    correlation = {'kind':'thread' if thread_id else 'reply_to', 'query_message_id':record['query']['message_id']}
    if thread_id:
        correlation['thread_id'] = thread_id
        correlation['query_message_ids'] = query_ids
    return {'code':code, 'label':code, 'raw_related_product':raw, 'artifact_info':raw,
            'response_message_id':message['message_id'], 'response_at':message.get('create_time'),
            'source_reported_at':_field(content, '上报时间'), 'source_versions':versions,
            'source_environment':_field(content, '云端环境'), 'quality':quality,
            'response':{'message_id':message['message_id'], 'create_time':message.get('create_time'),
                        'update_time':message.get('update_time'), 'observed_at':_now(),
                        'reply_to':message.get('reply_to'), 'thread_id':thread_id,
                        'correlation':correlation,
                        'content_sha256':hashlib.sha256(content.encode()).hexdigest(),
                        'asgard_bot_id':binding['asgard_bot_id'], 'asgard_app_id':binding['asgard_app_id']}}


def _direct_replies(settings, record, timeout):
    """Discover only exact reply_to candidates, then independently read them back.

    Time bounds only limit discovery volume. They never establish ownership.
    """
    sent = datetime.fromisoformat(record['query']['sent_at'].replace('Z', '+00:00'))
    if sent.tzinfo is None:
        raise ValueError('Query timestamp is not timezone-aware')
    now = datetime.now(timezone.utc)
    if now-sent > timedelta(hours=24) or sent-now > timedelta(minutes=1):
        raise DiscoveryWindowExpired('Original query is outside bounded discovery range')
    start = sent.replace(microsecond=0).isoformat()
    payload = policy.run_lark(settings, ['im', '+chat-messages-list', '--as', 'user', '--chat-id',
        record['binding']['query_chat_id'], '--start', start, '--end', now.isoformat(), '--order', 'asc',
        '--page-size', '50', '--page-all', '--page-limit', '10', '--no-reactions'], timeout=timeout,
        profile=record['binding']['lark_profile'])
    messages = _complete(payload).get('messages')
    if not isinstance(messages, list):
        raise ValueError('Invalid fixed-chat discovery response')
    candidates = {}
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Invalid discovery message')
        if (message.get('reply_to') != record['query']['message_id']
                or (message.get('sender') or {}).get('open_bot_id') != record['binding']['asgard_bot_id']
                or message.get('deleted') is True):
            continue
        if (message.get('chat_id') != record['binding']['query_chat_id']
                or MESSAGE.fullmatch(str(message.get('message_id') or '')) is None):
            raise ValueError('Correlated discovery identity mismatch')
        candidates[message['message_id']] = message
    if not candidates:
        return None
    readback = []
    ids = sorted(candidates)
    for index in range(0, len(ids), 50):
        batch = ids[index:index+50]
        payload = policy.run_lark(settings, ['im', '+messages-mget', '--as', 'user', '--message-ids',
            ','.join(batch), '--no-reactions'], timeout=timeout, profile=record['binding']['lark_profile'])
        values = _complete(payload).get('messages')
        if (not isinstance(values, list) or len(values) != len(batch)
                or any(not isinstance(m, dict) for m in values)
                or {m.get('message_id') for m in values} != set(batch)):
            raise ValueError('Correlated reply readback is incomplete')
        for message in values:
            if (message.get('chat_id') != record['binding']['query_chat_id']
                    or message.get('reply_to') != record['query']['message_id']
                    or (message.get('sender') or {}).get('open_bot_id') != record['binding']['asgard_bot_id']
                    or message.get('deleted') is True):
                raise ValueError('Correlated reply readback identity changed')
        readback.extend(values)
    return _extract_replies(readback, record)


def _observe(settings, record, remaining):
    timeout = max(1, min(30, math.ceil(remaining)))
    payload = policy.run_lark(settings, ['im', '+messages-mget', '--as', 'user', '--message-ids',
                              record['query']['message_id'], '--no-reactions'], timeout=timeout,
                              profile=record['binding']['lark_profile'])
    root = _root_from_payload(payload, record)
    record['query']['create_time'] = root.get('create_time')
    thread = root.get('thread_id')
    if record['query'].get('thread_id') and record['query']['thread_id'] != thread:
        raise ValueError('Query thread changed')
    if thread:
        record['query']['thread_id'] = thread
        replies = policy.run_lark(settings, ['im', '+threads-messages-list', '--as', 'user', '--thread', thread,
                                  '--order', 'asc', '--page-size', '50', '--page-all', '--page-limit', '10',
                                  '--no-reactions'], timeout=timeout, profile=record['binding']['lark_profile'])
        return _extract_replies(_complete(replies).get('messages'), record, thread)
    # A normal group reply can carry exact reply_to without a thread_id. Do
    # not infer association from an adjacent card, VIN or an embedded preview.
    return _direct_replies(settings, record, timeout)


def _event(callback, stage):
    if callback:
        callback({'scope':'product_version', 'stage':stage})


def get_product_version(settings, context, directory, *, on_stage=None):
    """Return a verified platform label or an explicit non-available outcome.

    Configuration/checkpoint binding errors raise InputError. Transport failures
    never leak upstream exception text and never trigger another send.
    """
    cfg = validate_config(settings); binding = _binding(settings, context, cfg)
    directory = Path(directory); _plain(directory)
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = directory/'product-version.json'
    with _job_lock(directory):
        record = _load(checkpoint, binding)
        if record is None:
            record = {'version':VERSION, 'binding':binding, 'phase':'prepared', 'created_at':_now(),
                      'query':{'command':'查询车辆版本 '+binding['vin']+' CCUFSD'}, 'result':None}
            _save(checkpoint, record)
        if record['phase'] == 'ready':
            result = record.get('result') or {}
            validate_result(settings, context, result)
            if (result.get('status') != 'ok' or result.get('query') != record.get('query')):
                raise InputError('制品查询已完成记录无效，禁止丢弃证据重发。')
            return result
        if record['phase'] in ('send_pending', 'unknown'):
            return _result(record, 'unknown', '版本查询发送结果尚未确认；保留原记录核查，不自动重发。')
        if record['phase'] == 'failed':
            return validate_result(settings, context, record['result'])
        if record['phase'] not in ('prepared', 'sent', 'timeout', 'poll_failed'):
            raise InputError('制品查询状态未知，禁止重新派发。')
        try:
            auth = policy.ensure_user_authorization(settings, binding['operator_open_id'])
        except policy.UserAuthorizationError:
            result = _result(record, 'failed', '固定 User 身份需要重新授权；制品信息未获取。')
            record['result'] = result; _save(checkpoint, record)
            return result
        except InputError as exc:
            if any(marker in str(exc) for marker in ('不一致', '身份', '用户已变化')):
                raise InputError('制品查询固定用户或 App 身份校验不一致，禁止继续。') from None
            result = _result(record, 'failed', '固定 User 身份授权暂未通过；制品信息未获取。')
            record['result'] = result; _save(checkpoint, record)
            return result
        except Exception:
            result = _result(record, 'failed', '固定 User 身份授权未通过；日志统计可独立保留，制品信息未获取。')
            record['result'] = result; _save(checkpoint, record)
            return result
        if auth.get('open_id') != binding['operator_open_id']:
            raise InputError('制品查询授权身份与任务固定用户不一致。')
        if record['phase'] == 'prepared':
            _event(on_stage, 'sending')
            record['phase'] = 'send_pending'; record['query']['sent_at'] = _now()
            record['query']['idempotency_key'] = _digest({'binding':binding, 'operation':'product_version'})[:48]
            _save(checkpoint, record)
            content = json.dumps({'text':'<at user_id="'+binding['asgard_bot_id']+'">阿斯加德</at> '+record['query']['command']}, ensure_ascii=False)
            try:
                payload = policy.run_lark(settings, ['im', '+messages-send', '--as', 'user', '--chat-id',
                    binding['query_chat_id'], '--msg-type', 'text', '--content', content,
                    '--idempotency-key', record['query']['idempotency_key']],
                    timeout=max(1, min(60, math.ceil(cfg['timeout_seconds']))), profile=binding['lark_profile'])
                sent = _data(payload)
                message_id = sent.get('message_id') or (sent.get('message') or {}).get('message_id')
                if MESSAGE.fullmatch(str(message_id or '')) is None or sent.get('chat_id') not in (None, binding['query_chat_id']):
                    raise ValueError('Send acknowledgement invalid')
                record['query']['message_id'] = message_id
                record['query']['send_create_time'] = sent.get('create_time')
                record['phase'] = 'sent'; _save(checkpoint, record)
            except Exception:
                record['phase'] = 'unknown'
                record['result'] = _result(record, 'unknown', '版本查询可能已发送但回执未确认；保留记录，不自动重发。')
                _save(checkpoint, record)
                return record['result']
        _event(on_stage, 'waiting_reply')
        deadline = time.monotonic()+cfg['timeout_seconds']
        while time.monotonic() < deadline:
            try:
                evidence = _observe(settings, record, deadline-time.monotonic())
            except DiscoveryWindowExpired:
                record['phase'] = 'failed'
                record['result'] = _result(record, 'missing', '原版本查询已超出 24 小时有界读取范围；保留记录，不自动重新发送。')
                _save(checkpoint, record)
                return record['result']
            except ValueError:
                record['phase'] = 'failed'
                record['result'] = _result(record, 'failed', '版本回复的来源、消息关联、四位制品码或唯一性未通过校验。')
                _save(checkpoint, record)
                return record['result']
            except Exception:
                record['phase'] = 'poll_failed'
                record['result'] = _result(record, 'failed', '版本查询已发送，但读取指定回复失败；重试只读取原消息。')
                _save(checkpoint, record)
                return record['result']
            if evidence:
                record['phase'] = 'ready'
                record['result'] = _result(record, 'ok', '已取得查询时平台返回的相关制品信息；历史测试时段归属未验证。', evidence)
                validate_result(settings, context, record['result'])
                _save(checkpoint, record); _event(on_stage, 'ready')
                return record['result']
            _save(checkpoint, record)
            remaining = deadline-time.monotonic()
            if remaining > 0:
                time.sleep(min(cfg['poll_interval_seconds'], remaining))
        record['phase'] = 'timeout'
        record['result'] = _result(record, 'timeout', '限定时间内未取得该查询消息对应的制品信息；重试只读取原消息。')
        _save(checkpoint, record)
        return record['result']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--vin', required=True)
    parser.add_argument('--task-key', required=True)
    parser.add_argument('--operator-open-id', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = json.loads(args.settings.read_text(encoding='utf-8-sig'))
        policy.WORKSPACE_ROOT = Path(settings.get('workspace_root') or Path.cwd()).resolve()
        policy.activate_lark_environment(settings)
        result = get_product_version(settings, {'vin':args.vin, 'task_key':args.task_key,
                                     'operator_open_id':args.operator_open_id}, args.directory)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'ok' else 2
    except InputError as exc:
        print(json.dumps({'status':'failed', 'reason':str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
