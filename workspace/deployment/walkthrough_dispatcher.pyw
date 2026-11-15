"""Hidden Windows task launcher; native jobs use the dedicated regular Python."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

WORKSPACE=Path(__file__).resolve().parents[1]


def main():
    installed=json.loads((WORKSPACE/'deployment/walkthrough-test.installation.json').read_text(encoding='utf-8-sig'))
    if installed.get('execution_enabled') is not True:return 0
    skill=Path(installed['skill_root']).resolve()
    if skill!=WORKSPACE/'runtime/wtr096-hardening/skill':raise ValueError('dispatcher release mismatch')
    python=WORKSPACE/'runtime/wtv081/Scripts/python.exe'
    if Path(sys.executable).resolve()!=python.with_name('pythonw.exe').resolve():raise ValueError('dispatcher Python mismatch')
    source=json.loads(Path(installed['source_manifest_path']).read_text(encoding='utf-8-sig'))
    if hashlib.sha256(Path(installed['settings_path']).read_bytes()).hexdigest()!=installed['settings_sha256']:
        raise ValueError('dispatcher settings mismatch')
    files=source['files']
    canonical=hashlib.sha256(json.dumps(files,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    if canonical!=installed['source_manifest_sha256']:raise ValueError('dispatcher source mismatch')
    for name,digest in files.items():
        path=skill/name
        if not path.resolve().is_relative_to(skill) or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('dispatcher source mismatch')
    runtime=Path(installed['runtime_state_dir'])
    state=runtime/'.dispatcher-state.json'
    def save(**fields):
        value={'schema':'walkthrough-dispatcher-state-v1','launcherPid':os.getpid(),**fields}
        temp=state.with_name(state.name+'.'+uuid4().hex+'.tmp')
        try:temp.write_text(json.dumps(value),encoding='utf-8');temp.replace(state)
        finally:temp.unlink(missing_ok=True)
    code='import sys;sys.path.insert(0,sys.argv[1]);import native_dispatcher;sys.exit(native_dispatcher.run(sys.argv[2]))'
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','OPENBLAS_NUM_THREADS':'1'}
    settings=json.loads(Path(installed['settings_path']).read_text(encoding='utf-8-sig'))
    allowed={f'WALKTHROUGH_{where}_{suffix}' for where in ('PROD','TEST') for suffix in ('ORIGIN','DOWNLOAD_HTTPS_HOSTS','DOWNLOAD_HTTP_HOSTS')}
    cloud=settings.get('cloud_environment')
    if not isinstance(cloud,dict) or set(cloud)-allowed or any(not isinstance(v,str) for v in cloud.values()):
        raise ValueError('dispatcher cloud binding mismatch')
    for key in allowed:env.pop(key,None)
    env.update(cloud)
    with (runtime/'.dispatcher.log').open('ab',buffering=0) as log:
        child=subprocess.Popen([str(python),'-B','-X','utf8','-c',code,str(skill/'scripts'),installed['settings_path']],
            cwd=WORKSPACE,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        began=datetime.now(timezone.utc).isoformat();save(stage='running',workerPid=child.pid,startedAt=began)
        status=child.wait();save(stage='exited',workerPid=child.pid,startedAt=began,exitCode=status)
        return status


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:sys.exit(1)
