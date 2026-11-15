"""Native walkthrough queue ownership, bounded capacity and task-service wakeup."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from request_model import InputError

VERSION = 'walkthrough-lifecycle-v1'
TASK = 'CC Connect Walkthrough Dispatcher'


def validate_source(record):
    from walkthrough_route import request_identity
    identity=request_identity(record.get('context_header'))
    request=record.get('request') or {}
    expected='walkthrough-'+hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
    material=json.dumps([identity,request['baseline_metadata'],request['replacement_metadata']],sort_keys=True)
    if (record.get('job_key')!=expected or record.get('request_sha256')!=hashlib.sha256(material.encode()).hexdigest()
            or any(record.get(k)!=v for k,v in identity.items())
            or any(request[role+'_metadata'].get('vin')!=record.get('vin') for role in ('baseline','replacement'))):
        raise InputError('走查任务来源或请求绑定无法核验。')


def enabled(settings):
    return settings.get('execution_policy', {}).get('version') == VERSION


def validate_policy(settings):
    policy = settings.get('execution_policy')
    if policy != {'version':VERSION,'heavy_jobs':1,'publication_workers':2,'parallel_window_discovery':True}:
        raise InputError('走查调度或并行策略不匹配，未开始执行。')
    return policy


@contextmanager
def advisory_lock(path, *, timeout=0, waiting=None):
    path=Path(path)
    if path.is_symlink():raise InputError('任务锁路径无效。')
    with path.open('a+b') as stream:
        stream.seek(0,os.SEEK_END)
        if stream.tell()==0:stream.write(b'0');stream.flush()
        deadline=time.monotonic()+timeout
        while True:
            stream.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic()>=deadline:raise InputError('任务仍由另一执行进程持有，请稍后查看。') from None
                if waiting:waiting()
                time.sleep(.25)
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def dispatcher_manifest(settings):
    validate_policy(settings)
    workspace=Path(settings['workspace_root']).resolve()
    path=workspace/'deployment/walkthrough-dispatcher.installation.json'
    try:
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        script=workspace/'deployment/walkthrough_dispatcher.pyw'
        if (data['schema']!='walkthrough-dispatcher-installation-v1' or data['taskName']!=TASK
                or Path(data['workspace']).resolve()!=workspace
                or Path(data['python']).resolve()!=workspace/'runtime/wtv081/Scripts/python.exe'
                or Path(data['actionExecutable']).resolve()!=workspace/'runtime/wtv081/Scripts/pythonw.exe'
                or data['dispatcherSha256']!=hashlib.sha256(script.read_bytes()).hexdigest()
                or data['actionArguments']!=f'-B "{script}"'):
            raise ValueError()
        return data
    except (OSError,ValueError,KeyError,TypeError):
        raise InputError('走查独立调度服务配置未通过校验，未创建任务。') from None


def task_service(settings, *, wake=False):
    data=dispatcher_manifest(settings)
    program=("$d=ConvertFrom-Json([Console]::In.ReadToEnd());"
        "$t=Get-ScheduledTask -TaskName $d.taskName -ErrorAction Stop;"
        "if(@($t.Actions).Count -ne 1 -or $t.Actions[0].Execute -ne $d.actionExecutable "
        "-or $t.Actions[0].Arguments -ne $d.actionArguments -or $t.State -eq 'Disabled'){exit 2};"
        +("Start-ScheduledTask -TaskName $d.taskName -ErrorAction Stop;" if wake else "")+
        "[Console]::Out.Write('{\"verified\":true}')")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',program],
            input=json.dumps(data),capture_output=True,text=True,encoding='utf-8',timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode or json.loads(result.stdout).get('verified') is not True:raise ValueError()
    except (OSError,ValueError,subprocess.SubprocessError):
        raise InputError('走查独立调度服务尚未就绪，请检查执行服务。') from None
    return {'verified':True,'started':wake,'method':'scheduled_task'}


def enqueue_resume(settings,path,save):
    path=Path(path)
    with advisory_lock(path.with_suffix('.worker.oslock')):
        raw=path.read_bytes();record=json.loads(raw)
        validate_source(record)
        if record.get('execution_policy')!=settings.get('execution_policy'):
            raise InputError('原任务调度策略无法验证，不自动接管。')
        if record.get('status') in ('completed','queued'):
            return {'status':record['status'],'job_key':record['job_key'],'record_path':str(path),'idempotent':True}
        archived=path.with_name(path.stem+'.attempt-'+str(time.time_ns())+'.json')
        with archived.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        record.update(status='queued',recovery_requested=True)
        record.pop('error',None);record.pop('failed_at',None)
        save(path,record)
    try:service=task_service(settings,wake=True)
    except InputError:service={'started':False,'method':'scheduled_task'}
    return {'status':'queued','job_key':record['job_key'],'record_path':str(path),'worker':service,
            'message':'已保留原执行证据，恢复任务已排队。'}


@contextmanager
def capacity(settings,path,record,save):
    if not enabled(settings):yield;return
    validate_policy(settings)
    queued=False
    def waiting():
        nonlocal queued
        if not queued:
            record['status']='waiting_capacity';record['execution_stage']='waiting_capacity';save(path,record);queued=True
    with advisory_lock(Path(settings['runtime_state_dir'])/'.full-analysis.slot.lock',timeout=8*3600,waiting=waiting):
        record['status']='analyzing';save(path,record)
        yield
