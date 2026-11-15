"""Experimental Git-for-Windows tool adapter; not part of frozen delivery.

Only the existing native logical mawk/grep invocations are mapped. Full original
rules, native extractor, CSV calculations and statistics remain unchanged.
Actual child tool use records both native and translated program SHA-256.
"""
from __future__ import annotations
import base64,hashlib,json,math,os,subprocess,uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path

POLICY='windows-git-gawk-private-v3'
BRACKET_KEYS=('finish','pc ts','pc_size','denoise')
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def forward_join(*parts):return os.path.join(*map(str,parts)).replace('\\','/')
def windows_trace_dir(trace_dir):return Path(trace_dir).with_name('windows-tools-trace')
def statistics_trace_dir(trace_dir):return Path(trace_dir).with_name('windows-statistics-trace')

PARSING_SCHEDULER='windows-native-bounded-parsing-v1'

def parsing_worker_budget(cpu_count):
    """Reserve at most half of the machine across two concurrent windows."""
    if cpu_count is None:cpu_count=2
    if type(cpu_count) is not int or cpu_count<1:raise ValueError('invalid native CPU budget')
    return max(1,min(4,cpu_count//4))

def install_parsing_scheduler(perf_csv,trace_dir):
    # No rule/extractor/statistical function is replaced. The original caller
    # still requests, records and receives exactly this finite process count.
    cpu=os.cpu_count()
    record={'policy':PARSING_SCHEDULER,'cpu_count':cpu,'concurrent_windows':2,
        'max_workers_per_window':parsing_worker_budget(cpu),'requested_workers':[]}
    path=Path(trace_dir).with_name('windows-parsing-scheduler.json')
    if path.exists():raise ValueError('duplicate parsing scheduler')
    def persist():
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record,sort_keys=True),encoding='utf-8')
        temporary.replace(path)
    persist()
    def choose_workers():
        record['requested_workers'].append(record['max_workers_per_window'])
        persist()
        return record['max_workers_per_window']
    perf_csv.get_dynamic_max_processes=choose_workers

def observed_stream(value):
    # Native callers use both binary and text subprocess capture. Preserve the
    # exact binary stderr without making the JSON observer fail after success.
    if isinstance(value,bytes):
        return {'encoding':'base64','bytes':len(value),'sha256':hashlib.sha256(value).hexdigest(),
                'data':base64.b64encode(value).decode('ascii')}
    return value

def translate_program(program):
    # Gawk reserves `default`; the native declaration has one exact known shape.
    before='function default(e){';after='function native_default(e){'
    if program.count(before)!=1:raise ValueError('native default helper changed')
    # All default call sites are emitted by native _build_common_awk_cac.
    translated=program.replace(before,after).replace('var = default($','var = native_default($')
    changes=[]
    # Mawk preserves these nonstandard string escapes for a dynamic regex;
    # gawk removes the slash before compiling the regex. Make only the four
    # observed native key_next inputs explicit, preserving their literal '['.
    for key in BRACKET_KEYS:
        old='key_next("'+key+'\\[")';new='key_next("'+key+'\\\\[")'
        count=translated.count(old)
        if count>1:raise ValueError('native bracket input repeated unexpectedly')
        if count:
            translated=translated.replace(old,new);changes.append((old,new))
    restored=translated
    for old,new in reversed(changes):restored=restored.replace(new,old)
    if restored.replace(after,before).replace('var = native_default($','var = default($')!=program:
        raise ValueError('non-reversible awk helper translation')
    return translated

