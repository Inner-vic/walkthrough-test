"""Read-only staged preflight failures and identity-bound expiry classification."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import preflight
import preflight_diagnostics as diagnostics


SECRET = 'SYNTHETIC-PRIVATE-TOKEN-OR-PATH'
NOW = datetime(2026, 11, 2, 19, 25, tzinfo=timezone.utc)


def identity():
    return {'appId': 'cli_fixed', 'identities': {'user': {'openId': 'ou_fixed', 'available': True,
            'tokenStatus': 'valid', 'expiresAt': '2100-01-01T00:00:00+00:00'}}}


class PreflightDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cli = self.root / 'synthetic-cli.exe'
        self.cli.write_text('synthetic', encoding='utf-8')
        self.rules = self.root / 'rules.json'
        self.rules.write_text('{"theme":{"metric":{}}}', encoding='utf-8')
        self.settings = {'expected_lark_app_id': 'cli_fixed', 'expected_operator_open_id': 'ou_fixed',
            'lark_cli': str(self.cli), 'cloud_settings_path': str(self.root / SECRET),
            'local_analysis': {'environment': 'prod_then_test', 'rules_json': str(self.rules)}}
        self.product = {'asgard_app_id': 'cli_asgard', 'timeout_seconds': 120, 'poll_interval_seconds': 2}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(preflight, 'datetime', SimpleNamespace(now=lambda zone: NOW)))
        stack.enter_context(patch.object(preflight, 'validate_product_config', return_value=self.product))
        for name in ('ensure_user_authorization', 'run_lark'):
            stack.enter_context(patch.object(preflight.policy, name, side_effect=AssertionError('action forbidden')))
        stack.enter_context(patch.object(preflight.cloud, 'request_json', side_effect=AssertionError('network forbidden')))

    def product_error(self, payload, *, settings=None):
        with patch.object(preflight.policy, 'auth_status', return_value=payload) as read:
            with self.assertRaises(diagnostics.PreflightError) as caught:
                preflight.check_product_configuration(settings or self.settings)
        read.assert_called_once_with(settings or self.settings, verify=False)
        return caught.exception

    def test_valid_fixed_identity_preserves_readonly_success_contract(self):
        with patch.object(preflight.policy, 'auth_status', return_value=identity()) as read:
            result = preflight.check_product_configuration(self.settings)
        read.assert_called_once_with(self.settings, verify=False)
        self.assertTrue(result['fixed_operator_matched'])
        self.assertTrue(result['project_app_matched'])
        for key in ('network_verified', 'query_sent', 'authorization_requested'):
            self.assertFalse(result[key])
        serialized = json.dumps(result)
        for value in ('cli_fixed', 'ou_fixed', '2100-', SECRET, str(self.root)):
            self.assertNotIn(value, serialized)

    def test_only_explicit_expiry_after_matching_identity_requests_refresh(self):
        cases = [({'tokenStatus': 'expired'}, True), ({'tokenStatus': 'expired'}, False),
            ({'tokenStatus': 'expired', 'expiresAt': None}, False),
            ({'tokenStatus': 'valid', 'expiresAt': NOW.isoformat()}, False),
            ({'tokenStatus': 'unknown', 'expiresAt': '2000-01-01T00:00:00+00:00'}, False)]
        for fields, available in cases:
            payload = identity()
            payload['identities']['user'].update(fields, available=available)
            with self.subTest(fields=fields, available=available):
                error = self.product_error(payload)
            self.assertEqual((error.stage, error.reason), ('product_identity', 'user_token_refresh_required'))

    def test_mismatched_or_missing_app_and_user_do_not_request_refresh_even_if_expired(self):
        variants = [
            ('project_app_missing', lambda value: value.pop('appId')),
            ('project_app_mismatch', lambda value: value.update(appId='cli_other')),
            ('user_identity_unavailable', lambda value: value['identities'].pop('user')),
            ('user_identity_unavailable', lambda value: value['identities']['user'].pop('openId')),
            ('user_identity_mismatch', lambda value: value['identities']['user'].update(openId='ou_other')),
        ]
        for reason, mutate in variants:
            payload = identity()
            payload['identities']['user'].update(tokenStatus='expired', available=False)
            mutate(payload)
            with self.subTest(reason=reason):
                error = self.product_error(payload)
                self.assertEqual(error.reason, reason)
                self.assertNotEqual(error.reason, 'user_token_refresh_required')

    def test_unknown_or_malformed_status_is_not_inferred_to_need_refresh(self):
        variants = [
            ('user_token_status_invalid', {'tokenStatus': 'invalid'}),
            ('user_token_status_invalid', {'tokenStatus': 'unknown', 'expiresAt': None}),
            ('user_token_status_invalid', {'tokenStatus': None}),
            ('user_token_status_invalid', {'tokenStatus': True}),
            ('user_token_expiry_unavailable', {'expiresAt': None}),
            ('user_token_expiry_unavailable', {'expiresAt': SECRET}),
            ('user_identity_unavailable', {'available': False}),
            ('auth_status_invalid', {'tokenStatus': 'expired', 'available': 'false'}),
        ]
        for reason, fields in variants:
            payload = identity()
            payload['identities']['user'].update(fields)
            with self.subTest(fields=fields):
                self.assertEqual(self.product_error(payload).reason, reason)
        with patch.object(preflight.policy, 'auth_status', side_effect=ValueError('expired ' + SECRET)), \
                self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check_product_configuration(self.settings)
        self.assertEqual(caught.exception.reason, 'auth_status_unavailable')
        self.assertNotIn(SECRET, json.dumps(diagnostics.preflight_failure_fields(caught.exception)))
        self.assertEqual(self.product_error([]).reason, 'auth_status_invalid')

    def test_missing_product_or_fixed_identity_configuration_precedes_auth_read(self):
        with patch.object(preflight, 'validate_product_config', side_effect=ValueError(SECRET)), \
                patch.object(preflight.policy, 'auth_status') as read, \
                self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check_product_configuration(self.settings)
        self.assertEqual(caught.exception.reason, 'product_configuration_invalid')
        read.assert_not_called()
        for fields in ({'expected_lark_app_id': None}, {'expected_operator_open_id': None},
                       {'expected_operator_open_id': 123}, {'expected_lark_app_id': {SECRET: SECRET}}):
            settings = {**self.settings, **fields}
            with self.subTest(fields=list(fields)), patch.object(preflight.policy, 'auth_status') as read, \
                    self.assertRaises(diagnostics.PreflightError) as caught:
                preflight.check_product_configuration(settings)
            self.assertEqual(caught.exception.reason, 'fixed_identity_not_configured')
            read.assert_not_called()

    def whole_preflight(self, overrides=None):
        stack = ExitStack()
        mocks = {}
        points = {'load_settings': self.settings, 'check_host_dependencies': {'host': 'synthetic'},
            'check_cloud_configuration': {'prod': {'configuration_status': 'configured'}},
            'cloud.load_settings': {'username': SECRET}, 'check_product_configuration': {'query_sent': False},
            'check_native_environment': {'status': 'native_preflight_passed'},
            'check_delivery_configuration': {'online_permissions_verified': False}}
        for name, value in points.items():
            kwargs = {'side_effect': overrides[name]} if overrides and name in overrides else {'return_value': value}
            mocks[name] = stack.enter_context(patch('preflight.' + name, **kwargs))
        return stack, mocks

    def test_each_preflight_stage_converts_unknown_errors_without_parsing_text(self):
        points = {'settings': 'load_settings', 'host_deps': 'check_host_dependencies',
            'cloud_config': 'check_cloud_configuration', 'cloud_identity': 'cloud.load_settings',
            'product_identity': 'check_product_configuration', 'native': 'check_native_environment',
            'delivery': 'check_delivery_configuration'}
        class UnprintableError(RuntimeError):
            def __str__(self):
                raise AssertionError('exception text must never be inspected')
        for stage, point in points.items():
            stack, _ = self.whole_preflight({point: UnprintableError(SECRET)})
            with self.subTest(stage=stage), stack, self.assertRaises(diagnostics.PreflightError) as caught:
                preflight.check(self.root / SECRET)
            fields = diagnostics.preflight_failure_fields(caught.exception)
            self.assertEqual(fields['error_diagnostic'], {'category': 'walkthrough_preflight_failed',
                'stage': stage, 'reason': 'check_failed'})
            self.assertNotIn(SECRET, json.dumps(fields))
        self.rules.write_text('not json ' + SECRET, encoding='utf-8')
        stack, _ = self.whole_preflight()
        with stack, self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check(self.root / SECRET)
        self.assertEqual((caught.exception.stage, caught.exception.reason), ('rules', 'check_failed'))

    def test_complete_preflight_keeps_native_and_delivery_checks(self):
        stack, mocks = self.whole_preflight()
        with stack:
            result = preflight.check(self.root / SECRET)
        self.assertEqual(result['status'], 'local_preflight_passed')
        mocks['check_native_environment'].assert_called_once_with(self.settings['local_analysis'])
        mocks['check_delivery_configuration'].assert_called_once_with(self.settings)
        self.assertEqual(result['cloud_environment_policy'], 'prod_then_test')

    def test_host_dependency_failures_have_finite_reasons(self):
        with patch.object(preflight.sys, 'version_info', (3, 10)), self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check_host_dependencies()
        self.assertEqual(caught.exception.reason, 'host_runtime_unsupported')
        with patch.object(preflight.importlib, 'import_module', side_effect=ImportError(SECRET)), \
                self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check_host_dependencies()
        self.assertEqual(caught.exception.reason, 'host_dependency_unavailable')
        with patch.object(preflight.importlib, 'import_module', return_value=SimpleNamespace(__version__=SECRET)), \
                self.assertRaises(diagnostics.PreflightError) as caught:
            preflight.check_host_dependencies()
        self.assertEqual(caught.exception.reason, 'host_dependency_version_mismatch')

    def test_enum_pairs_and_mutated_exception_fields_cannot_create_refresh_or_leak(self):
        for stage, reasons in diagnostics.PREFLIGHT_REASONS.items():
            for reason in reasons:
                error = diagnostics.PreflightError(stage, reason)
                fields = diagnostics.preflight_failure_fields(error)
                self.assertEqual(fields['error_diagnostic']['stage'], stage)
                self.assertEqual(fields['error_diagnostic']['reason'], reason)
        for stage, reason in (('native', 'user_token_refresh_required'), (SECRET, 'check_failed'),
                              ('product_identity', SECRET)):
            with self.subTest(stage=stage, reason=reason), self.assertRaises(ValueError) as caught:
                diagnostics.PreflightError(stage, reason)
            self.assertNotIn(SECRET, str(caught.exception))
        error = diagnostics.PreflightError('product_identity', 'user_token_refresh_required')
        error.args = (SECRET,)
        error.__cause__ = ValueError(SECRET)
        error.payload = {'access_token': SECRET}
        self.assertNotIn(SECRET, json.dumps(diagnostics.preflight_failure_fields(error)))
        error.stage, error.reason = SECRET, SECRET
        self.assertEqual(diagnostics.preflight_failure_diagnostic(error, stage='native'),
            {'category': 'walkthrough_preflight_failed', 'stage': 'native', 'reason': 'check_failed'})
        self.assertEqual(diagnostics.preflight_failure_diagnostic(ValueError(SECRET), stage=SECRET),
            {'category': 'walkthrough_preflight_failed', 'stage': 'preflight', 'reason': 'check_failed'})


if __name__ == '__main__':
    unittest.main()
