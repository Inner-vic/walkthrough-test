"""Worker-facing diagnostics must not stringify preflight exceptions."""
import json
import unittest

import walkthrough_worker as worker
from preflight_diagnostics import PreflightError, preflight_failure_fields


class PreflightFailureMappingTests(unittest.TestCase):
    def test_worker_forwards_finite_preflight_fields_without_exception_text(self):
        secret = 'SYNTHETIC-AUTH-TOKEN-DO-NOT-EXPOSE'
        error = PreflightError('product_identity','user_token_refresh_required')
        error.args = ('https://private.invalid/?access_token='+secret,)
        error.__cause__ = ValueError(secret)
        error.payload = {'userName':secret,'response':secret}
        fields = worker.safe_failure_fields(error)
        self.assertEqual(fields,preflight_failure_fields(error))
        self.assertEqual(fields['error_diagnostic']['category'],'walkthrough_preflight_failed')
        self.assertEqual(fields['error_diagnostic']['stage'],'product_identity')
        self.assertEqual(fields['error_diagnostic']['reason'],'user_token_refresh_required')
        self.assertNotIn(secret,json.dumps(fields))
        self.assertNotIn('https://',json.dumps(fields))


if __name__=='__main__':
    unittest.main()
