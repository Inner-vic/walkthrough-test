import contextlib
import io
import json
import unittest
from unittest.mock import patch

import native_analysis_failure as failures
import run_m100


class NativeAnalysisFailureTests(unittest.TestCase):
    def test_finite_stage_message_never_forwards_exception(self):
        for stage in failures.MESSAGES:
            value = {'status':'failed','failure':failures.failure(stage),
                     'error':'secret signed URL or private path'}
            self.assertEqual(failures.describe(value),failures.MESSAGES[stage])
        self.assertIsNone(failures.describe({'status':'failed','failure':{
            **failures.failure('input_validation'),'message':'untrusted exception'}}))

    def test_namespace_cli_failure_returns_typed_json_and_private_stderr(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        argv = ['run_m100.py','--analyzer-root','.','--log-dir','.',
                '--start','20261102172900','--end','20261102180400',
                '--source-identity','namespace_v3']
        with patch('sys.argv',argv), patch.object(run_m100,'preflight',side_effect=ValueError('private-detail')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                code = run_m100.main()
        self.assertEqual(code,1)
        value = json.loads(output.getvalue())
        self.assertEqual(failures.describe(value),failures.MESSAGES['input_validation'])
        self.assertNotIn('private-detail',output.getvalue())
        self.assertIn('private-detail',diagnostic.getvalue())

    def test_namespace_execution_failure_identifies_execution_stage(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        argv = ['run_m100.py','--analyzer-root','.','--log-dir','.',
                '--start','20261102172900','--end','20261102180400',
                '--source-identity','namespace_v3','--execute','--work-root','.']
        with patch('sys.argv',argv), patch.object(run_m100,'preflight',return_value=({},[])), \
                patch.object(run_m100,'execute',side_effect=RuntimeError('private-execution')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                self.assertEqual(run_m100.main(),1)
        self.assertEqual(json.loads(output.getvalue())['failure'],failures.failure('native_execution'))


if __name__ == '__main__':
    unittest.main()
