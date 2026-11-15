"""Portable conversion for the approved analyzer's brake configuration bytes.

The external source stays unchanged. Only a process-local wrapper around its
approved AWK builder is installed. The existing brake bit tests and malformed
input coercion are retained; clean hexadecimal tokens no longer depend on an
AWK implementation's optional conversion of dynamic ``0x`` strings.
"""
from __future__ import annotations

import ast
import configparser
import concurrent.futures
from datetime import datetime, timezone, timedelta
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import secrets
import stat
import textwrap
import threading

from analyzer_contract import ast_digest, validate_analyzer_contract, validate_contract_audit

POLICY = 'portable-brake-hex-spawn-v2'
BUILDER_AST_SHA256 = '4faac54b8db890f32c8ef1fc68e25d8bce087cb532dcbad6c920a6741fdb6894'
_MARKER = '_walkthrough_portable_brake_hex_spawn_v2'
_HELPER_NAME = '_walkthrough_hex_decimal_v1'
_CHILD = None

ORIGINAL_CLASSIFIER = '''        function classify_brake(b7, b10,   n1, n2, b1, b2, bit3_1, bit2_0){
            n1 = "0x" b7; n2 = "0x" b10
            b1 = n1+0; b2 = n2+0
            bit3_1 = int(b1/2) % 8
            bit2_0 = b2 % 8
            if (bit3_1 == 1 && bit2_0 == 0) return "自研EMB"
            if (bit3_1 == 1 && bit2_0 == 1) return "博世EMB"
            return "其他制动"
        }
'''

PATCHED_CLASSIFIER = '''        function _walkthrough_hex_decimal_v1(s,   i, n, digit){
            s = tolower(s)
            n = 0
            for (i = 1; i <= length(s); i++) {
                digit = index("0123456789abcdef", substr(s, i, 1)) - 1
                n = n * 16 + digit
            }
            return n
        }
        function classify_brake(b7, b10,   n1, n2, b1, b2, bit3_1, bit2_0){
            n1 = "0x" b7; n2 = "0x" b10
            b1 = n1+0; b2 = n2+0
            if (b7 ~ /^[0-9A-Fa-f]+$/ && b10 ~ /^[0-9A-Fa-f]+$/) {
                b1 = _walkthrough_hex_decimal_v1(b7)
                b2 = _walkthrough_hex_decimal_v1(b10)
            }
            bit3_1 = int(b1/2) % 8
            bit2_0 = b2 % 8
            if (bit3_1 == 1 && bit2_0 == 0) return "自研EMB"
            if (bit3_1 == 1 && bit2_0 == 1) return "博世EMB"
            return "其他制动"
        }
'''


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def expected_execution_compat_audit():
    """Return the fixed, independently verifiable algorithm identity."""
    algorithm = {
        'policy': POLICY,
        'builder_ast_sha256': BUILDER_AST_SHA256,
        'original_classifier_sha256': _sha(ORIGINAL_CLASSIFIER),
        'patched_classifier_sha256': _sha(PATCHED_CLASSIFIER),
        'clean_hex_only': True,
        'malformed_coercion_preserved': True,
        'brake_bit_tests_preserved': True,
        'external_source_modified': False,
        'spawn_initializer_required': True,
        'actual_mawk_consumption_required': True,
        'module_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    algorithm['algorithm_fingerprint'] = _sha(json.dumps(
        algorithm, sort_keys=True, ensure_ascii=False, separators=(',', ':')))
    return algorithm


def validate_execution_compat_audit(audit):
    """Validate identity only; unsigned receipts are not runtime attestation."""
    expected = expected_execution_compat_audit()
    if not isinstance(audit, dict) or set(audit) != set(expected):
        raise ValueError('原生十六进制兼容审计缺失或字段不完整。')
    if any(type(audit[key]) is not type(value) or audit[key] != value
           for key, value in expected.items()):
        raise ValueError('原生十六进制兼容策略、算法摘要或原函数契约不一致。')
    return dict(audit)


