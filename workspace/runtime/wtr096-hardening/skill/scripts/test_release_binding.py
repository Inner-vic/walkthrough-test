"""Release mismatch must stop before any task lock, auth or record write."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import walkthrough_worker as worker
from request_model import InputError


class ReleaseBindingTests(unittest.TestCase):
    def test_missing_or_changed_release_binding_leaves_record_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {'runtime_state_dir': str(root), 'execution_release': 'test-release',
                        'local_analysis': {'execution': 'windows'}}
            record = {'kind': 'walkthrough_test_local', 'job_key': 'walkthrough-offline',
                      'status': 'failed', 'execution_release': 'test-release',
                      'execution_binding_sha256': 'b' * 64,
                      'analysis_profile': worker.analysis_profile(settings['local_analysis'])}
            path = root / 'walkthrough-offline.json'
            cases = [('execution_release', None), ('execution_release', 'other-release'),
                     ('execution_binding_sha256', None), ('execution_binding_sha256', 'c' * 64),
                     ('analysis_profile', 'm100-full-native-audited-v9-windows-native')]
            for key, value in cases:
                changed = deepcopy(record)
                changed[key] = value
                path.write_text(json.dumps(changed), encoding='utf-8')
                original = path.read_bytes()
                with self.subTest(key=key, value=value), \
                        patch.object(worker, 'load_settings', return_value=settings), \
                        patch.object(worker, 'execution_binding', return_value='b' * 64), \
                        patch.object(worker.policy, 'ensure_user_authorization') as auth, \
                        patch.object(worker.os, 'open', side_effect=AssertionError('no lock permitted')):
                    with self.assertRaises(InputError):
                        worker.run_worker(path, 'unused')
                    auth.assert_not_called()
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.with_suffix('.worker.lock').exists())


if __name__ == '__main__':
    unittest.main()
