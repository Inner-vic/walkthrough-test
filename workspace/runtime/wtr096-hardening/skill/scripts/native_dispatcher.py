"""Task-service owner for native walkthrough requests; no automatic failed replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import native_lifecycle as lifecycle
import walkthrough_worker as worker


def launch_worker(path,settings_path,settings):
    """Each job owns a process: Lark HOME/profile activation is process-local."""
    try:
        with path.with_suffix('.log').open('ab',buffering=0) as log:
            result=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(worker.__file__).resolve()),
                'worker','--record',str(path),'--settings',str(settings_path)],
                cwd=settings['workspace_root'],env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',OPENBLAS_NUM_THREADS='1'),
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            code=result.returncode
    except OSError:code=1
    if code:
        try:
            with lifecycle.advisory_lock(path.with_suffix('.worker.oslock')):
                record=json.loads(path.read_text(encoding='utf-8'))
                lifecycle.validate_source(record);worker.validate_execution_release(settings,record)
                if record.get('status') not in ('completed','failed'):
                    archived=path.with_name(path.stem+'.launch-failed-'+str(time.time_ns())+'.json')
                    with archived.open('xb') as stream:stream.write(path.read_bytes())
                    record.update(status='failed',error='走查执行进程未正常启动，诊断已保留；请检查执行环境后恢复。',
                                  failed_at=datetime.now(timezone.utc).isoformat())
                    worker.policy.atomic_json(path,record)
        except (OSError,ValueError,KeyError,TypeError):pass
    return code


def pending(settings):
    result=[]
    for path in Path(settings['runtime_state_dir']).glob('walkthrough-*.json'):
        if re.fullmatch(r'walkthrough-[0-9a-f]{24}',path.stem) is None:continue
        try:
            record=json.loads(path.read_text(encoding='utf-8'))
            if (record.get('scheduler_managed') is not True or record.get('status')!='queued'
                    or record.get('execution_release')!=settings.get('execution_release')):continue
            lifecycle.validate_source(record);worker.validate_execution_release(settings,record)
            if record.get('execution_policy')!=settings.get('execution_policy'):continue
            result.append((str(record.get('created_at') or ''),path))
        except (OSError,ValueError,KeyError,TypeError):continue
    return [path for _,path in sorted(result)]


def recover_interrupted(settings):
    """Never replay a started dead worker. Retain an explicit failure instead."""
    runtime=Path(settings['runtime_state_dir'])
    for path in runtime.glob('walkthrough-*.json'):
        if re.fullmatch(r'walkthrough-[0-9a-f]{24}',path.stem) is None:continue
        try:
            record=json.loads(path.read_text(encoding='utf-8'))
            if (record.get('scheduler_managed') is not True or record.get('execution_release')!=settings['execution_release']
                    or record.get('status') in ('queued','completed','failed') or not record.get('worker_pid')):continue
            lifecycle.validate_source(record);worker.validate_execution_release(settings,record)
            with lifecycle.advisory_lock(path.with_suffix('.worker.oslock')):
                # The dispatcher owns the whole worker lifetime. If its task
                # lock is free, no native worker owns the record any longer.
                original=path.read_bytes()
                saved=path.with_name(path.stem+'.interrupted-'+str(time.time_ns())+'.json')
                with saved.open('xb') as stream:stream.write(original)
                record.update(status='failed',error='走查执行进程已中断，输入和诊断已保留；可恢复本任务。',
                              failed_at=datetime.now(timezone.utc).isoformat())
                worker.policy.atomic_json(path,record)
        except (OSError,ValueError,KeyError,TypeError):continue


def run(settings_path,*,idle_seconds=90):
    settings=worker.load_settings(settings_path);lifecycle.validate_policy(settings)
    runtime=Path(settings['runtime_state_dir']);runtime.mkdir(parents=True,exist_ok=True)
    with lifecycle.advisory_lock(runtime/'.dispatcher.oslock'):
        recover_interrupted(settings)
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='walkthrough-job') as executor:
            active={};idle=time.monotonic()
            while True:
                for path,future in list(active.items()):
                    if not future.done():continue
                    try:future.result()
                    except Exception:pass  # Exact task owns its finite failure.
                    del active[path];idle=time.monotonic()
                for path in pending(settings):
                    if len(active)>=2:break
                    if path in active:continue
                    active[path]=executor.submit(launch_worker,path,settings_path,settings);idle=time.monotonic()
                if not active and time.monotonic()-idle>=idle_seconds:return 0
                time.sleep(.5 if active else 2)