def patch_awk_program(program):
    """Replace exactly one approved classifier, leaving all other bytes intact."""
    if (not isinstance(program, str)
            or program.count(ORIGINAL_CLASSIFIER) != 1
            or program.count('function classify_brake(') != 1
            or _HELPER_NAME in program):
        raise ValueError('原生 AWK 制动分类结构已变化，拒绝猜测或重复安装兼容逻辑。')
    return program.replace(ORIGINAL_CLASSIFIER, PATCHED_CLASSIFIER, 1)


def _approved_builder(perf_utils):
    original = getattr(perf_utils, '_build_awk_program', None)
    if not inspect.isfunction(original) or getattr(original, _MARKER, False):
        raise ValueError('原生 AWK 生成器缺失或已安装兼容逻辑。')
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(original)))
    except (OSError, TypeError, SyntaxError, ValueError):
        raise ValueError('无法核验原生 AWK 生成器源码。') from None
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef)
            or tree.body[0].name != '_build_awk_program'
            or ast_digest(tree) != BUILDER_AST_SHA256):
        raise ValueError('原生 AWK 生成器完整 AST 已变化，须先适配和真实回归。')
    # This has no parser, subprocess, file or network side effects in the
    # approved builder. It proves the generated fragment before installation.
    patch_awk_program(original('', 2026))

    return original


