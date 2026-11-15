"""Download failures must survive task boundaries without publication or secrets."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cloud_log_client as cloud
import cloud_pipeline as pipeline
import walkthrough_worker as worker
import window_pipeline as window
from test_task_environment_selection import per_window_task_fixture

SECRET = 'SYNTHETIC-DOWNLOAD-TOKEN-NOT-FOR-PERSISTENCE'
META = {'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:00:00','end':'2026-10-21T15:01:00'}


class DownloadFailurePipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def failure(self):
        error = cloud.CloudDownloadError(reason='http_bad_request', environment='prod',
            http_status=400, upstream_code='DOWNLOAD_FILE_FAILURE')
        error.args = ('https://private.invalid/?authorization=' + SECRET,)
        error.payload = {'message':SECRET}
        error.__cause__ = RuntimeError(SECRET)
        return error

    def test_worker_persists_finite_failure_and_does_not_publish(self):
        path = self.root/'synthetic-task.json'
        path.write_text(json.dumps({'kind':'walkthrough_test_local','status':'queued',
            'report_operator_open_id':'synthetic-user','analysis_profile':'synthetic',
            'vin':META['vin'],'request_sha256':'synthetic'}), encoding='utf-8')
        settings = {'runtime_state_dir':str(self.root),'local_analysis':{}}
        error = self.failure()
        with patch.object(worker,'load_settings',return_value=settings), \
                patch.object(worker.policy,'activate_lark_environment'), \
                patch.object(worker.policy,'ensure_user_authorization'), \
                patch.object(worker,'analysis_profile',return_value='synthetic'), \
                patch.object(worker,'check_native_environment',return_value={'status':'synthetic'}), \
                patch.object(worker,'analyze_task',side_effect=error), \
                patch.object(worker,'publish') as publish:
            with self.assertRaises(cloud.CloudDownloadError):
                worker.run_worker(path,'unused')
        publish.assert_not_called()
        persisted = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(persisted['status'],'failed')
        self.assertEqual(persisted['error_diagnostic'],cloud.download_failure_diagnostic(error))
        self.assertIn('HTTP 400',persisted['error'])
        self.assertNotIn(SECRET,path.read_text(encoding='utf-8'))
        self.assertNotIn('https://',path.read_text(encoding='utf-8'))
        self.assertFalse(path.with_suffix('.result.json').exists())
        self.assertFalse(path.with_suffix('.worker.lock').exists())

    def test_parallel_generic_failure_does_not_hide_precise_download_failure(self):
        rules = self.root/'rules.json'
        rules.write_text('{}',encoding='utf-8')
        requests = {'baseline':META,'replacement':{**META,'start':'2026-10-22T15:00:00','end':'2026-10-22T15:01:00'}}
        error = self.failure()
        def process(metadata,settings,directory,rules,**kwargs):
            if Path(directory).name=='baseline':
                raise RuntimeError(SECRET)
            raise error
        with patch.object(pipeline,'validate_config',return_value={'window_workers':2}), \
                patch.object(pipeline.selection,'select_task',return_value=(
                    per_window_task_fixture({role:'prod' for role in requests},requests=requests),None)), \
                patch.object(pipeline,'process_window',side_effect=process), \
                patch.object(pipeline,'as_completed',side_effect=lambda futures:iter(futures)):
            with self.assertRaises(cloud.CloudDownloadError) as caught:
                pipeline.run_windows(requests,{'local_analysis':{'environment':'test_then_prod'}},
                    self.root/'pipeline',rules)
        self.assertIs(caught.exception,error)
        path = self.root/'pipeline/pipeline-manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['windows']['replacement']['error_diagnostic'],
            cloud.download_failure_diagnostic(error))
        self.assertEqual(manifest['windows']['replacement']['status'],'failed')
        self.assertNotIn(SECRET,path.read_text(encoding='utf-8'))

    def test_optional_pcm_download_remains_missing_and_retains_finite_reason(self):
        error = self.failure()
        with patch.object(window.selection,'select_window',return_value=(
                {'environment':'prod'},{'ad':{},'pcm':[],'pcm_error':False})), \
                patch.object(window.ad,'cached_result',return_value={'status':'ok'}), \
                patch.object(window.pcm,'cached_result',return_value=None), \
                patch.object(window.ad,'fetch_window',return_value={'counts':{}}), \
                patch.object(window.pcm,'prepare',side_effect=error):
            prepared = window.prepare_window(META,{'local_analysis':{}},self.root,self.root/'rules')
        self.assertEqual(prepared.pcm_error['status'],'failed')
        self.assertEqual(prepared.pcm_error['metrics'],{})
        self.assertEqual(prepared.pcm_error['error_diagnostic'],cloud.download_failure_diagnostic(error))
        self.assertEqual(prepared.sources['pcm']['status'],'failed')
        self.assertEqual(prepared.sources['pcm']['error_diagnostic'],cloud.download_failure_diagnostic(error))
        self.assertEqual(prepared.environment,'prod')
        self.assertNotIn(SECRET,json.dumps(prepared.pcm_error))


if __name__=='__main__':
    unittest.main()
