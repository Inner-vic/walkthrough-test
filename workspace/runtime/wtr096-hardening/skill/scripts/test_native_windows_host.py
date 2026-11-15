"""Synthetic Windows host/config/cache tests; no cloud, auth or publication."""
import copy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import native_windows_host as host
import native_log_source as native
from request_model import InputError

BASE = Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.name == 'nt', 'Windows native interface tests')
class WindowsHostTests(unittest.TestCase):
    def setUp(self):
        self.root = BASE / 'w26/host-tests' / uuid.uuid4().hex[:12]
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.python = self.root / 'python.exe'
        self.python.write_bytes(b'synthetic executable, never run')
        self.analyzer = self.root / 'latest analyzer'
        self.analyzer.mkdir()
        self.git = self.root / 'git usr bin'
        self.git.mkdir()
        for name in ('gawk', 'grep'):
            (self.git / (name + '.exe')).write_bytes(('synthetic ' + name).encode())
        self.adapter = self.root / 'adapter.py'
        self.adapter.write_text('POLICY=' + repr(host.ADAPTER_POLICY) + '\n', encoding='utf-8')
        self.enterContext(patch.object(host, '_adapter_path', return_value=self.adapter))
        self.cfg = {'execution': 'windows', 'python': str(self.python),
                    'analyzer_root': str(self.analyzer), 'windows_git_bin': str(self.git)}

    def cleanup(self):
        assert self.root.resolve().parent == (BASE / 'w26/host-tests').resolve()
        shutil.rmtree(self.root)

    def binding(self):
        return host.runtime_binding(self.cfg)

    def execution_audit(self, binding=None):
        binding = binding or self.binding()
        return {'policy': host.ADAPTER_POLICY, 'actual_gawk_calls': 1, 'actual_grep_calls': 1,
            'records': [dict(policy=host.ADAPTER_POLICY, logical_tool=logical, errors=[], returncode=0,
                timezone='CST-8', locale='C', executable=binding['tools'][tool]['path'],
                executable_sha256=binding['tools'][tool]['sha256'], only_default_symbol_renamed=True,
                explicit_dynamic_regex_bracket_inputs=[],reversible_translation_verified=True,
                original_program_sha256='a' * 64, actual_program_sha256='b' * 64)
                for logical, tool in [('mawk', 'gawk'), ('grep', 'grep')]]}

    def probe(self, *, epoch=1792741320, bad_version=False, bad_hex=False):
        def tool(argv, *, environment, input=None):
            self.assertEqual(environment['TZ'], 'CST-8')
            self.assertEqual(environment['LC_ALL'], 'C')
            if argv[-1] == '--version':
                name = Path(argv[0]).stem
                text = host.TOOL_VERSION_PREFIXES[name]
                if bad_version and name == 'gawk':
                    text = 'GNU Awk 5.3.20'
                return SimpleNamespace(returncode=0, stdout=text + '\n', stderr='')
            if Path(argv[0]).stem == 'gawk':
                return SimpleNamespace(returncode=0, stdout='1792741320 ' + ('54' if bad_hex else '53') + '\n', stderr='')
            return SimpleNamespace(returncode=0, stdout='native-probe\n', stderr='')
        with patch.object(host.sys, 'executable', str(self.python)), \
             patch.object(host, '_tool_run', side_effect=tool), \
             patch.object(host.time, 'mktime', return_value=epoch), \
             patch.dict(host.os.environ, {'TZ': 'CST-8'}):
            return host.probe_windows(self.cfg)

    def test_explicit_windows_paths_accept_spaces_without_shell_parsing(self):
        self.assertEqual(host.validate_windows_config(self.cfg)['windows_git_bin'], str(self.git.resolve()))

    def test_no_os_guess_changes_existing_default_profile(self):
        self.assertEqual(host.analysis_profile(), host.NON_WINDOWS_ANALYSIS_PROFILE)
        self.assertEqual(host.analysis_profile({'execution': 'wsl'}), host.NON_WINDOWS_ANALYSIS_PROFILE)
        self.assertEqual(host.analysis_profile(self.cfg), host.WINDOWS_ANALYSIS_PROFILE)

    def test_relative_unc_drive_relative_and_posix_paths_are_rejected(self):
        for value in ('python.exe', 'C:python.exe', r'\\server\share\python.exe', '/opt/python'):
            with self.subTest(path=value), self.assertRaises(ValueError):
                host.validate_windows_config({**self.cfg, 'python': value})

    def test_missing_tool_or_interpreter_is_rejected(self):
        (self.git / 'grep.exe').unlink()
        with self.assertRaises(ValueError):
            self.binding()

    def test_reparse_path_is_rejected(self):
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)):
            with self.assertRaisesRegex(ValueError, 'reparse'):
                host._no_links(self.python)

    def test_unknown_adapter_policy_is_not_accepted(self):
        self.adapter.write_text("POLICY='unknown'\n", encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'policy'):
            self.binding()

    def test_child_environment_is_fixed_without_mutating_parent(self):
        original = {'TZ': 'Asia/Shanghai', 'PATH': 'unchanged', 'KEEP': 'yes'}
        environment = host.execution_environment(self.cfg, original)
        self.assertEqual(original, {'TZ': 'Asia/Shanghai', 'PATH': 'unchanged', 'KEEP': 'yes'})
        self.assertEqual(environment['TZ'], 'CST-8')
        self.assertEqual(environment['LC_ALL'], 'C')
        self.assertEqual(environment['PYTHONUTF8'], '1')
        self.assertEqual(environment['PATH'], 'unchanged')

    def test_binding_rejects_tool_change_and_adapter_change(self):
        expected = self.binding()
        (self.git / 'gawk.exe').write_bytes(b'changed tool')
        with self.assertRaises(ValueError):
            host.validate_binding(expected, self.cfg)
        expected = self.binding()
        self.adapter.write_text('POLICY=' + repr(host.ADAPTER_POLICY) + '\n# changed managed source\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            host.validate_binding(expected, self.cfg)

    def test_offline_probe_validates_python_gawk_epoch_hex_and_grep(self):
        receipt = self.probe()
        self.assertEqual(host.validate_probe(receipt, self.cfg), receipt)

    def test_wrong_crt_timezone_epoch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'timezone'):
            self.probe(epoch=1792741320 + 7 * 3600)

    def test_unverified_tool_version_and_wrong_hex_result_are_rejected(self):
        for kwargs in ({'bad_version': True}, {'bad_hex': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.probe(**kwargs)

    def test_probe_label_without_exact_execution_values_is_rejected(self):
        receipt = self.probe()
        receipt['gawk_epoch'] += 1
        with self.assertRaises(ValueError):
            host.validate_probe(receipt, self.cfg)

    def test_actual_execution_requires_current_tools_success_and_timezone(self):
        binding = self.binding()
        self.assertEqual(host.validate_execution_audit(self.execution_audit(binding), binding)['actual_gawk_calls'], 1)
        for key, value in [('executable_sha256', '0' * 64), ('timezone', 'Asia/Shanghai'), ('returncode', 1)]:
            audit = self.execution_audit(binding)
            audit['records'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                host.validate_execution_audit(audit, binding)

    def test_execution_audit_does_not_invent_calls_from_summary_counts(self):
        audit = self.execution_audit()
        audit['actual_gawk_calls'] += 1
        with self.assertRaises(ValueError):
            host.validate_execution_audit(audit, self.binding())

    def test_v2_approved_bracket_translation_is_allowed_but_unknown_edits_are_rejected(self):
        binding = self.binding()
        audit = self.execution_audit(binding)
        audit['records'][0].update(only_default_symbol_renamed=False,
                                  explicit_dynamic_regex_bracket_inputs=['finish', 'pc ts'])
        self.assertEqual(host.validate_execution_audit(audit, binding), audit)
        for change in ({'explicit_dynamic_regex_bracket_inputs': ['invented']},
                       {'reversible_translation_verified': False},
                       {'only_default_symbol_renamed': True}):
            invalid = copy.deepcopy(audit)
            invalid['records'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                host.validate_execution_audit(invalid, binding)

    def test_previous_windows_v1_adapter_receipt_is_not_reused(self):
        audit = self.execution_audit()
        audit['policy'] = 'windows-git-gawk-private-v1'
        with self.assertRaises(ValueError):
            host.validate_execution_audit(audit, self.binding())

    def test_windows_cleanup_removes_only_three_native_subtrees(self):
        window = self.root / 'window'
        window.mkdir()
        for name in ('input', 'native-prepared', 'analysis'):
            (window / name).mkdir()
            (window / name / 'synthetic.log').write_text('synthetic')
        (window / 'report-data.json').write_text('{}')
        native.cleanup_inputs(window, {'local_analysis': self.cfg})
        self.assertEqual([p.name for p in window.iterdir()], ['report-data.json'])

    def test_config_validator_keeps_native_full_and_rejects_shortcut_backend(self):
        settings = {'cloud_settings_path': 'synthetic', 'local_analysis': {**self.cfg,
                    'backend': 'native_full', 'rules_json': 'synthetic rules'}}
        self.assertEqual(native.validate_config(settings)['execution'], 'windows')
        settings['local_analysis']['backend'] = 'direct'
        with self.assertRaises(InputError):
            native.validate_config(settings)

    def test_windows_v2_cache_rejected_even_with_current_profile_and_runtime(self):
        from test_native_cache_identity import NativeCacheIdentityTests
        fixture = NativeCacheIdentityTests('test_altered_metric_values_are_rejected')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        binding = self.binding()
        fixture.report['analysis_scope']['profile'] = host.WINDOWS_ANALYSIS_PROFILE
        fixture.cache['analysis_scope']['profile'] = host.WINDOWS_ANALYSIS_PROFILE
        fixture.report.update(windows_host_runtime=binding, windows_execution=self.execution_audit(binding))
        fixture.cache.update(windows_host_runtime=copy.deepcopy(binding),
                             windows_execution=copy.deepcopy(fixture.report['windows_execution']))
        fixture.rewrite_detail_with_valid_hashes()
        # Updating the profile label and tool receipt cannot turn the old
        # basename-only selection proof into a source namespace audit.
        with self.assertRaisesRegex(InputError, '按来源'):
            native.cached_result(fixture.metadata, fixture.root, fixture.rules,
                                 execution_settings=self.cfg)
        with self.assertRaises(InputError):
            native.cached_result(fixture.metadata, fixture.root, fixture.rules)
        (self.git / 'grep.exe').write_bytes(b'updated tool')
        with self.assertRaisesRegex(InputError, '绑定'):
            native.cached_result(fixture.metadata, fixture.root, fixture.rules, execution_settings=self.cfg)

    def test_linux_cache_cannot_be_claimed_as_windows_even_when_statistics_match(self):
        from test_native_cache_identity import NativeCacheIdentityTests
        fixture = NativeCacheIdentityTests('test_altered_metric_values_are_rejected')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.assertRaises(InputError):
            native.cached_result(fixture.metadata, fixture.root, fixture.rules, execution_settings=self.cfg)

    def test_windows_preflight_uses_isolated_utf8_argv_and_requires_real_probe_receipt(self):
        import native_preflight
        import native_windows_process
        from test_native_preflight import good_receipt
        payload = good_receipt()
        payload.update(execution='windows', windows_native=self.probe())
        with patch.object(native_windows_process, 'run_windows_tree',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))) as run:
            self.assertEqual(native_preflight.check_native_environment(self.cfg), payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [str(self.python), '-X', 'utf8', '-B'])
        self.assertNotIn('wsl.exe', argv)
        self.assertIn('--windows-git-bin', argv)
        self.assertEqual(run.call_args.kwargs['env']['TZ'], 'CST-8')
        del payload['windows_native']
        with patch.object(native_windows_process, 'run_windows_tree',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))):
            with self.assertRaises(native_preflight.NativePreflightError):
                native_preflight.check_native_environment(self.cfg)

    def test_windows_analysis_uses_explicit_flag_and_job_environment_without_shell(self):
        import native_inputs
        import native_windows_process
        metadata = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:48:00',
                    'end': '2026-10-21T16:22:00'}
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        cfg = {**self.cfg, 'backend': 'native_full', 'rules_json': str(rules)}
        settings = {'local_analysis': cfg, 'cloud_settings_path': 'not-read'}
        window = self.root / 'window'
        (window / 'input').mkdir(parents=True)
        downloaded = {'request': metadata, 'environment': 'testtwo', 'source_policy': native.SOURCE_POLICY,
                      'files': [], 'counts': {}, 'bytes': 0}
        native.write_json(window / 'input/download-manifest.json', downloaded)
        with patch.object(native_inputs, 'prepare_native_inputs', return_value={}), \
             patch.object(native_windows_process, 'run_windows_tree',
                return_value=SimpleNamespace(returncode=1, stdout='{"status":"failed"}', stderr='synthetic failure')) as run, \
             patch.object(native, 'fetch_window', side_effect=AssertionError('must not query')), \
             patch.object(native.subprocess, 'run', side_effect=AssertionError('must use Job boundary')):
            with self.assertRaisesRegex(InputError, '原生解析失败'):
                native.analyze_window(metadata, settings, window, rules, prepared=downloaded)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [str(self.python), '-X', 'utf8', '-B'])
        self.assertEqual(argv[-4:], ['--native-execution', 'windows', '--source-identity', 'namespace_v3'])
        self.assertEqual(run.call_args.kwargs['env']['TZ'], 'CST-8')
        self.assertEqual(run.call_args.kwargs['env']['WALKTHROUGH_WINDOWS_GIT_BIN'], str(self.git.resolve()))

    def test_new_configuration_cli_has_explicit_windows_and_preserves_wsl_default(self):
        from configure_walkthrough import build_parser
        common = ['--workspace-root', str(self.root), '--identity-settings', str(self.root / 'identity.json'),
            '--cloud-settings', str(self.root / 'cloud.json'), '--output', str(self.root / 'new.json'),
            '--analyzer-python', str(self.python), '--analyzer-root', str(self.analyzer),
            '--query-chat-id', 'oc_synthetic', '--asgard-bot-id', 'ou_synthetic',
            '--rules-json', str(self.root / 'rules.json')]
        self.assertEqual(build_parser().parse_args(common).execution, 'wsl')
        parsed = build_parser().parse_args(common + ['--execution', 'windows', '--windows-git-bin', str(self.git)])
        self.assertEqual(parsed.execution, 'windows')
        self.assertEqual(parsed.windows_git_bin, str(self.git))


if __name__ == '__main__':
    unittest.main()
