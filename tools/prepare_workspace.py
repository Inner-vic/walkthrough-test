"""Prepare an absent Windows workspace with locked local wheels; never activate or authorize."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from check_repository import ROOT, verify


def plain_parents(path):
    for parent in (path, *path.parents):
        if parent.exists():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked paths are not permitted')


def run(argv):
    subprocess.run(list(map(str, argv)), check=True, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def prepare(workspace, base_python, wheelhouse):
    if os.name != 'nt':
        raise ValueError('Windows x64 required')
    verify()
    if not workspace.is_absolute() or '..' in workspace.parts or workspace.exists():
        raise ValueError('Use an absent absolute workspace')
    workspace = workspace.absolute()
    for value in (workspace, base_python, wheelhouse):
        plain_parents(value)
    if workspace.is_relative_to(ROOT) or ROOT.is_relative_to(workspace):
        raise ValueError('Repository and deployment workspace must be separate')
    if not wheelhouse.is_dir():
        raise ValueError('Local wheelhouse required')
    run([base_python, '-I', '-c', 'import sys,struct;assert sys.implementation.name=="cpython";'
         'assert sys.version_info[:2]==(3,11);assert struct.calcsize("P")==8'])
    shutil.copytree(ROOT / 'workspace', workspace)
    python = workspace / 'runtime/wtv081/Scripts/python.exe'
    run([base_python, '-I', '-m', 'venv', python.parents[1]])
    release = workspace / 'runtime/wtr096-hardening'
    run([python, '-I', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--no-index',
         '--find-links', wheelhouse, '--require-hashes', '-r', release / 'skill/requirements-windows-native.lock'])
    run([python, '-I', '-m', 'pip', 'check'])
    private = workspace / 'private'
    private.mkdir()
    config = json.loads((ROOT / 'examples/settings.windows.example.json').read_text('utf-8'))
    config.update(workspace_root=str(workspace), runtime_state_dir=str(workspace / 'data/walkthrough-test-windows081/runtime'),
                  cloud_settings_path=str(private / 'walkthrough-cloud.settings.json'),
                  lark_cli=str(workspace / 'tools/lark-cli/lark-cli.exe'), lark_home=str(private / 'project-lark-home'),
                  auth_credential_replica_path=str(private / 'project-lark-home/.lark-cli/credential-replica.json'))
    config['local_analysis'].update(python=str(python), analyzer_root=str(workspace / 'runtime/approved-full-analyzer'),
                                    windows_git_bin=str(workspace / 'runtime/git-tools/usr/bin'))
    config['report_delivery']['native_template_bridge_manifest'] = str(workspace / 'data/walkthrough-template-bridge/ready.json')
    def save(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    save(private / 'walkthrough.settings.json.example', config)
    save(private / 'walkthrough-cloud.settings.json.example', {'username': 'REPLACE_APPROVED_USERNAME'})
    isolation = json.loads((ROOT / 'examples/isolation.example.json').read_text('utf-8'))
    for name in ('workspace_root', 'project_id', 'lark_home', 'lark_cli', 'auth_credential_replica_path', 'expected_lark_app_id'):
        isolation[name] = config[name]
    isolation.update(settings_path=str(private / 'walkthrough.settings.json'), python_executable=str(python),
                     expected_dispatch_open_id=config['expected_dispatch_open_id'])
    isolation['profiles'] = {'dispatch_user': config['lark_profile'], 'report_bot': config['report_lark_profile']}
    save(workspace / 'deployment/cc-connect-lark-isolation.json.example', isolation)
    source = json.loads((release / 'SOURCE-MANIFEST.json').read_text('utf-8'))
    installation = {'schema_version': 2, 'version': '0.8.1-windows-integration-target.16',
        'execution_release': 'walkthrough-target16-hardening', 'workspace_root': str(workspace),
        'skill_root': str(release / 'skill'), 'host_python': str(python),
        'entrypoint': str(workspace / 'deployment/walkthrough_entry.py'),
        'settings_path': str(private / 'walkthrough.settings.json'), 'runtime_state_dir': config['runtime_state_dir'],
        'execution_enabled': False, 'execution': 'windows', 'backend': 'native_full',
        'cloud_environment_policy': 'prod_then_test', 'cloud_environment_selection_scope': 'per_window',
        'legacy': {}, 'retained_releases': [], 'source_manifest_path': str(release / 'SOURCE-MANIFEST.json'),
        'source_manifest_sha256': source['canonical_source_sha256'], 'settings_sha256': 'REPLACE_FINAL_SETTINGS_SHA256',
        'deployment_status': 'candidate_requires_configuration_and_acceptance', 'online_first_request_verified': False}
    save(workspace / 'deployment/walkthrough-test.installation.json', installation)
    Path(config['runtime_state_dir']).mkdir(parents=True)
    run([python, '-B', '-X', 'utf8', ROOT / 'tools/offline_selfcheck.py', '--scripts', release / 'skill/scripts'])
    return {'status': 'candidate_prepared_disabled', 'dependencies_installed': True,
            'business_jobs_created': 0, 'authorization_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--base-python', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.workspace, args.base_python, args.wheelhouse)))
    except Exception as exc:
        print(json.dumps({'status': 'not_activated', 'error_type': type(exc).__name__,
                          'instruction': 'Retain any partial candidate for diagnosis; existing deployments were not modified.'}))
        sys.exit(1)
