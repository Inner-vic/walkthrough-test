"""Synthetic bounded cleanup tests, including a POSIX long-path fixture."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_cleanup as cleanup
import native_log_source as native
from request_model import InputError


class NativeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = {'local_analysis':{'execution':'linux','keep_raw_logs':False}}

    def populate(self):
        for name in ('input','native-prepared','analysis'):
            folder = self.root/name
            folder.mkdir()
            (folder/'synthetic.log').write_text('synthetic',encoding='utf-8')
        for name in ('native-result.json','report-data.json','Base-full.xlsx'):
            (self.root/name).write_text('saved result',encoding='utf-8')

    def test_only_fixed_children_removed_and_results_preserved(self):
        self.populate()
        native.cleanup_inputs(self.root,self.settings)
        self.assertEqual({p.name for p in self.root.iterdir()},
                         {'native-result.json','report-data.json','Base-full.xlsx'})
        native.cleanup_inputs(self.root,self.settings)

    def test_keep_raw_does_not_call_cleanup_or_subprocess(self):
        self.populate()
        settings = {'local_analysis':{'keep_raw_logs':True}}
        with (patch.object(cleanup,'remove_target',side_effect=AssertionError('deleted')),
              patch.object(native.subprocess,'run',side_effect=AssertionError('spawned'))):
            native.cleanup_inputs(self.root,settings)
        self.assertTrue((self.root/'analysis'/'synthetic.log').is_file())

    def test_relative_and_drive_root_are_rejected(self):
        for root in ('relative/window',self.root.anchor):
            with self.subTest(root=root), self.assertRaises(ValueError):
                cleanup.remove_target(root,'analysis')

    def test_parent_traversal_root_is_rejected(self):
        with self.assertRaises(ValueError):
            cleanup.remove_target(self.root/'..'/'other-window','analysis')

    def test_missing_or_file_root_is_rejected(self):
        marker = self.root/'not-a-directory'
        marker.write_text('preserve',encoding='utf-8')
        for path in (self.root/'absent',marker):
            with self.subTest(path=path), self.assertRaises((ValueError,OSError)):
                cleanup.remove_target(path,'analysis')
        self.assertEqual(marker.read_text(encoding='utf-8'),'preserve')

    def test_target_outside_fixed_names_is_rejected(self):
        for name in ('../outside','.',str(self.root),'native-result.json','analysis/child'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                cleanup.remove_target(self.root,name)

    def test_file_target_is_rejected_without_deletion(self):
        marker = self.root/'analysis'
        marker.write_text('preserve',encoding='utf-8')
        with self.assertRaises(ValueError):cleanup.remove_target(self.root,'analysis')
        self.assertEqual(marker.read_text(encoding='utf-8'),'preserve')

    def test_target_symlink_and_junction_attributes_are_rejected(self):
        target = self.root/'analysis';target.mkdir()
        real_lstat = os.lstat
        for attributes,mode in ((0,stat.S_IFLNK),(0x400,stat.S_IFDIR)):
            def lstat(path, *args, **kwargs):
                if str(path)==cleanup._io_path(target):
                    return SimpleNamespace(st_mode=mode,st_file_attributes=attributes)
                return real_lstat(path,*args,**kwargs)
            with self.subTest(attributes=attributes), patch.object(cleanup.os,'lstat',side_effect=lstat):
                with self.assertRaisesRegex(ValueError,'链接|联接'):
                    cleanup.remove_target(self.root,'analysis')
        self.assertTrue(target.is_dir())

    def test_ancestor_link_is_rejected_before_deletion(self):
        target = self.root/'analysis';target.mkdir()
        real_lstat = os.lstat
        def lstat(path, *args, **kwargs):
            if str(path)==cleanup._io_path(self.root):
                return SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0)
            return real_lstat(path,*args,**kwargs)
        with patch.object(cleanup.os,'lstat',side_effect=lstat):
            with self.assertRaisesRegex(ValueError,'链接'):cleanup.remove_target(self.root,'analysis')
        self.assertTrue(target.is_dir())

    def test_resolved_target_escape_is_rejected(self):
        real_resolved = cleanup._resolved
        def resolved(path, *, strict):
            if Path(path).name=='analysis':return self.root.parent/'outside'
            return real_resolved(path,strict=strict)
        with patch.object(cleanup,'_resolved',side_effect=resolved):
            with self.assertRaisesRegex(ValueError,'超出'):cleanup.remove_target(self.root,'analysis')

    def test_nested_symlink_is_rejected_before_any_deletion(self):
        self.populate()
        child=self.root/'analysis'/'synthetic.log'
        real_lstat=os.lstat
        def lstat(path,*args,**kwargs):
            if str(path)==cleanup._io_path(child):
                return SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=0)
            return real_lstat(path,*args,**kwargs)
        with patch.object(cleanup.os,'lstat',side_effect=lstat):
            with self.assertRaisesRegex(ValueError,'链接'):cleanup.remove_target(self.root,'analysis')
        self.assertTrue(child.is_file())

    def test_wsl_uses_configured_runtime_and_argument_list(self):
        self.populate()
        settings={'local_analysis':{'execution':'wsl','distribution':'Synthetic-Distro',
                  'python':'/opt/analyzer venv/bin/python','wsl_executable':'synthetic-wsl','keep_raw_logs':False}}
        expected_root=native.wsl_path(self.root)
        def run(command,**kwargs):
            self.assertEqual(command,['synthetic-wsl','-d','Synthetic-Distro','--exec',
                '/opt/analyzer venv/bin/python',native.wsl_path(native.HERE/'native_cleanup.py'),'--root',expected_root])
            self.assertNotIn('shell',kwargs)
            cleanup.remove_target(self.root,'analysis')
            return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'cleaned',
                'target':expected_root+'/analysis','removed':True}))
        with patch.object(native.subprocess,'run',side_effect=run) as invoke:
            native.cleanup_inputs(self.root,settings)
        self.assertEqual(invoke.call_count,1)
        self.assertTrue((self.root/'native-result.json').is_file())
        self.assertFalse((self.root/'input').exists())

    def test_wsl_failure_preserves_results_and_host_inputs_for_resume(self):
        self.populate()
        settings={'local_analysis':{'execution':'wsl','distribution':'Synthetic','python':'/venv/python'}}
        with patch.object(native.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='{}')):
            with self.assertRaisesRegex(InputError,'恢复交付'):native.cleanup_inputs(self.root,settings)
        self.assertTrue((self.root/'input').is_dir())
        self.assertTrue((self.root/'report-data.json').is_file())

    def test_wsl_response_cannot_claim_another_target(self):
        self.populate()
        settings={'local_analysis':{'execution':'wsl','distribution':'Synthetic','python':'/venv/python'}}
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'status':'cleaned','target':'/other/analysis'}))
        with patch.object(native.subprocess,'run',return_value=response):
            with self.assertRaises(InputError):native.cleanup_inputs(self.root,settings)
        self.assertTrue((self.root/'analysis').is_dir())

    def test_windows_long_path_cleanup(self):
        if os.name!='nt':self.skipTest('Windows extended-length paths only')
        target=self.root/'input'
        deep=target/('segment-'+'x'*80)/('segment-'+'y'*80)/('segment-'+'z'*80)
        os.makedirs(cleanup._io_path(deep))
        with open(cleanup._io_path(deep/'long.log'),'w',encoding='utf-8') as handle:handle.write('synthetic')
        self.assertGreater(len(str(deep/'long.log')),260)
        self.assertTrue(cleanup.remove_target(self.root,'input'))
        self.assertFalse(target.exists())

    def test_posix_cli_long_path_and_colon_filename(self):
        if os.name=='nt':self.skipTest('Run this case under Linux/WSL')
        analysis=self.root/'analysis'
        deep=analysis/('x'*100)/('y'*100)/('z'*100)
        deep.mkdir(parents=True)
        (deep/'backtrace:synthetic.zip').write_text('synthetic',encoding='utf-8')
        marker=self.root/'report-data.json';marker.write_text('saved',encoding='utf-8')
        done=subprocess.run([sys.executable,str(Path(cleanup.__file__).resolve()),'--root',str(self.root)],
                            capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertEqual(json.loads(done.stdout)['target'],str(analysis))
        self.assertFalse(analysis.exists())
        self.assertEqual(marker.read_text(encoding='utf-8'),'saved')


if __name__=='__main__':
    unittest.main()
