"""Offline service ownership, capacity, recovery and business-feedback checks."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import native_lifecycle as life
import native_feedback as feedback
import native_dispatcher as dispatcher
import environment_selection as selection
import walkthrough_worker as worker
from request_model import InputError

POLICY={'version':life.VERSION,'heavy_jobs':1,'publication_workers':2,'parallel_window_discovery':True}
HEADER='[cc-connect request_context_v=1 sender_id=ou_offline platform=feishu chat_id=oc_offline message_id=om_offline thread_id=]'
VIN='DEM0VEH1CLE000001'
WINDOWS={role:{'vin':VIN,'start':f'2026-10-{day}T15:00:00','end':f'2026-10-{day}T15:35:00'} for role,day in [('baseline','21'),('replacement','22')]}


def save(path,value):
    temp=Path(path).with_suffix('.unit.tmp');temp.write_text(json.dumps(value),encoding='utf-8');temp.replace(path)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.settings={'runtime_state_dir':str(self.root),'execution_policy':deepcopy(POLICY),'execution_release':'offline-release'}

    def record(self):
        from walkthrough_route import request_identity
        identity=request_identity(HEADER)
        material=json.dumps([identity,WINDOWS['baseline'],WINDOWS['replacement']],sort_keys=True)
        key='walkthrough-'+hashlib.sha256(identity['source_message_id'].encode()).hexdigest()[:24]
        return {'job_key':key,'kind':'walkthrough_test_local','status':'queued','context_header':HEADER,
            'request':{r+'_metadata':v for r,v in WINDOWS.items()},'vin':VIN,**identity,
            'request_sha256':hashlib.sha256(material.encode()).hexdigest(),'scheduler_managed':True,
            'execution_release':'offline-release','execution_policy':deepcopy(POLICY),'created_at':'2026-11-03T00:00:00Z'}

    def test_competing_owner_and_stable_lock_inode(self):
        path=self.root/'lock'
        with life.advisory_lock(path):
            with self.assertRaises(InputError):
                with life.advisory_lock(path):pass
        before=path.stat().st_ino
        with life.advisory_lock(path):pass
        self.assertEqual(path.stat().st_ino,before)

    def test_process_death_releases_worker_lock(self):
        path=self.root/'worker.oslock';ready=self.root/'ready'
        code='import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import native_lifecycle as n\nwith n.advisory_lock(Path(sys.argv[2])):\n Path(sys.argv[3]).write_text("ready");time.sleep(30)'
        child=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-B','-X','utf8','-c',code,str(Path(life.__file__).parent),str(path),str(ready)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            end=time.monotonic()+8
            while not ready.exists() and child.poll() is None and time.monotonic()<end:time.sleep(.05)
            self.assertTrue(ready.exists())
            child.kill();child.wait(timeout=5)
            with life.advisory_lock(path):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)

    def test_heavy_capacity_serializes_jobs_and_records_waiting(self):
        active=0;peak=0;guard=threading.Lock();waiting=threading.Event();release=threading.Event()
        def run(number):
            nonlocal active,peak
            record={};path=self.root/(str(number)+'.json')
            def persist(p,r):
                if r.get('status')=='waiting_capacity':waiting.set()
            with life.capacity(self.settings,path,record,persist):
                with guard:active+=1;peak=max(peak,active)
                if number==1:release.wait(2)
                with guard:active-=1
        one=threading.Thread(target=run,args=(1,));one.start();time.sleep(.05)
        two=threading.Thread(target=run,args=(2,));two.start()
        self.assertTrue(waiting.wait(2));release.set();one.join(3);two.join(3)
        self.assertEqual(peak,1)

    def test_source_mutation_is_rejected(self):
        record=self.record();life.validate_source(record)
        record['sender_id']='ou_other'
        with self.assertRaises(InputError):life.validate_source(record)

    def test_pending_requires_own_version_source_and_managed_marker(self):
        record=self.record();path=self.root/(record['job_key']+'.json');save(path,record)
        with patch.object(dispatcher.worker,'validate_execution_release'):
            self.assertEqual(dispatcher.pending(self.settings),[path])
            for change in [{'scheduler_managed':False},{'execution_release':'old'},{'sender_id':'ou_other'},{'status':'failed'}]:
                save(path,{**record,**change});self.assertEqual(dispatcher.pending(self.settings),[])

    def test_started_interruption_is_archived_not_replayed(self):
        record=self.record();record.update(status='analyzing',worker_pid=1234567)
        path=self.root/(record['job_key']+'.json');save(path,record);before=path.read_bytes()
        with patch.object(dispatcher.worker,'validate_execution_release'):
            dispatcher.recover_interrupted(self.settings)
        self.assertEqual(json.loads(path.read_text())['status'],'failed')
        archived=list(self.root.glob('*.interrupted-*.json'));self.assertEqual(len(archived),1)
        self.assertEqual(archived[0].read_bytes(),before)
        with patch.object(dispatcher.worker,'validate_execution_release'):
            self.assertEqual(dispatcher.pending(self.settings),[])

    def test_live_owner_is_not_marked_interrupted(self):
        record=self.record();record.update(status='analyzing',worker_pid=os.getpid())
        path=self.root/(record['job_key']+'.json');save(path,record);before=path.read_bytes()
        with life.advisory_lock(path.with_suffix('.worker.oslock')),patch.object(dispatcher.worker,'validate_execution_release'):
            dispatcher.recover_interrupted(self.settings)
        self.assertEqual(path.read_bytes(),before)

    def test_resume_archives_failure_and_wakes_only_after_queue_is_persisted(self):
        record=self.record();record.update(status='failed',error='old evidence')
        path=self.root/(record['job_key']+'.json');save(path,record);before=path.read_bytes()
        def wake(settings,**kwargs):
            self.assertEqual(json.loads(path.read_text())['status'],'queued');return {'started':True}
        with patch.object(life,'task_service',side_effect=wake):result=life.enqueue_resume(self.settings,path,save)
        self.assertEqual(result['status'],'queued')
        self.assertEqual(next(self.root.glob('*.attempt-*.json')).read_bytes(),before)

    def test_feedback_coalesces_and_stops_before_delivery(self):
        record=self.record();record.update(status='analyzing',analysis_stage='processing_windows')
        path=self.root/(record['job_key']+'.json');save(path,record)
        sent=threading.Event()
        def reply(*args,**kwargs):sent.set();return {'ok':True,'identity':'bot','data':{'message_id':'om_feedback'}}
        policy=SimpleNamespace(atomic_json=save,run_lark=Mock(side_effect=reply))
        with feedback.monitor(path,{'report_lark_profile':'offline-bot'},policy,initial_delay=.01,interval=.02) as close:
            self.assertTrue(sent.wait(1));time.sleep(.08);close()
            record['status']='delivering';save(path,record)
        self.assertEqual(policy.run_lark.call_count,1)
        self.assertNotIn('--reply-in-thread',policy.run_lark.call_args.args[1])

    def test_pending_feedback_is_not_repeated(self):
        record=self.record();path=self.root/(record['job_key']+'.json');save(path,record)
        save(path.with_suffix('.feedback.json'),{'pending':True})
        policy=SimpleNamespace(atomic_json=save,run_lark=Mock())
        with feedback.monitor(path,{},policy,initial_delay=.01,interval=.02):time.sleep(.06)
        policy.run_lark.assert_not_called()

    def test_queue_and_report_stage_business_messages(self):
        self.assertIn('等待',feedback.business_status({'status':'waiting_capacity','analysis_task_stage':'processing_windows'}))
        self.assertIn('权限',feedback.business_status({'status':'analyzing','execution_stage':'create_report'}))
        self.assertEqual(feedback.business_status({'status':'failed','error':'来源映射失败'}),'来源映射失败')

    def test_admission_persists_before_wake_and_never_launches_frontend_worker(self):
        self.settings['workspace_root']=str(self.root)
        self.settings.pop('execution_release')
        request={'vin':VIN,**{role+'_metadata':meta for role,meta in WINDOWS.items()}}
        args=SimpleNamespace(ccconnect_context=HEADER,settings=self.root/'settings.json',text='offline input',received_at='2026-11-03T00:00:00Z',interpretation=None)
        def service(settings,*,wake=False):
            if wake:
                paths=list(self.root.glob('walkthrough-*.json'));self.assertEqual(len(paths),1)
                self.assertTrue(json.loads(paths[0].read_text())['scheduler_managed'])
            return {'started':wake,'verified':True}
        with patch.object(worker,'load_settings',return_value=self.settings),patch.object(worker,'source_normalization_anchor',return_value=args.received_at),patch.object(worker,'route',return_value={'status':'ready','normalized':request}),patch.object(worker.lifecycle,'task_service',side_effect=service),patch.object(worker.policy,'activate_lark_environment'),patch.object(worker.policy,'ensure_report_bot_authorization'),patch.object(worker.policy,'ensure_user_authorization',return_value={'open_id':'ou_operator'}),patch.object(worker.policy,'ensure_secure_label_authorization'),patch.object(worker.policy,'resolve_report_secure_label',return_value={'id':'offline'}),patch.object(worker.subprocess,'Popen') as popen:
            result=worker.start_job(args)
        self.assertEqual(result['status'],'queued');popen.assert_not_called()

    def test_completed_source_is_not_reauthorized_or_rewritten(self):
        record=self.record();record['status']='completed';path=self.root/(record['job_key']+'.json');save(path,record);before=path.read_bytes()
        with patch.object(worker.policy,'ensure_user_authorization') as auth:
            self.assertEqual(worker._run_owned(path,self.settings),0)
        auth.assert_not_called();self.assertEqual(path.read_bytes(),before)

    def test_service_launches_separate_job_process_without_profile_activation(self):
        self.settings['workspace_root']=str(self.root)
        record=self.record();path=self.root/(record['job_key']+'.json');save(path,record)
        before=dict(os.environ)
        with patch.object(dispatcher.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run,patch.object(worker,'run_worker') as inline:
            self.assertEqual(dispatcher.launch_worker(path,self.root/'settings.json',self.settings),0)
        inline.assert_not_called();self.assertEqual(dict(os.environ),before)
        command=run.call_args.args[0]
        self.assertIn('--record',command);self.assertIn('--settings',command)
        self.assertEqual(run.call_args.kwargs['env'].get('USERPROFILE'),before.get('USERPROFILE'))


class ParallelDiscoveryTests(unittest.TestCase):
    def test_both_windows_overlap_and_proofs_exist_before_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);barrier=threading.Barrier(2);roles=[]
            settings={'cloud_settings_path':'offline','local_analysis':{'environment':'prod_then_test'},'execution_policy':POLICY}
            def discover(meta,cfg,user,*,role,on_attempt,on_query):
                barrier.wait(timeout=2);roles.append(role)
                on_attempt([{'counts':{'x':1}}]);return {'provider_environment':'testtwo','request':meta},{'ad':{},'pcm':[]}
            with patch.object(selection.ad.cloud,'load_settings',return_value={'username':'offline'}),patch.object(selection,'_discover_window',side_effect=discover),patch.object(selection,'validate_task_decision',side_effect=lambda value,*_:value):
                decision,_=selection.select_task(WINDOWS,settings,root)
            self.assertCountEqual(roles,['baseline','replacement'])
            self.assertEqual(set(decision['windows']),set(roles))
            self.assertTrue((root/'task-environment-selection.json').is_file())

    def test_one_query_failure_drains_peer_and_does_not_publish_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);barrier=threading.Barrier(2);drained=threading.Event()
            def discover(meta,cfg,user,*,role,**kwargs):
                barrier.wait(timeout=2)
                if role=='baseline':raise InputError('offline query failure')
                time.sleep(.05);drained.set();return {},{}
            settings={'cloud_settings_path':'offline','local_analysis':{'environment':'prod_then_test'},'execution_policy':POLICY}
            with patch.object(selection.ad.cloud,'load_settings',return_value={'username':'offline'}),patch.object(selection,'_discover_window',side_effect=discover),self.assertRaises(InputError):
                selection.select_task(WINDOWS,settings,root)
            self.assertTrue(drained.is_set());self.assertFalse((root/'task-environment-selection.json').exists())


if __name__=='__main__':unittest.main()
