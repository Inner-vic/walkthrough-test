"""First-time profile initialization; secrets are read with getpass, never argv."""
import argparse,getpass,json,os,subprocess
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',type=Path,required=True);a=p.parse_args()
 s=json.loads(a.settings.read_text('utf-8-sig'));home=Path(s['lark_home']).expanduser()
 if not home.is_absolute() or home.resolve()==Path.home().resolve():raise ValueError('Dedicated absolute home required')
 for item in (home,*home.parents):
  if item.exists() and (item.is_symlink() or getattr(item.lstat(),'st_file_attributes',0)&0x400):raise ValueError('Linked home refused')
 home=home.resolve()
 config=home/'.lark-cli/config.json'
 if config.exists():raise ValueError('Existing configuration refused; use controlled maintenance')
 profiles=(s['lark_profile'],s['report_lark_profile'])
 if profiles[0]==profiles[1] or any(not x.startswith(s['project_id']+'-') for x in profiles):raise ValueError('Profile namespace mismatch')
 home.mkdir(parents=True,exist_ok=True)
 env=dict(os.environ,HOME=str(home),USERPROFILE=str(home),LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
 secret=getpass.getpass('Approved Feishu App Secret (hidden): ')
 if not secret:raise ValueError('Empty secret')
 for profile,mode in zip(profiles,('user','bot')):
  r=subprocess.run([s['lark_cli'],'config','init','--app-id',s['expected_lark_app_id'],'--app-secret-stdin','--name',profile],input=secret+'\n',text=True,encoding='utf-8',env=env,capture_output=True)
  if r.returncode:raise RuntimeError('Profile initialization failed; inspect local CLI configuration privately')
  r=subprocess.run([s['lark_cli'],'config','strict-mode',mode,'--profile',profile],env=env,capture_output=True)
  if r.returncode:raise RuntimeError('Strict-mode configuration failed; do not authorize or start workers')
 print(json.dumps({'status':'profiles_initialized','oauth_started':False,'business_jobs_created':0}))

if __name__=='__main__':main()
