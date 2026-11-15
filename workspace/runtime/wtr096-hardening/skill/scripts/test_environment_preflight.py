"""The first environment is required; a missing backup must remain lazy."""
import unittest
from unittest.mock import patch

import preflight


class EnvironmentPreflightTests(unittest.TestCase):
    def test_prod_first_allows_missing_test_until_fallback_is_needed(self):
        def configuration(environment):
            if environment == 'test':
                raise ValueError('synthetic missing backup')
            return {}
        with patch.object(preflight.cloud, 'environment_config', side_effect=configuration):
            evidence = preflight.check_cloud_configuration('prod_then_test')
        self.assertTrue(evidence['prod']['required_at_start'])
        self.assertFalse(evidence['test']['required_at_start'])
        self.assertEqual(evidence['test']['configuration_status'], 'missing_or_invalid')

    def test_prod_first_rejects_missing_prod_even_if_test_is_configured(self):
        def configuration(environment):
            if environment == 'prod':
                raise ValueError('synthetic missing first environment')
            return {}
        with patch.object(preflight.cloud, 'environment_config', side_effect=configuration):
            with self.assertRaisesRegex(ValueError, 'missing first'):
                preflight.check_cloud_configuration('prod_then_test')


if __name__ == '__main__':
    unittest.main()