class WindowsTools:
    def __init__(self,original,trace_dir):
        self.original=original;self.trace=Path(trace_dir);self.trace.mkdir(exist_ok=True)
    def __getattr__(self,name):return getattr(self.original,name)
    def run(self,*args,**kwargs):
        command=args[0] if args else kwargs.get('args')
        if not isinstance(command,(tuple,list)) or not command or command[0] not in ('mawk','grep'):
            return self.original.run(*args,**kwargs)
        logical=command[0];git=Path(os.environ['WALKTHROUGH_WINDOWS_GIT_BIN'])
        actual=git/('gawk.exe' if logical=='mawk' else 'grep.exe')
        if not actual.is_file():raise ValueError('configured Git tool absent')
        token=str(os.getpid())+'-'+uuid.uuid4().hex
        record={'policy':POLICY,'pid':os.getpid(),'parent_pid':os.getppid(),
            'logical_tool':logical,'executable':str(actual),'executable_sha256':sha(actual),
            'original_argv':list(map(str,command)),'returncode':None,'errors':[]}
        argv=list(map(str,command));argv[0]=str(actual)
        try:
            if logical=='mawk':
                if len(argv)<3 or argv[1]!='-f':raise ValueError('unexpected native awk shape')
                source=Path(argv[2]);raw=source.read_text(encoding='utf-8')
                translated=translate_program(raw)
                target=self.trace/(token+'.gawk');target.write_text(translated,encoding='utf-8',newline='\n')
                argv[2]=str(target)
                record.update(original_program_sha256=sha(source),actual_program_sha256=sha(target),
                    translated_program=str(target),
                    only_default_symbol_renamed=not any('key_next("'+key+'\\[")' in raw for key in BRACKET_KEYS),
                    explicit_dynamic_regex_bracket_inputs=[key for key in BRACKET_KEYS if 'key_next("'+key+'\\[")' in raw],
                    reversible_translation_verified=True)
            env=dict(kwargs.get('env') or os.environ)
            # MSYS zoneinfo is not guaranteed installed. Asia/Shanghai silently
            # becomes UTC in the installed Git build; fixed POSIX +08 is proved.
            env.update(TZ='CST-8',LC_ALL='C',LANG='C')
            kwargs['env']=env
            record.update(actual_argv=argv,timezone=env['TZ'],locale=env['LC_ALL'])
            if args:args=(argv,*args[1:])
            else:kwargs['args']=argv
            result=self.original.run(*args,**kwargs)
            record.update(returncode=result.returncode,stderr=observed_stream(result.stderr))
            if logical=='mawk' and result.returncode:raise RuntimeError('Windows native gawk failed')
            return result
        except BaseException as exc:
            record['errors'].append(type(exc).__name__)
            raise
        finally:
            (self.trace/(token+'.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')

class _WindowsOs:
    """Module-local absence adapter; never mutates the global os module."""
    def __init__(self,original,*,log_only=False):self.original,self.log_only=original,log_only
    def __getattr__(self,name):return getattr(self.original,name)
    def getloadavg(self):
        if self.log_only:return (float('nan'),)*3  # diagnostic prints load1m=nan
        # Invoke the native existing failure fallback (2 processes), rather than
        # treating Windows' missing AttributeError as an estimated Unix load.
        raise OSError('Windows does not provide Unix load average')

class _WindowsTemporaryFiles:
    """Keep the approved generated AWK's LF bytes identical when persisted."""
    def __init__(self,original):self.original=original
    def __getattr__(self,name):return getattr(self.original,name)
    def NamedTemporaryFile(self,*args,**kwargs):
        if args==('w',) and kwargs=={'suffix':'.awk','delete':False,'encoding':'utf-8'}:
            kwargs={**kwargs,'newline':'\n'}
        return self.original.NamedTemporaryFile(*args,**kwargs)

class _WindowsDiagnosticTime:
    """Windows CRT rejects negative epochs which Linux formats for diagnostics."""
    def __init__(self,original,record,path):self.original,self.record,self.path=original,record,path
    def __getattr__(self,name):return getattr(self.original,name)
    def localtime(self,value=None):
        try:return self.original.localtime() if value is None else self.original.localtime(value)
        except OSError:
            if value is None or not math.isfinite(float(value)) or float(value)>=0:raise
            moment=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(seconds=float(value))
            result=moment.astimezone(timezone(timedelta(hours=8))).timetuple()
            self.record['negative_epoch_diagnostic_fallbacks'].append({'epoch':float(value),
                'formatted':self.original.strftime('%Y-%m-%d %H:%M:%S',result)})
            self.path.write_text(json.dumps(self.record,ensure_ascii=False,indent=2),encoding='utf-8')
            return result

def initialize_windows_statistics_child(trace_dir,nonce):
    from common import perf_plot
    from analyzer_contract import validate_analyzer_contract
    session=json.loads((Path(trace_dir)/'session.json').read_text(encoding='utf-8'))
    source=Path(perf_plot.__file__).resolve().parents[1]
    if (session['nonce']!=nonce or session['parent_pid']!=os.getppid()
            or source!=Path(session['source_root']).resolve()
            or validate_analyzer_contract(source)!=session['analyzer_contract']):
        raise ValueError('Windows statistics child source/session mismatch')
    path=statistics_trace_dir(trace_dir)/('worker-'+str(os.getpid())+'.json')
    record={'policy':POLICY,'pid':os.getpid(),'parent_pid':os.getppid(),'nonce':nonce,
        'source_root':str(source),'perf_plot_sha256':sha(perf_plot.__file__),
        'negative_epoch_diagnostic_fallbacks':[]}
    if path.exists():raise ValueError('duplicate Windows statistics child')
    path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    perf_plot.time=_WindowsDiagnosticTime(perf_plot.time,record,path)

def initialize_windows_child(trace_dir,nonce):
    # Spawn imports native_execution_compat fresh: its initializer here is the
    # original approved one, including source/INI/nonce checks and hex fix.
    import native_execution_compat
    native_execution_compat._initialize_spawn_compat(trace_dir,nonce)
    from common import perf_utils
    perf_utils.join=forward_join
    perf_utils.tempfile=_WindowsTemporaryFiles(perf_utils.tempfile)
    proxy=perf_utils.subprocess
    if type(proxy).__name__!='_SubprocessProxy':raise ValueError('missing native consumption guard')
    proxy.original=WindowsTools(proxy.original,windows_trace_dir(trace_dir))

def install_windows_native(perf_utils,trace_dir):
    if os.name!='nt':raise ValueError('Windows research adapter on unexpected host')
    import native_execution_compat
    from common import perf_csv,perf_plot
    perf_utils.os=_WindowsOs(perf_utils.os)
    perf_csv.os=_WindowsOs(perf_csv.os,log_only=True)
    install_parsing_scheduler(perf_csv,trace_dir)
    native_execution_compat._initialize_spawn_compat=initialize_windows_child
    perf_utils.subprocess=WindowsTools(perf_utils.subprocess,windows_trace_dir(trace_dir))
    statistics_trace_dir(trace_dir).mkdir(exist_ok=False)
    session=json.loads((Path(trace_dir)/'session.json').read_text(encoding='utf-8'))
    original_pool=perf_plot.Pool
    def statistics_pool(*args,**kwargs):
        if kwargs.get('initializer') is not None or kwargs.get('initargs'):
            raise ValueError('native statistics pool already has initializer')
        return original_pool(*args,**{**kwargs,'initializer':initialize_windows_statistics_child,
            'initargs':(str(Path(trace_dir).resolve()),session['nonce'])})
    perf_plot.Pool=statistics_pool

def finalize_windows_native(trace_dir,*,require_statistics=False):
    records=[json.loads(p.read_text(encoding='utf-8')) for p in sorted(windows_trace_dir(trace_dir).glob('*.json'))]
    awk=[r for r in records if r['logical_tool']=='mawk']
    if not awk or any(r['errors'] or r['returncode']!=0 for r in awk):
        raise ValueError('Windows native tool actual consumption missing or failed')
    for row in awk:
        if sha(row['translated_program'])!=row['actual_program_sha256']:raise ValueError('translated program changed')
    statistics=[json.loads(p.read_text(encoding='utf-8')) for p in sorted(statistics_trace_dir(trace_dir).glob('worker-*.json'))]
    if require_statistics and not statistics:raise ValueError('Windows statistics spawn consumption missing')
    scheduling=json.loads(Path(trace_dir).with_name('windows-parsing-scheduler.json').read_text(encoding='utf-8'))
    from native_windows_host import validate_parsing_scheduler
    session=json.loads((Path(trace_dir)/'session.json').read_text(encoding='utf-8'))
    validate_parsing_scheduler(scheduling,session.get('pools'))
    return {'policy':POLICY,'actual_gawk_calls':len(awk),'actual_grep_calls':len(records)-len(awk),
        'records':records,'statistics_workers':statistics,'requires_independent_windows_parity':True,
        'unix_load_average_supported':False,'parsing_scheduler':scheduling,
        'legacy_hex_receipt_scope':'native program before reversible gawk syntax adaptation'}
