"""Only the explicit 走查测试 trigger may create this independent business task."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from request_model import InputError, parse_comparison_request, TIME_RANGE_PATTERN
from request_context import followup_identity_from_context, RequestContextError


def request_identity(header):
    """Validate transport identity exactly as the standalone worker does."""
    try:
        parsed = followup_identity_from_context(header)
    except RequestContextError as exc:
        raise InputError(str(exc)) from None
    fields = {'chat_id':parsed['chat_id'],'sender_id':parsed['requester_id'],'message_id':parsed['message_id']}
    values = {}
    for source,target,pattern in [('chat_id','source_chat_id',r'(?:chat:)?oc_[A-Za-z0-9]+'),
                                   ('sender_id','sender_id',r'ou_[A-Za-z0-9]+'),
                                   ('message_id','source_message_id',r'om_[A-Za-z0-9]+')]:
        if not re.fullmatch(pattern,fields.get(source,'')):
            raise InputError('请求上下文缺少或包含无效 '+source+'。')
        values[target] = fields[source]
    values['source_thread_id'] = values['source_message_id']
    return values


def source_normalization_anchor(settings, header, received_at=None, *, identity=None):
    """Read an exact source record's clock; never choose a recent task."""
    identity = request_identity(header) if identity is None else identity
    key = 'walkthrough-'+hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
    path = Path(settings['runtime_state_dir'])/(key+'.json')
    if not path.is_file():
        return received_at
    record = json.loads(path.read_text(encoding='utf-8'))
    if (record.get('kind') != 'walkthrough_test_local' or record.get('job_key') != key
            or any(record.get(field) != value for field,value in identity.items())):
        raise InputError('同一来源消息的走查身份绑定不一致，拒绝串用记录。')
    normalization = (record.get('request') or {}).get('normalization') or {}
    # Legacy records predate the audit envelope; their actual creation time is
    # the only permitted recovery anchor, never the current day's clock.
    anchor = normalization.get('received_at') or record.get('created_at')
    if not isinstance(anchor,str) or not anchor.strip():
        raise InputError('已有走查任务缺少可验证的接收时间，未重新解析或创建任务。')
    return anchor


def _shared_input_module(module_name):
    """Load only an allowlisted sibling input module, not another Skill's modules."""
    if module_name not in ('natural_input','semantic_input'):
        raise InputError('自然语言输入规范化组件名称异常，未创建走查任务。')
    source = (Path(__file__).resolve().parents[2] / 'chassis-business-routing' /
              'scripts' / (module_name+'.py')).resolve()
    name = '_ccconnect_chassis_'+module_name
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(existing.__file__).resolve() != source:
            raise InputError('自然语言输入规范化组件路径异常，未创建走查任务。')
        return existing
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise InputError('自然语言输入规范化组件不可用，未创建走查任务。')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _natural_input_module():
    return _shared_input_module('natural_input')


def _missing_fields(message, text):
    """Keep clarification business-specific; never invent missing dates or VINs."""
    if '未识别到 VIN' in message:
        return ['VIN']
    if '多个 VIN' in message:
        return ['同一车辆 VIN']
    if '两个时间段' in message and '重叠' not in message and '标记' not in message:
        count = len(list(TIME_RANGE_PATTERN.finditer(text)))
        if count == 1:
            return ['第二段开始时间', '第二段结束时间']
        return ['两段完整的开始和结束时间']
    if '重叠' in message:
        return ['两个不重叠时段']
    if '角色' in message or '标记' in message:
        return ['Base 与 PCM更新的角色对应关系']
    return ['有效的开始和结束时间']


def canonical_time(text):
    value = datetime.fromisoformat(text.replace('Z','+00:00'))
    if value.microsecond:
        raise InputError('走查测试的本地分析入口要求整秒时间。')
    if value.tzinfo:
        value = value.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    return value.isoformat(timespec='seconds')