def _json_bytes(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode('utf-8')


def _object_sha(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _plain(path, *, directory=False):
    path=Path(path)
    info=path.lstat()
    if (path.is_symlink() or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('兼容审计只接受普通文件和目录。')
    return path


def _write_record(path,value):
    path=Path(path)
    if path.exists():_plain(path)
    temporary=path.with_suffix('.tmp')
    with temporary.open('xb') as stream:stream.write(_json_bytes(value))
    temporary.replace(path)


def _read_record(path):
    return json.loads(_plain(path).read_text(encoding='utf-8'))


def _epochs(request):
    if not isinstance(request,str) or not re.fullmatch(r'\d{14}/\d{14}',request):
        raise ValueError('兼容审计时间窗无效。')
    values=[datetime.strptime(x,'%Y%m%d%H%M%S').replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            for x in request.split('/')]
    if values[0]>=values[1]:raise ValueError('兼容审计时间窗顺序无效。')
    return values


def _observe_ini(session):
    path=_plain(Path(session['source_root'])/'general_performance/o.ini')
    raw=path.read_bytes()
    cfg=configparser.ConfigParser();cfg.read_string(raw.decode('utf-8-sig'))
    request=cfg.get('PARAMETER','time',fallback='').strip()
    log_root=cfg.get('PARAMETER','logPath',fallback='').strip()
    if request!=session['request_time_range'] or not log_root or Path(log_root).resolve()!=Path(session['expected_log_root']).resolve():
        raise ValueError('实际AWK子进程INI时间窗或日志目录不符。')
    return {'time_range':request,'log_root':str(Path(log_root).resolve()),
            'sha256':hashlib.sha256(raw).hexdigest()}


def _publish_child(state):
    _write_record(state['path'],state['record'])


def _child_error(state,kind):
    state['record']['errors'].append(kind)
    _publish_child(state)


class _SubprocessProxy:
    """Only this perf_utils module sees the proxy; subprocess is not changed."""
    def __init__(self,original,state):self.original,self.state=original,state
    def __getattr__(self,name):return getattr(self.original,name)
    def run(self,*args,**kwargs):
        command=args[0] if args else kwargs.get('args')
        if not isinstance(command,(tuple,list)) or not command or str(command[0])!='mawk':
            return self.original.run(*args,**kwargs)
        state=self.state
        try:
            pending=[row for row in state['record']['calls'] if row['consumption'] is None]
            if len(command)<3 or command[1]!='-f' or len(pending)!=1:
                raise ValueError('实际mawk调用与唯一生成程序不对应。')
            row=pending[0]
            program=_plain(command[2])
            raw=program.read_bytes();digest=hashlib.sha256(raw).hexdigest()
            if digest!=row['generation']['patched_program_sha256']:
                raise ValueError('实际mawk消费的程序不是已修改的程序。')
            actual_ini=_observe_ini(state['session'])
            if actual_ini!=state['record']['initial_ini']:
                raise ValueError('AWK调用期间实际INI已变化。')
            row['consumption']={'program_sha256':digest,'program_bytes':len(raw),
                'input_path_count':len(command)-3,'input_paths_sha256':_object_sha(list(map(str,command[3:]))),
                'actual_ini':actual_ini,'returncode':None,'program_after_sha256':None}
            _publish_child(state)
            result=self.original.run(*args,**kwargs)
            after=hashlib.sha256(_plain(program).read_bytes()).hexdigest()
            row['consumption'].update(returncode=result.returncode,program_after_sha256=after)
            _publish_child(state)
            if result.returncode or after!=digest:
                _child_error(state,'actual_mawk_failed_or_program_changed')
            return result
        except BaseException as exc:
            _child_error(state,'actual_mawk_consumption_'+type(exc).__name__)
            raise


def _initialize_spawn_compat(trace_dir,nonce):
    """Top-level importable initializer used by the original spawn pool."""
    global _CHILD
    if _CHILD is not None:raise ValueError('子进程兼容初始化不能重复。')
    trace=_plain(trace_dir,directory=True)
    session=_read_record(trace/'session.json')
    validate_execution_compat_audit(session['installation'])
    if session['nonce']!=nonce or session['parent_pid']!=os.getppid():
        raise ValueError('AWK子进程不属于本次父进程/nonce。')
    from common import perf_utils
    source=Path(perf_utils.__file__).resolve().parents[1]
    if source!=Path(session['source_root']).resolve():raise ValueError('子进程导入了其它分析器。')
    contract=validate_analyzer_contract(source)
    if contract!=session['analyzer_contract']:raise ValueError('子进程实际源码契约不符。')
    original=_approved_builder(perf_utils)
    record={'schema_version':1,'policy':POLICY,'nonce':nonce,
        'parent_pid':os.getppid(),'pid':os.getpid(),
        'request_time_range':session['request_time_range'],
        'analyzer_contract':contract,'initial_ini':_observe_ini(session),
        'calls':[],'errors':[]}
    path=trace/('worker-'+str(os.getpid())+'.json')
    if path.exists():raise ValueError('兼容子进程审计文件已存在。')
    state={'path':path,'record':record,'session':session}
    _publish_child(state)
    @functools.wraps(original)
    def wrapped(context,nye):
        try:
            before=original(context,nye);after=patch_awk_program(before)
            if before.replace(ORIGINAL_CLASSIFIER,'',1)!=after.replace(PATCHED_CLASSIFIER,'',1):
                raise ValueError('制动分类以外的AWK内容变化。')
            record['calls'].append({'generation':{
                'original_program_sha256':_sha(before),'patched_program_sha256':_sha(after),
                'context_sha256':_sha(context),'year':nye,'outside_classifier_bytes_unchanged':True},
                'consumption':None})
            _publish_child(state)
            return after
        except BaseException as exc:
            _child_error(state,'program_generation_'+type(exc).__name__)
            raise
    setattr(wrapped,_MARKER,True)
    perf_utils._build_awk_program=wrapped
    perf_utils.subprocess=_SubprocessProxy(perf_utils.subprocess,state)
    _CHILD=state


def install_execution_compat(perf_utils,perf_csv=None,*,trace_dir=None,request_time_range=None):
    """Guard original spawn construction and initialize actual parser children."""
    _approved_builder(perf_utils)
    epochs=_epochs(request_time_range)
    if perf_csv is None or trace_dir is None:raise ValueError('必须提供原生进程池和独立运行审计目录。')
    original_pool=getattr(perf_csv,'ProcessPoolExecutor',None)
    if original_pool is not concurrent.futures.ProcessPoolExecutor:
        raise ValueError('原生进程池边界已变化或已安装。')
    if getattr(perf_csv,'txt_process_tool',None) is not getattr(perf_utils,'txt_process_tool',None):
        raise ValueError('原始txt_process_tool提交边界已变化。')
    source=Path(perf_utils.__file__).resolve().parents[1]
    contract=validate_analyzer_contract(source)
    trace=Path(trace_dir)
    if trace.resolve().parent!=source.parent.resolve() or trace.name!='execution-compat-trace':
        raise ValueError('兼容运行审计必须位于本次隔离任务目录。')
    _plain(trace.parent,directory=True)
    if trace.exists():raise ValueError('兼容运行审计目录必须为新建。')
    trace.mkdir()
    session={'schema_version':1,'policy':POLICY,'nonce':secrets.token_hex(16),
        'parent_pid':os.getpid(),'request_time_range':request_time_range,'epoch_range':epochs,
        'source_root':str(source),'expected_log_root':str(source.parent/'input/perf'),
        'analyzer_contract':contract,'installation':expected_execution_compat_audit(),
        'pools':[],'submissions':[],'errors':[]}
    _write_record(trace/'session.json',session)
    lock=threading.RLock()
    signature=inspect.signature(original_pool)
    def guarded_pool(*args,**kwargs):
        bound=signature.bind_partial(*args,**kwargs);bound.apply_defaults()
        values=bound.arguments
        context=values.get('mp_context')
        if context is None or context.get_start_method()!='spawn':
            raise ValueError('原生进程池必须保留明确spawn上下文。')
        if values.get('initializer') is not None or values.get('initargs') not in ((),None):
            raise ValueError('原生进程池已有initializer，不能覆盖。')
        requested=values.get('max_workers')
        values['initializer']=_initialize_spawn_compat
        values['initargs']=(str(trace.resolve()),session['nonce'])
        executor=original_pool(*bound.args,**bound.kwargs)
        with lock:
            session['pools'].append({'start_method':executor._mp_context.get_start_method(),
                'requested_max_workers':requested,'actual_max_workers':executor._max_workers})
            _write_record(trace/'session.json',session)
        original_submit=executor.submit
        def submit(function,*task_args,**task_kwargs):
            try:
                if (function is not perf_utils.txt_process_tool or task_kwargs or len(task_args)!=5
                        or list(task_args[4])!=epochs):
                    raise ValueError('实际子进程任务函数、参数或时间窗不符。')
                paths=task_args[2]
                if not isinstance(paths,(str,tuple,list)):raise ValueError('原生mawk输入路径结构变化。')
                with lock:
                    session['submissions'].append({'function':'common.perf_utils.txt_process_tool',
                        'theme':str(task_args[3]),'epoch_range':list(task_args[4]),
                        'input_path_count':len(paths),'expected_mawk_call':bool(paths)})
                    _write_record(trace/'session.json',session)
                return original_submit(function,*task_args,**task_kwargs)
            except BaseException as exc:
                with lock:
                    session['errors'].append('pool_submit_'+type(exc).__name__)
                    _write_record(trace/'session.json',session)
                raise
        executor.submit=submit
        return executor
    setattr(guarded_pool,_MARKER,True)
    perf_csv.ProcessPoolExecutor=guarded_pool
    return expected_execution_compat_audit()


def validate_execution_compat_runtime_audit(audit,*,request_time_range,contract_fingerprint):
    """Check captured child consumption; does not authenticate unsigned JSON."""
    try:
        if not isinstance(audit,dict) or set(audit)!={'schema_version','policy','session','session_sha256','workers','total_mawk_calls'}:
            raise ValueError('运行审计结构无效。')
        if type(audit['schema_version']) is not int or audit['schema_version']!=1 or audit['policy']!=POLICY:
            raise ValueError('运行审计版本过期。')
        session=audit['session'];validate_execution_compat_audit(session['installation'])
        validate_contract_audit(session['analyzer_contract'])
        if (session['schema_version']!=1 or session['policy']!=POLICY
                or session['request_time_range']!=request_time_range or session['epoch_range']!=_epochs(request_time_range)
                or session['analyzer_contract']['contract_fingerprint']!=contract_fingerprint
                or not re.fullmatch('[0-9a-f]{32}',session['nonce'])
                or type(session['parent_pid']) is not int or session['parent_pid']<=0
                or session['errors'] or audit['session_sha256']!=_object_sha(session)):
            raise ValueError('运行审计时间窗、nonce、源码或父进程绑定无效。')
        if not session['pools'] or not session['submissions']:raise ValueError('未实际提交原生子进程任务。')
        for pool in session['pools']:
            if pool['start_method']!='spawn' or type(pool['actual_max_workers']) is not int or pool['actual_max_workers']<=0:
                raise ValueError('原生spawn/并发约束变化。')
            requested=pool['requested_max_workers']
            if requested is not None and (type(requested) is not int or requested!=pool['actual_max_workers']):
                raise ValueError('原生max_workers被改变。')
        expected_calls=0
        for task in session['submissions']:
            if (task['function']!='common.perf_utils.txt_process_tool' or task['epoch_range']!=session['epoch_range']
                    or type(task['input_path_count']) is not int or task['input_path_count']<0
                    or type(task['expected_mawk_call']) is not bool
                    or task['expected_mawk_call']!=bool(task['input_path_count'])):
                raise ValueError('原始实际任务/窗口/输入绑定无效。')
            expected_calls+=int(task['expected_mawk_call'])
        workers=audit['workers']
        if not isinstance(workers,list) or not workers or len(workers)>128:raise ValueError('缺少实际子进程记录。')
        pids=set();total=0
        for wrapped in workers:
            record=wrapped['record']
            if wrapped['sha256']!=_object_sha(record):raise ValueError('实际子进程记录SHA不一致。')
            validate_contract_audit(record['analyzer_contract'])
            if (record['schema_version']!=1 or record['policy']!=POLICY
                    or record['nonce']!=session['nonce'] or record['parent_pid']!=session['parent_pid']
                    or record['request_time_range']!=request_time_range
                    or record['analyzer_contract']!=session['analyzer_contract']
                    or type(record['pid']) is not int or record['pid']<=0 or record['pid'] in pids
                    or record['pid']==session['parent_pid'] or record['errors']):
                raise ValueError('实际子进程身份、源码或错误闩锁无效。')
            pids.add(record['pid'])
            ini=record['initial_ini']
            if (ini['time_range']!=request_time_range or ini['log_root']!=session['expected_log_root']
                    or not re.fullmatch('[0-9a-f]{64}',ini['sha256'])):
                raise ValueError('实际子进程INI窗口不符。')
            for row in record['calls']:
                generation,consumption=row['generation'],row['consumption']
                if (generation['outside_classifier_bytes_unchanged'] is not True
                        or type(generation['year']) is not int or generation['year']!=int(request_time_range[:4])):
                    raise ValueError('生成程序修改范围或年份不符。')
                for key in ('original_program_sha256','patched_program_sha256','context_sha256'):
                    if not re.fullmatch('[0-9a-f]{64}',generation[key]):raise ValueError('生成程序摘要缺失。')
                if (not isinstance(consumption,dict) or consumption['program_sha256']!=generation['patched_program_sha256']
                        or consumption['program_after_sha256']!=consumption['program_sha256']
                        or consumption['actual_ini']!=ini or type(consumption['returncode']) is not int
                        or consumption['returncode']!=0 or type(consumption['program_bytes']) is not int
                        or consumption['program_bytes']<=0 or type(consumption['input_path_count']) is not int
                        or consumption['input_path_count']<=0
                        or not re.fullmatch('[0-9a-f]{64}',consumption['input_paths_sha256'])):
                    raise ValueError('缺少成功实际mawk消费，或消费程序/窗口被改变。')
                total+=1
        if (type(audit['total_mawk_calls']) is not int or not 0<total==audit['total_mawk_calls']==expected_calls):
            raise ValueError('实际mawk调用数与已提交任务不符。')
    except (KeyError,TypeError,AttributeError,IndexError) as exc:
        raise ValueError('原生子进程实际消费审计不完整。') from exc
    return audit


def finalize_execution_compat(trace_dir,*,request_time_range):
    """Read after the original native pool exits; every real call must succeed."""
    trace=_plain(trace_dir,directory=True)
    entries=list(trace.iterdir())
    if any(path.name!='session.json' and not re.fullmatch(r'worker-[0-9]+\.json',path.name) for path in entries):
        raise ValueError('子进程兼容审计有额外或未完成文件。')
    session=_read_record(trace/'session.json')
    records=[_read_record(path) for path in sorted(entries) if path.name!='session.json']
    audit={'schema_version':1,'policy':POLICY,'session':session,'session_sha256':_object_sha(session),
        'workers':[{'record':record,'sha256':_object_sha(record)} for record in records],
        'total_mawk_calls':sum(len(record['calls']) for record in records)}
    validate_execution_compat_runtime_audit(audit,request_time_range=request_time_range,
        contract_fingerprint=session['analyzer_contract']['contract_fingerprint'])
    return audit
