"""Offline bounded-session, lock and publisher failure-injection regressions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import patch

import native_sheet_delivery as delivery
import template_reference_access as access_module
import test_template_reference_reader as fixtures
from lark_auth_lock import lark_auth_lock, LarkAuthLockTimeout
from request_model import InputError


class SnapshotSessionTests(unittest.TestCase):
    setUp = fixtures.TemplateReferenceReaderTests.setUp
    save_manifest = fixtures.TemplateReferenceReaderTests.save_manifest
    run_delivery = fixtures.TemplateReferenceReaderTests.run_delivery
    change_data = fixtures.TemplateReferenceReaderTests.change_data
    assert_no_real_io = fixtures.TemplateReferenceReaderTests.assert_no_real_io

    def prepare(self, *, role='baseline', purpose='reference', timeout=180):
        references = {name: {'token': 'templateSynthetic', 'sheet_id': 'sheet0'} for name in delivery.ROLES}
        access = access_module.build_source_binding(self.settings, references, 'relaySynthetic')
        directory = self.path.with_suffix('.native-sheets') / role
        directory.mkdir(parents=True, exist_ok=True)
        scope = {'job_key': self.record['job_key'], 'role': role, 'purpose': purpose}
        session = access_module.user_reference_read_session(self.settings, access,
            scope=scope, directory=directory, timeout=timeout)
        return session, access, scope, directory

    def snapshot(self, session, access, scope, directory):
        def call(settings, unused_binding, args, timeout=150):
            return session.call(settings, access, args, scope=scope, timeout=timeout)
        return delivery._snapshot(self.settings, {}, 'templateSynthetic', directory, 'reference', call=call)

    def test_complete_snapshot_keeps_all_15_reads_with_two_auth_checks_under_same_lock(self):
        session, access, scope, directory = self.prepare()
        self.snapshot_auth_status.reset_mock()
        owner = threading.get_ident()
        observations = []
        def status(settings, **options):
            observations.append(threading.get_ident())
            self.assertIs(options['verify'], False)
            self.assertGreater(options['timeout'], 0)
            self.assertLessEqual(options['timeout'], 30)
            return {'appId': settings['expected_lark_app_id'], 'identities': {'user': {
                'openId': settings['expected_operator_open_id'], 'available': True, 'tokenStatus': 'valid',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}}
        self.snapshot_auth_status.side_effect = status
        with session:
            value = self.snapshot(session, access, scope, directory)
            # The exact same lock remains reentrant in this thread.
            with lark_auth_lock(self.settings, timeout=0.1):
                pass
            # A different thread cannot access credentials until all reads and
            # the post-identity check complete.
            with ThreadPoolExecutor(max_workers=1) as pool:
                def blocked():
                    with lark_auth_lock(self.settings, timeout=0.02):
                        return 'unexpected access'
                with self.assertRaises(LarkAuthLockTimeout):
                    pool.submit(blocked).result()
        self.assertEqual(len(value['sheets']), 7)
        self.assertEqual(session.audit['api_call_count'], 15)
        self.assertEqual(session.audit['identity_check_count'], 2)
        self.assertEqual(session.audit['status'], 'verified')
        self.assertEqual(observations, [owner, owner])
        self.assertEqual(self.snapshot_auth_status.call_count, 2)
        self.assertEqual(self.fake.counts['info'], 1)
        self.assertEqual(self.fake.counts['layout'], 7)
        self.assertEqual(self.fake.counts['get'], 7)
        self.assert_no_real_io()

    def test_nested_cli_keeps_existing_replica_begin_and_commit_protocol(self):
        session, access, scope, directory = self.prepare()
        events = []
        original_call = access_module.policy.run_lark
        original_auth = self.snapshot_auth_status.side_effect
        def nested_call(settings, args, **options):
            with lark_auth_lock(settings, timeout=0.1):
                return original_call(settings, args, **options)
        def nested_auth(settings, **options):
            with lark_auth_lock(settings, timeout=0.1):
                return original_auth(settings, **options)
        with (patch('lark_auth_lock.import_replica', side_effect=lambda settings: events.append('import')),
              patch('lark_auth_lock.begin_replica', side_effect=lambda settings, digest: events.append('begin')),
              patch('lark_auth_lock.export_replica', side_effect=lambda settings, digest: events.append('export') or {}),
              patch.object(access_module.policy, 'run_lark', side_effect=nested_call),
              patch.object(access_module.policy, 'auth_status', side_effect=nested_auth)):
            with session:
                self.snapshot(session, access, scope, directory)
        self.assertEqual(events.count('import'), 1)
        self.assertEqual(events.count('begin'), 17)  # 15 API + pre/post auth.
        self.assertEqual(events.count('export'), 1)

    def test_post_snapshot_user_change_blocks_copy_and_closes_session(self):
        session, access, scope, directory = self.prepare()
        original = self.snapshot_auth_status.side_effect
        count = [0]
        def changed(settings, **options):
            count[0] += 1
            value = original(settings, **options)
            if count[0] == 2:
                value['identities']['user']['openId'] = 'ou_changedUser'
            return value
        self.snapshot_auth_status.side_effect = changed
        with self.assertRaisesRegex(InputError, 'App/User'):
            with session:
                self.snapshot(session, access, scope, directory)
        self.assertEqual(session.state, 'closed')
        self.assertEqual(session.audit['status'], 'failed')
        self.assertNotIn('copy', self.fake.counts)
        with lark_auth_lock(self.settings, timeout=0.1):
            pass

    def test_pre_or_post_app_change_and_invalid_auth_never_yields_valid_snapshot(self):
        for phase in (1, 2):
            for field, invalid in (('appId', 'cli_wrong'), ('available', False), ('tokenStatus', 'expired'), ('expiresAt', '2000-01-01T00:00:00+00:00')):
                with self.subTest(phase=phase, field=field):
                    session, access, scope, directory = self.prepare()
                    count = [0]
                    def status(settings, **options):
                        count[0] += 1
                        value = {'appId': settings['expected_lark_app_id'], 'identities': {'user': {
                            'openId': settings['expected_operator_open_id'], 'available': True, 'tokenStatus': 'valid',
                            'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}}
                        if count[0] == phase:
                            (value if field == 'appId' else value['identities']['user'])[field] = invalid
                        return value
                    self.snapshot_auth_status.side_effect = status
                    with self.assertRaises(InputError):
                        with session:
                            self.snapshot(session, access, scope, directory)
                    self.assertEqual(session.audit['status'], 'failed')
                    self.assertEqual(session.state, 'closed')

    def test_read_error_runs_post_check_and_cannot_be_reused(self):
        session, access, scope, directory = self.prepare()
        self.snapshot_auth_status.reset_mock()
        self.fake.fail['layout'] = 'before'
        with self.assertRaises(InputError):
            with session:
                self.snapshot(session, access, scope, directory)
        self.assertEqual(self.snapshot_auth_status.call_count, 2)
        count = len(self.fake.events)
        with self.assertRaises(InputError):
            session.call(self.settings, access, ['sheets', '+workbook-info', '--spreadsheet-token', 'templateSynthetic'], scope=scope)
        self.assertEqual(len(self.fake.events), count)
        with self.assertRaises(InputError):
            session.__enter__()

    def test_timeout_is_total_budget_and_no_later_api_or_valid_snapshot(self):
        session, access, scope, directory = self.prepare(timeout=1)
        clock = [100.0]
        with patch.object(access_module.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(InputError, '有界'):
                with session:
                    clock[0] = 101.1
                    self.snapshot(session, access, scope, directory)
        self.assertEqual(session.audit['api_call_count'], 0)
        self.assertEqual(session.audit['status'], 'failed')

    def test_scope_settings_and_access_changes_reject_before_next_api(self):
        for which in ('scope', 'settings', 'access'):
            with self.subTest(which=which):
                session, access, scope, directory = self.prepare()
                old_settings = copy.deepcopy(self.settings)
                count = len(self.fake.events)
                with self.assertRaises(InputError):
                    with session:
                        if which == 'scope': scope['role'] = 'replacement'
                        elif which == 'settings': self.settings['expected_operator_open_id'] = 'ou_changed'
                        else: access['source_token'] = 'otherTemplate'
                        session.call(self.settings, access, ['sheets', '+workbook-info', '--spreadsheet-token', 'templateSynthetic'], scope=scope)
                self.assertEqual(len(self.fake.events), count)
                self.settings.clear(); self.settings.update(old_settings)

    def test_session_cannot_be_used_from_another_thread(self):
        session, access, scope, directory = self.prepare()
        with self.assertRaises(InputError):
            with session:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(InputError):
                        pool.submit(session.call, self.settings, access,
                            ['sheets', '+workbook-info', '--spreadsheet-token', 'templateSynthetic'], scope=scope).result()
        self.assertEqual(session.audit['api_call_count'], 0)

    def test_manifest_change_during_snapshot_is_not_accepted(self):
        session, access, scope, directory = self.prepare()
        with self.assertRaises(InputError):
            with session:
                self.manifest['verification']['extra'] = 'changed'
                self.save_manifest()
                self.snapshot(session, access, scope, directory)
        self.assertEqual(session.audit['status'], 'failed')
        self.assertEqual(session.audit['api_call_count'], 0)

    def test_only_read_commands_and_bound_output_directory_are_allowed(self):
        commands = [
            ['drive', '+copy', '--token', 'templateSynthetic'],
            ['sheets', '+workbook-info', '--spreadsheet-token', 'relaySynthetic'],
            ['sheets', '+workbook-info', '--spreadsheet-token', 'templateSynthetic', '--as', 'bot'],
            ['drive', 'permission.members', 'auth', '--token', 'templateSynthetic', '--type', 'sheet', '--action', 'copy']]
        for command in commands:
            with self.subTest(command=command):
                session, access, scope, directory = self.prepare()
                with self.assertRaises(InputError):
                    with session:
                        session.call(self.settings, access, command, scope=scope)
                self.assertEqual(session.audit['api_call_count'], 0)

    def test_more_than_one_complete_snapshot_is_rejected(self):
        session, access, scope, directory = self.prepare()
        with self.assertRaisesRegex(InputError, '调用上限'):
            with session:
                self.snapshot(session, access, scope, directory)
                self.snapshot(session, access, scope, directory)
        self.assertEqual(session.audit['api_call_count'], 15)

    def test_output_path_cannot_cross_role_and_invalid_timeouts_are_finite(self):
        session, access, scope, directory = self.prepare()
        other = directory.parent / 'replacement'
        other.mkdir()
        relative = './' + (other / 'foreign.json').relative_to(self.root).as_posix()
        command = ['sheets', '+cells-get', '--spreadsheet-token', 'templateSynthetic',
            '--sheet-id', 'sheet0', '--range', 'B1:O2', '--include', 'value,formula,style', '--output-path', relative]
        with self.assertRaisesRegex(InputError, '跨任务或角色'):
            with session:
                session.call(self.settings, access, command, scope=scope)
        self.assertFalse((other / 'foreign.json').exists())
        self.assertEqual(session.audit['api_call_count'], 0)
        for invalid in (True, None, 0, -1, float('inf'), float('nan'), 181):
            with self.subTest(timeout=invalid), self.assertRaises(InputError):
                access_module.user_reference_read_session(self.settings, access,
                    scope=scope, directory=directory, timeout=invalid)

    def test_full_publisher_post_identity_failure_does_not_save_snapshot_or_create_asset(self):
        original = self.snapshot_auth_status.side_effect
        count = [0]
        def changed(settings, **options):
            count[0] += 1
            value = original(settings, **options)
            if count[0] == 2:
                value['identities']['user']['openId'] = 'ou_changedAfterReads'
            return value
        self.snapshot_auth_status.side_effect = changed
        with self.assertRaises(InputError):
            self.run_delivery()
        baseline = self.record['native_sheets']['roles']['baseline']
        self.assertNotIn('reference_snapshot', baseline)
        self.assertNotIn('copy', self.fake.counts)
        metric = next(row for row in baseline['phase_timings'] if row['stage'] == 'snapshot_reference')
        self.assertEqual(metric['outcome'], 'failed')
        self.assertEqual(metric['reference_read_session']['status'], 'failed')

    def test_full_publisher_has_four_15_read_sessions_eight_auth_checks_and_unchanged_bot_lifecycle(self):
        self.snapshot_auth_status.reset_mock()
        output = self.run_delivery()
        self.assertEqual(self.snapshot_auth_status.call_count, 8)
        self.assertEqual(self.fake.auth_calls, 4)  # Original copy-permission pre/rechecks retained.
        self.assertEqual(self.fake.counts['copy'], 2)
        self.assertEqual(self.fake.counts['write'], 2)
        self.assertEqual(len(self.owner_calls), 2)
        for role in delivery.ROLES:
            rows = self.record['native_sheets']['roles'][role]['phase_timings']
            sessions = [row for row in rows if 'reference_read_session' in row]
            self.assertEqual(len(sessions), 2)
            self.assertTrue(all(row['api_call_count'] == 15 and row['reference_read_session']['identity_check_count'] == 2 for row in sessions))
            self.assertTrue(all(row['reference_read_session']['status'] == 'verified' for row in sessions))
            self.assertEqual(output[role]['validation']['status'], 'ok')
            self.assertTrue(output[role]['permissions']['owner_transfer'])
            self.assertEqual(output[role]['permissions']['secure_label']['name'], 'L3')
        self.assert_no_real_io()


if __name__ == '__main__':
    unittest.main()