def route(text, received_at=None, interpretation=None):
    if not isinstance(text,str) or '走查测试' not in text:
        return {'route':'none','status':'no_match','message':'未命中走查测试，不启动本 Skill。'}
    if re.search(r'https?://',text):
        return {'route':'walkthrough-test','status':'needs_input','missing':['VIN 和两个完整时段'],
                'message':'走查测试从原始日志计算；请提供 VIN 和两个完整时段，不接受结果表链接。'}
    original_text = text
    if isinstance(received_at,str):
        try:
            received_at = datetime.fromisoformat(received_at.replace('Z','+00:00'))
        except ValueError:
            return {'route':'walkthrough-test','status':'needs_input','missing':['有效的消息接收时间'],
                    'message':'无法确认本次消息的日期基准，未创建任务；请核对接收时间。'}
    if interpretation is None:
        normalized = _natural_input_module().normalize_input(text, received_at=received_at)
    else:
        normalized = _shared_input_module('semantic_input').apply_interpretation(
            text,interpretation,received_at=received_at)
    if normalized.get('needs_confirmation'):
        return {'route':'walkthrough-test','status':'needs_input',
                'missing':normalized.get('missing') or ['需确认的日期和时间'],
                'message':normalized.get('message') or '请补充或确认日期和时间，未创建任务。',
                'normalization':{'original_text':original_text,'canonical_text':normalized['text'],
                                 'source_sha256':hashlib.sha256(original_text.encode('utf-8')).hexdigest(),
                                 'received_at':normalized['received_at'],'changes':normalized.get('changes',[])}}
    text = normalized['text']
    # Match the existing request grammar without changing the old router/parser.
    pattern = r'(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:到|至|~|～|—|-)\s*(\d{1,2}:\d{2}(?::\d{2})?)(?![:\d])'
    expanded = re.sub(pattern,lambda m:f'{m[1]} {m[2]} - {m[1]} {m[3]}',text)
    changes = list(normalized.get('changes',[]))
    if expanded != text:
        changes.append({'kind':'range_completion','original':text,'normalized':expanded,
                        'basis':'同一明确时段的结束时刻沿用开始日期，不跨消息或跨任务补齐'})
    try:
        request = parse_comparison_request(expanded)
        if _shared_input_module('semantic_input').has_unbound_clocks(
                expanded,list(TIME_RANGE_PATTERN.finditer(expanded))):
            raise InputError('还识别到时段之外的时刻或精度，请明确两段准确的起止时间，不能自动忽略候选时刻。')
        for role in ('baseline','replacement'):
            for field in ('start','end'):
                request[role+'_metadata'][field] = canonical_time(request[role+'_metadata'][field])
        request['comparison_key'] = hashlib.sha256(json.dumps(
            [request['baseline_metadata'],request['replacement_metadata']],sort_keys=True).encode()).hexdigest()[:24]
        request['normalization'] = {'original_text':original_text,'canonical_text':expanded,
                                    'source_sha256':hashlib.sha256(original_text.encode('utf-8')).hexdigest(),
                                    'received_at':normalized['received_at'],
                                    'changes':changes}
        if interpretation is not None:
            request['normalization']['interpretation'] = interpretation
        return {'route':'walkthrough-test','status':'ready','normalized':request,
                'message':'已识别走查测试；从车云取日志并在 Windows 内存中解析和对比。'}
    except (InputError,ValueError) as exc:
        return {'route':'walkthrough-test','status':'needs_input','missing':_missing_fields(str(exc),expanded),
                'message':str(exc),
                'normalization':{'original_text':original_text,'canonical_text':expanded,
                                 'source_sha256':hashlib.sha256(original_text.encode('utf-8')).hexdigest(),
                                 'received_at':normalized['received_at'],'changes':changes}}


def _dispatch_plain(path, *, directory=False):
    """Reject links, junctions and replaced files before selecting route code."""
    import stat
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('untrusted route path')
        if item == path:
            if (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) is not True:
                raise ValueError('unexpected route path type')
            if not directory and getattr(info, 'st_nlink', 1) != 1:
                raise ValueError('hardlinked route file')
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError('invalid route parent')
    return path


