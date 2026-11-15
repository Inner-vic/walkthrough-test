"""Run an explicit project CLI profile; bootstrap never overwrites a replica."""
import argparse,json,os,subprocess,sys
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--workspace',type=Path,required=True)
 p.add_argument('--manifest',type=Path,required=True)
 p.add_argument('--profile-kind',choices=('user','bot'),default='user')
 p.add_argument('--bootstrap',action='store_true',help='First installation only, while business workers are stopped')
 p.add_argument('--seed-replica',action='store_true')
 p.add_argument('command',nargs=argparse.REMAINDER)
 a=p.parse_args();workspace=a.workspace.resolve();sys.path.insert(0,str(workspace/'deployment'))
 from feishu_auth_maintenance import load_configuration,lark_environment,load_worker
 from lark_auth_lock import lark_auth_lock
 from lark_auth_store import seed_replica
 manifest,settings=load_configuration(a.manifest)
 if Path(manifest['workspace_root']).resolve()!=workspace:raise ValueError('workspace mismatch')
 replica=Path(settings['auth_credential_replica_path'])
 args=a.command[1:] if a.command[:1]==['--'] else a.command
 if a.bootstrap:
  if replica.exists() or replica.with_suffix(replica.suffix+'.pending').exists():raise ValueError('Existing credential state: use project OAuth recovery; bootstrap refused')
  lock_settings={k:v for k,v in settings.items() if k!='auth_credential_replica_path'}
 else:lock_settings=settings
 if a.seed_replica:
  if not a.bootstrap or args or a.profile_kind!='user':raise ValueError('Seed requires explicit bootstrap and no CLI command')
  worker=load_worker(workspace)
  with lark_auth_lock(lock_settings):
   worker.ensure_user_authorization(lock_settings,settings['expected_dispatch_open_id'],force_verify=True)
   worker.ensure_report_bot_authorization(lock_settings)
   worker.ensure_secure_label_authorization(lock_settings)
   result=seed_replica(settings)
  print(json.dumps({'status':'replica_initialized','created':result.get('created',True)}));return 0
 if not args or any(x=='--profile' or x.startswith('--profile=') for x in args):raise ValueError('Provide command without overriding profile')
 if args[:2]==['auth','login'] and (not a.bootstrap or a.profile_kind!='user'):raise ValueError('Login allowed only for first user bootstrap; existing deployments use feishu_project_oauth.py')
 profile=settings['lark_profile' if a.profile_kind=='user' else 'report_lark_profile']
 with lark_auth_lock(lock_settings):
  return subprocess.run([settings['lark_cli'],*args,'--profile',profile],cwd=workspace,env=lark_environment(settings)).returncode

if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as error:
  print(json.dumps({'status':'failed','error_type':type(error).__name__,'message':'Project CLI configuration or binding failed; inspect private configuration locally.'}));raise SystemExit(1)
