"""Extract the official Git usr tool subtree into one new task directory.

No installer is executed. No links, user configuration or global environment
are created. This accepts only one SHA-pinned official distribution.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile

ARCHIVE_SHA256='4c05716ec90806b29d6392e6190e2277a6687688f889f0039d7ae3ca53ed433f'
ARCHIVE_URL='https://github.com/git-for-windows/git/releases/download/v2.52.0.windows.1/Git-2.52.0-64-bit.tar.bz2'

def sha(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as handle:
        for data in iter(lambda:handle.read(1024**2),b''):result.update(data)
    return result.hexdigest()

def plain(path):
    for item in (path,*path.parents):
        try:info=item.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Tool paths must not contain links or reparse points')

def prepare(archive,destination,workspace):
    if os.name!='nt':raise ValueError('This tool subtree is for Windows only')
    archive=Path(archive).absolute();destination=Path(destination).absolute();workspace=Path(workspace).absolute()
    for path in (archive,destination,workspace):
        if not path.is_absolute() or '..' in path.parts:raise ValueError('Explicit absolute paths are required')
        plain(path)
    workspace=workspace.resolve(strict=True)
    destination=destination.resolve()
    if (not workspace.is_dir() or destination.exists() or destination.parent==destination
            or not any(destination.is_relative_to(workspace/name) and destination!=workspace/name
                       for name in ('runtime','staging'))):
        raise ValueError('Choose one new directory under this workspace runtime or staging')
    if not archive.is_file() or sha(archive)!=ARCHIVE_SHA256:raise ValueError('Official Git archive SHA256 differs')
    files={};selected=[];seen=set()
    with tarfile.open(archive,'r:bz2') as handle:
        for member in handle.getmembers():
            if not (member.name=='usr' or member.name.startswith('usr/') or member.name=='LICENSE.txt'):continue
            relative=PurePosixPath(member.name)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in member.name
                    or ':' in member.name or not (member.isfile() or member.isdir())):
                raise ValueError('Unexpected tool subtree member; no links will be materialized')
            key=relative.as_posix().casefold()
            if key in seen:raise ValueError('Aliased or duplicate official tool member')
            seen.add(key);selected.append((member,relative))
        if not {'usr/bin/gawk.exe','usr/bin/grep.exe'}.issubset(seen):raise ValueError('Required tools absent')
        destination.mkdir(parents=True,exist_ok=False)
        for member,relative in selected:
            target=destination.joinpath(*relative.parts)
            if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with handle.extractfile(member) as source,target.open('xb') as output:shutil.copyfileobj(source,output)
            if target.stat().st_size!=member.size:raise ValueError('Tool member length differs')
            files[relative.as_posix()]={'sha256':sha(target),'bytes':target.stat().st_size}
    if sha(archive)!=ARCHIVE_SHA256:raise ValueError('Official archive changed during extraction')
    environment={**os.environ,'TZ':'CST-8','LC_ALL':'C','LANG':'C'}
    versions={}
    for name,prefix in (('gawk','GNU Awk 5.3.2'),('grep','grep (GNU grep) 3.0')):
        result=subprocess.run([str(destination/'usr/bin'/(name+'.exe')),'--version'],
            capture_output=True,text=True,encoding='utf-8',env=environment,timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        first=result.stdout.splitlines()[0] if result.stdout else ''
        if result.returncode or not (first==prefix or first.startswith(prefix+',')):
            raise ValueError('Extracted tool version differs')
        versions[name]=first
    receipt={'status':'windows_tools_prepared','archive_url':ARCHIVE_URL,'archive_sha256':ARCHIVE_SHA256,
        'subtree':'usr','excluded_links_outside_subtree':True,'files':files,'versions':versions,
        'global_environment_modified':False,'installer_executed':False}
    (destination/'TOOL-PROVENANCE.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return {'status':receipt['status'],'files':len(files),'versions':versions,
        'gawk_sha256':files['usr/bin/gawk.exe']['sha256'],'grep_sha256':files['usr/bin/grep.exe']['sha256'],
        'global_environment_modified':False,'installer_executed':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--workspace-root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(prepare(args.archive,args.destination,args.workspace_root)))