def _installed_route_main(argv=None, *, workspace=None):
    """CLI-only bridge; import consumers retain the local compatibility API.

    Read only the deployment installation manifest.  Route in the selected
    interpreter process so that no Skill parser modules share a namespace.
    """
    import os
    import subprocess
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--text', required=True)
    parser.add_argument('--received-at')
    parser.add_argument('--interpretation')
    args = parser.parse_args(argv)
    failed = {'route':'walkthrough-test', 'status':'unavailable', 'missing':[],
              'normalized':{}, 'message':'走查测试独立入口不可用，请检查部署；不会切换到其他业务。'}
    try:
        # Validate JSON in memory but preserve the original argument bytes/text.
        if args.interpretation is not None:
            json.loads(args.interpretation)
        current = Path(__file__).absolute()
        if workspace is None:
            workspace = next(p for p in current.parents if (p/'AGENTS.md').is_file()
                             and (p/'.agents/skills').is_dir())
        workspace = _dispatch_plain(workspace, directory=True)
        manifest = _dispatch_plain(workspace/'deployment/walkthrough-test.installation.json')
        installed = json.loads(manifest.read_text(encoding='utf-8-sig'))
        # The route shim never reads identity settings or invokes a host action.
        target_root = Path(installed['skill_root'])
        host_python = Path(installed['host_python'])
        if not target_root.is_absolute() or not host_python.is_absolute():
            raise ValueError('relative installation route path')
        if '..' in target_root.parts or '..' in host_python.parts:
            raise ValueError('noncanonical installation route path')
        relative = target_root.relative_to(workspace)
        if (len(relative.parts) != 3 or relative.parts[0] != 'runtime'
                # Approve these exact dedicated release names while preserving
                # every other release/path and recursion guard.
                or (re.fullmatch(r'wtr[0-9]{3,}', relative.parts[1]) is None
                    and relative.parts[1] not in ('wtr088-taskenv', 'wtr089-windowenv', 'wtr090-prodnull', 'wtr091-resume', 'wtr092-preflight', 'wtr093-sources', 'wtr094-performance', 'wtr095-lifecycle', 'wtr096-hardening'))
                or relative.parts[2] != 'skill'):
            raise ValueError('unexpected release route path')
        expected_python = workspace/'runtime/wtv081/Scripts/python.exe'
        if host_python != expected_python:
            raise ValueError('unexpected route interpreter')
        _dispatch_plain(target_root, directory=True)
        target = _dispatch_plain(target_root/'scripts/walkthrough_route.py')
        _dispatch_plain(host_python)
        if os.path.normcase(str(target)) == os.path.normcase(str(current)) or target.samefile(current):
            raise ValueError('recursive route dispatch')
        command = [str(host_python), '-B', '-X', 'utf8', str(target), '--text', args.text]
        if args.received_at is not None:
            command.extend(['--received-at', args.received_at])
        if args.interpretation is not None:
            command.extend(['--interpretation', args.interpretation])
        result = subprocess.run(command, cwd=workspace, capture_output=True,
            text=True, encoding='utf-8', check=True, timeout=12, stdin=subprocess.DEVNULL,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        decision = json.loads(result.stdout)
        if (not isinstance(decision, dict)
                or decision.get('route') not in ('none', 'walkthrough-test')
                or decision.get('status') not in ('ready', 'needs_input', 'no_match')
                or not isinstance(decision.get('message'), str)
                or not isinstance(decision.get('normalized', {}), dict)
                or not isinstance(decision.get('missing', []), list)):
            raise ValueError('invalid route decision')
        print(json.dumps(decision, ensure_ascii=False))
    except Exception:
        # No command line, installation values, stderr or exception text escapes.
        print(json.dumps(failed, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(_installed_route_main())
