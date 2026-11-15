"""Per-window selection and frozen legacy-final guards; synthetic only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import cloud_log_client as cloud
import cloud_pipeline as pipeline
import environment_selection as selection
import window_pipeline as window
import walkthrough_worker as worker
from request_model import InputError


REQUESTS = {
    'baseline': {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:00:00', 'end': '2026-10-21T15:01:00'},
    'replacement': {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-22T15:00:00', 'end': '2026-10-22T15:01:00'},
}


def counts(available=True):
    return {name: int(available) for name in selection.CLASSES}


def task_fixture(environment='test', policy='test_then_prod', requests=None):
    attempts = []
    if environment == 'prod' and policy == 'test_then_prod':
        attempts.append({'environment': 'testtwo', 'windows': {
            'baseline': {'counts': counts(), 'pcm_status': 'ok'},
            'replacement': {'counts': counts(False), 'pcm_status': 'ok'}}})
    attempts.append({'environment': selection.provider(environment), 'windows': {
        role: {'counts': counts(), 'pcm_status': 'ok'} for role in selection.ROLES}})
    return {'version': selection.TASK_VERSION, 'status': 'selected', 'request': deepcopy(requests or REQUESTS),
            'policy': policy, 'environment': environment,
            'provider_environment': selection.provider(environment), 'attempts': attempts}


def pipeline_fixture(task):
    return {'version': selection.TASK_PIPELINE_VERSION,
            'environment': task['provider_environment'], 'environment_policy': task['policy'],
            'task_environment_selection': deepcopy(task),
            'task_selection_sha256': selection.task_selection_sha256(task),
            'windows': {role: {'version': window.RESULT_VERSION, 'status': 'ok', 'rules_sha256': 'a' * 64,
                'request': deepcopy(task['request'][role]),
                'environment': task['provider_environment'],
                'environment_selection': selection.window_decision(task, role)} for role in selection.ROLES},
            'sources': {role: window.source_plan(task['request'][role], task['environment'])
                        for role in selection.ROLES}}


def per_window_task_fixture(environments=None, policy='test_then_prod', requests=None):
    requests = deepcopy(requests or REQUESTS)
    environments = environments or {role: 'test' for role in selection.ROLES}
    choices = {}
    for role in selection.ROLES:
        env = environments[role]
        order = selection.POLICY_ORDERS[policy]
        choices[role] = {'version': selection.WINDOW_VERSION, 'status': 'selected',
            'selection_scope': 'window', 'policy': policy, 'request': requests[role],
            'environment': env, 'provider_environment': selection.provider(env),
            'attempts': [{'environment': selection.provider(name),
                          'counts': counts(name == env), 'pcm_status': 'ok'}
                         for name in order[:order.index(env) + 1]]}
    return {'version': selection.PER_WINDOW_TASK_VERSION, 'status': 'selected',
            'selection_scope': 'per_window', 'policy': policy, 'request': requests,
            'environment': 'per_window', 'provider_environment': 'per_window', 'windows': choices}


def per_window_pipeline_fixture(task):
    result = {'version': selection.PER_WINDOW_PIPELINE_VERSION,
              'environment': 'per_window', 'environment_policy': task['policy'],
              'task_environment_selection': deepcopy(task),
              'task_selection_sha256': selection.task_selection_sha256(task),
              'windows': {}, 'sources': {}}
    for role in selection.ROLES:
        proof = selection.window_decision(task, role)
        result['windows'][role] = {'version': window.RESULT_VERSION, 'status': 'ok',
            'rules_sha256': 'a' * 64, 'request': deepcopy(task['request'][role]),
            'environment': proof['provider_environment'], 'environment_selection': proof}
        result['sources'][role] = window.source_plan(task['request'][role], proof['environment'])
    return result


def legacy_fixture(environments=None):
    environments = environments or {role: 'test' for role in selection.ROLES}
    windows = {}
    sources = {}
    for role in selection.ROLES:
        env = environments[role]
        attempts = []
        if env == 'prod':
            attempts.append({'environment': 'testtwo', 'counts': counts(False), 'pcm_status': 'ok'})
        attempts.append({'environment': selection.provider(env), 'counts': counts(), 'pcm_status': 'ok'})
        decision = {'version': selection.VERSION, 'policy': 'test_then_prod',
                    'request': deepcopy(REQUESTS[role]), 'environment': env,
                    'provider_environment': selection.provider(env), 'attempts': attempts}
        windows[role] = {'version': window.RESULT_VERSION, 'request': deepcopy(REQUESTS[role]),
                         'environment': selection.provider(env), 'status': 'ok',
                         'rules_sha256': 'a' * 64, 'environment_selection': decision}
        sources[role] = window.source_plan(REQUESTS[role], env)
    return {'version': 'cloud-window-full-v5', 'environment': 'per_window',
            'environment_policy': 'test_then_prod', 'windows': windows, 'sources': sources}


class TaskEnvironmentSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'cloud_settings_path': 'unused-synthetic-cloud-settings',
                         'local_analysis': {'environment': 'test_then_prod'}}

    def sources(self, availability):
        def role(metadata):
            return next(name for name in selection.ROLES if REQUESTS[name]['start'] == metadata['start'])
        def ad(metadata, log_class, username, *, environment):
            number = availability[environment][role(metadata)].get(log_class, 0)
            return [{} for _ in range(number)]
        def pcm(metadata, settings, *, environment):
            number = availability[environment][role(metadata)].get(selection.pcm.LOG_CLASS, 0)
            return [{} for _ in range(number)]
        return ad, pcm

    def select(self, availability, *, settings=None):
        ad, pcm = self.sources(availability)
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': 'synthetic'}), \
                patch.object(selection.ad, 'query_class', side_effect=ad) as ad_query, \
                patch.object(selection.pcm, 'discover', side_effect=pcm) as pcm_query:
            result = selection.select_task(REQUESTS, settings or self.settings, self.root)
        return result, ad_query.call_args_list, pcm_query.call_args_list

    def test_both_test_windows_available_lock_test_before_download(self):
        (task, discovery), ad_calls, pcm_calls = self.select({'test': {
            role: counts() for role in selection.ROLES}})
        self.assertEqual(task['environment'], 'per_window')
        self.assertEqual({choice['environment'] for choice in task['windows'].values()}, {'test'})
        self.assertEqual(set(discovery), set(selection.ROLES))
        self.assertEqual(len(ad_calls), 8)
        self.assertEqual(len(pcm_calls), 2)
        self.assertTrue(all(call.kwargs['environment'] == 'test' for call in [*ad_calls, *pcm_calls]))
        self.assertTrue((self.root / 'task-environment-selection.json').is_file())
        self.assertFalse((self.root / 'baseline').exists())

    def test_only_empty_test_window_falls_back_and_mixed_environments_are_bound(self):
        (task, discovery), ad_calls, pcm_calls = self.select({
            'test': {'baseline': counts(), 'replacement': counts(False)},
            'prod': {'replacement': counts()}})
        self.assertEqual(task['environment'], 'per_window')
        self.assertEqual([a.kwargs['environment'] for a in ad_calls], ['test'] * 8 + ['prod'] * 4)
        self.assertEqual([a.kwargs['environment'] for a in pcm_calls], ['test'] * 2 + ['prod'])
        for role, env in (('baseline', 'test'), ('replacement', 'prod')):
            proof = selection.window_decision(task, role)
            self.assertEqual(proof['environment'], env)
            selection.validate_decision(proof, REQUESTS[role], 'test_then_prod')
            self.assertEqual(len(discovery[role]['ad']), 4)

    def test_window_empty_in_both_environments_has_no_final_selection(self):
        for missing in selection.ROLES:
            with self.subTest(missing=missing):
                directory = self.root / missing
                availability = {env: {role: counts(role != missing) for role in selection.ROLES}
                                for env in ('test', 'prod')}
                ad, pcm = self.sources(availability)
                with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': 'synthetic'}), \
                        patch.object(selection.ad, 'query_class', side_effect=ad), \
                        patch.object(selection.pcm, 'discover', side_effect=pcm):
                    with self.assertRaisesRegex(InputError, '均未查到所需日志'):
                        selection.select_task(REQUESTS, self.settings, directory)
                self.assertFalse((directory / 'task-environment-selection.json').exists())
                evidence = json.loads((directory / 'task-environment-attempts.json').read_text('utf-8'))
                self.assertEqual(evidence['status'], 'failed')
                self.assertEqual(evidence['windows'][missing]['status'], 'missing_sources')

    def test_test_only_does_not_try_prod_and_prod_only_does_not_try_test(self):
        settings = {**self.settings, 'local_analysis': {'environment': 'prod'}}
        (task, _), calls, pcm_calls = self.select({'prod': {
            role: counts() for role in selection.ROLES}}, settings=settings)
        self.assertEqual({choice['environment'] for choice in task['windows'].values()}, {'prod'})
        self.assertTrue(all(call.kwargs['environment'] == 'prod' for call in [*calls, *pcm_calls]))
        other = self.root / 'only-test'
        ad, pcm = self.sources({'test': {'baseline': counts(), 'replacement': counts(False)}})
        with patch.object(selection.ad.cloud, 'load_settings', return_value={'username': 'synthetic'}), \
                patch.object(selection.ad, 'query_class', side_effect=ad) as query, \
                patch.object(selection.pcm, 'discover', side_effect=pcm):
            with self.assertRaises(InputError):
                selection.select_task(REQUESTS, {**self.settings, 'local_analysis': {'environment': 'test'}}, other)
        self.assertTrue(all(call.kwargs['environment'] == 'test' for call in query.call_args_list))

    def test_ad_auth_contract_or_other_error_does_not_mean_empty(self):
        errors = [cloud.CloudAuthError(operation='query', environment='test', http_status=401),
                  cloud.CloudResponseContractError(reason='data_null', operation='query',
                      environment='test', http_status=200, business_code=200), InputError('synthetic AD failure')]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__), \
                    patch.object(selection.ad.cloud, 'load_settings', return_value={'username': 'synthetic'}), \
                    patch.object(selection.ad, 'query_class', side_effect=error) as query, \
                    patch.object(selection.pcm, 'discover') as pcm:
                with self.assertRaises(type(error)):
                    selection.select_task(REQUESTS, self.settings, self.root / str(index))
                self.assertEqual(query.call_count, 1)
                self.assertEqual(query.call_args.kwargs['environment'], 'test')
                pcm.assert_not_called()

    def test_pcm_query_error_stops_selection_even_when_ad_exists(self):
        errors = [cloud.CloudAuthError(operation='query', environment='test', http_status=403),
                  cloud.CloudResponseContractError(reason='data_null', operation='query', environment='test'),
                  cloud.CloudServerError('synthetic PCM 500')]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__), \
                    patch.object(selection.ad.cloud, 'load_settings', return_value={'username': 'synthetic'}), \
                    patch.object(selection.ad, 'query_class', return_value=[{}]) as query, \
                    patch.object(selection.pcm, 'discover', side_effect=error):
                expected = InputError if isinstance(error, cloud.CloudServerError) else type(error)
                with self.assertRaises(expected):
                    selection.select_task(REQUESTS, self.settings, self.root / str(index))
                self.assertTrue(all(call.kwargs['environment'] == 'test' for call in query.call_args_list))

    def test_actual_ad400_pcm200_limits_remain_independent(self):
        task = task_fixture()
        for role in selection.ROLES:
            task['attempts'][0]['windows'][role]['counts'][selection.ad.LOG_CLASSES[0]] = 400
            task['attempts'][0]['windows'][role]['counts'][selection.pcm.LOG_CLASS] = 200
        selection.validate_task_decision(task, REQUESTS, 'test_then_prod')
        for name, number in ((selection.ad.LOG_CLASSES[0], 401), (selection.pcm.LOG_CLASS, 201)):
            invalid = deepcopy(task)
            invalid['attempts'][0]['windows']['baseline']['counts'][name] = number
            with self.subTest(log_class=name), self.assertRaises(InputError):
                selection.validate_task_decision(invalid, REQUESTS, 'test_then_prod')

    def test_no_fallback_evidence_when_both_test_windows_have_sources(self):
        task = task_fixture('prod')
        task['attempts'][0]['windows']['replacement']['counts'] = counts()
        with self.assertRaisesRegex(InputError, '禁止无依据'):
            selection.validate_task_decision(task, REQUESTS, 'test_then_prod')

    def test_old_per_window_caches_are_not_retagged_as_task_selection(self):
        directory = self.root / 'baseline'
        directory.mkdir()
        old = directory / 'environment-selection.json'
        old.write_text(json.dumps({'version': selection.VERSION}), encoding='utf-8')
        before = old.read_bytes()
        with patch.object(selection.ad.cloud, 'load_settings') as credentials, \
                patch.object(selection.ad, 'query_class') as query:
            with self.assertRaisesRegex(InputError, '不自动迁移'):
                selection.select_task(REQUESTS, self.settings, self.root)
        credentials.assert_not_called()
        query.assert_not_called()
        self.assertEqual(old.read_bytes(), before)

    def test_cached_task_is_reused_but_each_window_must_match_its_proof(self):
        (task, _), _, _ = self.select({'test': {role: counts() for role in selection.ROLES}})
        with patch.object(selection.ad.cloud, 'load_settings') as credentials:
            cached, discovery = selection.select_task(REQUESTS, self.settings, self.root)
        self.assertEqual(cached, task)
        self.assertIsNone(discovery)
        credentials.assert_not_called()
        expected = selection.bind_task_window(task, 'baseline', REQUESTS['baseline'], self.settings,
                                              self.root / 'baseline')
        self.assertEqual(expected['task_selection_sha256'], selection.task_selection_sha256(task))
        path = self.root / 'baseline/environment-selection.json'
        corrupt = deepcopy(expected)
        corrupt['environment'] = 'prod'
        path.write_text(json.dumps(corrupt), encoding='utf-8')
        with self.assertRaises(InputError):
            selection.bind_task_window(task, 'baseline', REQUESTS['baseline'], self.settings,
                                       self.root / 'baseline')

    def test_task_bound_input_preparation_cannot_reselect_a_window(self):
        task = per_window_task_fixture({role: 'prod' for role in selection.ROLES})
        discovery = {'ad': {}, 'pcm': [], 'pcm_error': False}
        with patch.object(window.selection, 'select_window') as standalone, \
                patch.object(window.ad, 'cached_result', return_value=None), \
                patch.object(window.pcm, 'cached_result', return_value=None), \
                patch.object(window.ad, 'fetch_window', return_value={'counts': {}}) as ad, \
                patch.object(window.pcm, 'prepare', return_value={'counts': {}}) as pcm, \
                patch.object(window.ad, 'digest', return_value='synthetic'):
            prepared = window.prepare_window(REQUESTS['baseline'], self.settings, self.root / 'baseline',
                self.root / 'rules', task_selection=task, role='baseline', discovery=discovery)
        standalone.assert_not_called()
        self.assertEqual(prepared.environment, 'prod')
        self.assertEqual(ad.call_args.kwargs['environment'], 'prod')
        self.assertEqual(pcm.call_args.kwargs['environment'], 'prod')

    def test_pipeline_processes_mixed_windows_in_parallel_after_both_choices(self):
        task = per_window_task_fixture({'baseline': 'test', 'replacement': 'prod'})
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        both = threading.Barrier(2, timeout=3)
        seen = []
        lock = threading.Lock()
        def process(metadata, settings, directory, rules, *, on_stage, task_selection, role, discovery):
            proof = selection.window_decision(task_selection, role)
            with lock:
                seen.append((role, proof['environment'], proof['task_selection_sha256']))
            both.wait()
            provider = proof['provider_environment']
            return {'version': window.RESULT_VERSION, 'request': metadata, 'environment': provider,
                    'environment_selection': proof, 'rules_sha256': pipeline.digest(rules), 'status': 'ok',
                    'ad': {'status': 'ok', 'environment': provider, 'source': {'environment': provider}},
                    'pcm': {'status': 'ok', 'source': {'environment': provider}},
                    'sources': window.source_plan(metadata, proof['environment'])}
        with patch.object(pipeline, 'validate_config', return_value={'window_workers': 2}), \
                patch.object(selection, 'select_task', return_value=(task, None)) as choose, \
                patch.object(pipeline, 'process_window', side_effect=process):
            result = pipeline.run_windows(REQUESTS, self.settings, self.root, rules)
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(result['environment'], 'per_window')
        self.assertEqual({value[0]:value[1] for value in seen}, {'baseline':'test','replacement':'prod'})
        self.assertEqual({value[2] for value in seen}, {selection.task_selection_sha256(task)})
        self.assertEqual(result['source_manifest']['windows']['baseline']['sources']['ad']['environment'], 'testtwo')
        self.assertEqual(result['source_manifest']['windows']['replacement']['sources']['pcm']['environment'], 'prod')
        selection.validate_task_pipeline(result, REQUESTS, 'test_then_prod')

    def test_pipeline_rejects_mixed_or_different_task_evidence(self):
        task = task_fixture()
        valid = pipeline_fixture(task)
        selection.validate_task_pipeline(valid, REQUESTS, 'test_then_prod')
        invalid = deepcopy(valid)
        invalid['windows']['replacement']['environment'] = 'prod'
        with self.assertRaisesRegex(InputError, '禁止混合'):
            selection.validate_task_pipeline(invalid, REQUESTS, 'test_then_prod')
        invalid = deepcopy(valid)
        invalid['windows']['replacement']['environment_selection']['task_selection_sha256'] = '0' * 64
        with self.assertRaises(InputError):
            selection.validate_task_pipeline(invalid, REQUESTS, 'test_then_prod')
        record = {'request': {'baseline_metadata': REQUESTS['baseline'],
                              'replacement_metadata': REQUESTS['replacement']}}
        with patch.object(worker, 'compare_performance') as compare:
            with self.assertRaises(InputError):
                worker._finalize_local_analysis(self.settings, record, self.root, self.root / 'rules', invalid)
        compare.assert_not_called()

    def test_cached_task_pipeline_rejects_detached_rules_status_and_sources(self):
        valid = pipeline_fixture(task_fixture())
        mutations = [
            lambda value: value['windows']['replacement'].update(rules_sha256='b' * 64),
            lambda value: value['windows']['replacement'].update(status='failed'),
            lambda value: value['windows']['replacement'].update(version='window-result-full-v2'),
            lambda value: value['sources']['replacement']['ad'].update(environment='prod'),
            lambda value: value['sources']['replacement']['ad'].update(
                query=selection.ad.archive_query_metadata(REQUESTS['baseline'])),
            lambda value: value['sources']['replacement']['pcm'].update(source_policy='wrong-policy'),
            lambda value: value['sources']['replacement']['ad'].update(log_classes=['wrong-class']),
        ]
        for index, mutate in enumerate(mutations):
            invalid = deepcopy(valid)
            mutate(invalid)
            with self.subTest(mutation=index), self.assertRaises(InputError):
                selection.validate_cached_pipeline(invalid, REQUESTS, 'test_then_prod')
        fresh = deepcopy(valid)
        for role in selection.ROLES:
            fresh['windows'][role]['sources'] = fresh['sources'][role]
        del fresh['sources']
        selection.validate_cached_pipeline(fresh, REQUESTS, 'test_then_prod')
        both_positions = deepcopy(fresh)
        both_positions['sources'] = deepcopy(valid['sources'])
        both_positions['windows']['replacement']['sources']['ad']['environment'] = 'prod'
        with self.assertRaises(InputError):
            selection.validate_cached_pipeline(both_positions, REQUESTS, 'test_then_prod')
        both_positions = deepcopy(fresh)
        both_positions['sources'] = deepcopy(valid['sources'])
        both_positions['sources']['replacement']['pcm']['environment'] = 'prod'
        with self.assertRaises(InputError):
            selection.validate_cached_pipeline(both_positions, REQUESTS, 'test_then_prod')

    def test_cached_task_pipeline_requires_one_complete_source_position(self):
        invalid = pipeline_fixture(task_fixture())
        del invalid['sources']
        with self.assertRaises(InputError):
            selection.validate_cached_pipeline(invalid, REQUESTS, 'test_then_prod')
        invalid = pipeline_fixture(task_fixture())
        invalid['sources'] = None
        with self.assertRaises(InputError):
            selection.validate_cached_pipeline(invalid, REQUESTS, 'test_then_prod')

    def test_old_pipeline_manifest_is_preserved_and_rejected_before_query(self):
        rules = self.root / 'rules.json'
        rules.write_text('{}', encoding='utf-8')
        old = self.root / 'pipeline-manifest.json'
        old.write_text(json.dumps({'version': 'cloud-window-full-v5', 'request': REQUESTS,
            'rules_sha256': pipeline.digest(rules), 'environment_policy': 'test_then_prod'}), encoding='utf-8')
        before = old.read_bytes()
        with patch.object(pipeline, 'validate_config', return_value={'window_workers': 2}), \
                patch.object(selection, 'select_task') as choose, \
                patch.object(pipeline, 'process_window') as process:
            with self.assertRaises(InputError):
                pipeline.run_windows(REQUESTS, self.settings, self.root, rules)
        choose.assert_not_called()
        process.assert_not_called()
        self.assertEqual(old.read_bytes(), before)

    def test_old_final_is_not_requeried_recomputed_or_published(self):
        path = self.root / 'synthetic-task.json'
        result_path = path.with_suffix('.result.json')
        result_path.write_text(json.dumps({'vin': REQUESTS['baseline']['vin'], 'request_sha256': 'synthetic',
            'source_policy': worker.SOURCE_POLICY, 'delivery_format': worker.DELIVERY_FORMAT,
            'pipeline': {'version': 'cloud-window-full-v5', 'environment': 'per_window'}}), encoding='utf-8')
        result_before = result_path.read_bytes()
        record = {'kind': 'walkthrough_test_local', 'status': 'failed',
                  'report_operator_open_id': 'synthetic-user', 'vin': REQUESTS['baseline']['vin'],
                  'request_sha256': 'synthetic', 'result_sha256': worker.digest(result_path),
                  'request': {'baseline_metadata': REQUESTS['baseline'],
                              'replacement_metadata': REQUESTS['replacement']}}
        path.write_text(json.dumps(record), encoding='utf-8')
        settings = {**self.settings, 'runtime_state_dir': str(self.root)}
        with patch.object(worker, 'load_settings', return_value=settings), \
                patch.object(worker.policy, 'activate_lark_environment'), \
                patch.object(worker.policy, 'ensure_user_authorization'), \
                patch.object(worker, 'analyze_task') as analyze, patch.object(worker, 'publish') as publish:
            with self.assertRaisesRegex(InputError, '不迁移或重算'):
                worker.run_worker(path, 'unused')
        analyze.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(result_path.read_bytes(), result_before)

    def test_legacy_same_environment_proof_is_validated_without_retagging(self):
        for env in ('test', 'prod'):
            with self.subTest(environment=env):
                legacy = legacy_fixture({role: env for role in selection.ROLES})
                before = deepcopy(legacy)
                self.assertIs(selection.validate_cached_pipeline(legacy, REQUESTS, 'test_then_prod'), legacy)
                self.assertEqual(legacy, before)
                self.assertNotIn('task_environment_selection', legacy)
        mixed = legacy_fixture({'baseline': 'test', 'replacement': 'prod'})
        with self.assertRaisesRegex(InputError, '混合了两种环境'):
            selection.validate_cached_pipeline(mixed, REQUESTS, 'test_then_prod')
        tampered = legacy_fixture()
        tampered['sources']['replacement']['ad']['query']['end'] = '2026-10-23T15:00:00'
        with self.assertRaises(InputError):
            selection.validate_cached_pipeline(tampered, REQUESTS, 'test_then_prod')

    def test_default_prod_first_selects_each_window_without_querying_unneeded_fallback(self):
        settings = {**self.settings, 'local_analysis': {}}
        (task, _), ad_calls, pcm_calls = self.select({
            'prod': {'baseline': counts(False), 'replacement': counts()},
            'test': {'baseline': counts()}}, settings=settings)
        self.assertEqual(task['policy'], 'prod_then_test')
        self.assertEqual({role: choice['environment'] for role, choice in task['windows'].items()},
                         {'baseline': 'test', 'replacement': 'prod'})
        self.assertEqual([call.kwargs['environment'] for call in ad_calls],
                         ['prod'] * 4 + ['test'] * 4 + ['prod'] * 4)
        self.assertEqual([call.kwargs['environment'] for call in pcm_calls], ['prod', 'test', 'prod'])
        for role in selection.ROLES:
            self.assertEqual(task['windows'][role]['request'], REQUESTS[role])
        selection.validate_task_decision(task, REQUESTS, 'prod_then_test')

    def test_default_prod_first_also_supports_opposite_environment_mapping(self):
        settings = {**self.settings, 'local_analysis': {}}
        (task, _), _, _ = self.select({
            'prod': {'baseline': counts(), 'replacement': counts(False)},
            'test': {'replacement': counts()}}, settings=settings)
        self.assertEqual({role: choice['environment'] for role, choice in task['windows'].items()},
                         {'baseline': 'prod', 'replacement': 'test'})

    def test_new_mixed_final_and_cached_selection_are_preserved_and_verified_without_network(self):
        task = per_window_task_fixture({'baseline': 'test', 'replacement': 'prod'}, policy='prod_then_test')
        path = self.root / 'task-environment-selection.json'
        path.write_text(json.dumps(task), encoding='utf-8')
        before = path.read_bytes()
        settings = {**self.settings, 'local_analysis': {'environment': 'prod_then_test'}}
        with patch.object(selection.ad.cloud, 'load_settings') as credentials, \
                patch.object(selection.ad, 'query_class') as query, \
                patch.object(selection.pcm, 'discover') as pcm_query:
            saved, discovery = selection.select_task(REQUESTS, settings, self.root)
        self.assertEqual(saved, task)
        self.assertIsNone(discovery)
        credentials.assert_not_called()
        query.assert_not_called()
        pcm_query.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        final = per_window_pipeline_fixture(task)
        frozen = deepcopy(final)
        self.assertIs(selection.validate_cached_pipeline(final, REQUESTS, 'prod_then_test'), final)
        self.assertEqual(final, frozen)

    def test_new_mixed_final_rejects_role_environment_policy_source_and_rules_tampering(self):
        valid = per_window_pipeline_fixture(per_window_task_fixture(
            {'baseline': 'test', 'replacement': 'prod'}, policy='prod_then_test'))
        mutations = [
            lambda value: value['windows']['replacement'].update(environment='testtwo'),
            lambda value: value['windows']['baseline'].update(request=deepcopy(REQUESTS['replacement'])),
            lambda value: value['windows']['baseline']['environment_selection'].update(role='replacement'),
            lambda value: value['windows']['baseline']['environment_selection'].update(task_selection_sha256='0' * 64),
            lambda value: value['windows']['baseline'].update(rules_sha256='b' * 64),
            lambda value: value['sources']['replacement']['pcm'].update(environment='testtwo'),
            lambda value: value['task_environment_selection'].update(policy='test_then_prod'),
            lambda value: value['task_environment_selection']['windows']['baseline']['attempts'][0].update(pcm_status='failed'),
            lambda value: value['task_environment_selection']['windows']['baseline']['attempts'][0].update(counts=counts()),
            lambda value: value.update(version=selection.TASK_PIPELINE_VERSION),
        ]
        for index, mutate in enumerate(mutations):
            invalid = deepcopy(valid)
            mutate(invalid)
            before = deepcopy(invalid)
            with self.subTest(mutation=index), self.assertRaises(InputError):
                selection.validate_cached_pipeline(invalid, REQUESTS, 'prod_then_test')
            self.assertEqual(invalid, before)

    def test_new_computation_never_reuses_a_legacy_task_selection(self):
        path = self.root / 'task-environment-selection.json'
        path.write_text(json.dumps(task_fixture()), encoding='utf-8')
        before = path.read_bytes()
        with patch.object(selection.ad.cloud, 'load_settings') as credentials, \
                patch.object(selection.ad, 'query_class') as query:
            with self.assertRaisesRegex(InputError, '不自动迁移'):
                selection.select_task(REQUESTS, self.settings, self.root)
        credentials.assert_not_called()
        query.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_resumed_pcm_query_failure_is_fatal_and_never_hidden_as_partial(self):
        task = per_window_task_fixture({'baseline': 'prod', 'replacement': 'test'}, policy='prod_then_test')
        settings = {**self.settings, 'local_analysis': {'environment': 'prod_then_test'}}
        for index, error in enumerate((TimeoutError('sensitive-url'),
                cloud.CloudAuthError(operation='query', environment='prod', http_status=401),
                cloud.CloudResponseContractError(reason='data_null', operation='query', environment='prod'))):
            with self.subTest(error=type(error).__name__), \
                    patch.object(window.ad, 'cached_result', return_value={'download': {'counts': {}}}), \
                    patch.object(window.pcm, 'cached_result', return_value=None), \
                    patch.object(window.pcm, 'discover', side_effect=error) as query, \
                    patch.object(window.pcm, 'prepare') as prepare, \
                    patch.object(window.ad, 'digest', return_value='synthetic'):
                expected = InputError if isinstance(error, TimeoutError) else type(error)
                with self.assertRaises(expected) as caught:
                    window.prepare_window(REQUESTS['baseline'], settings, self.root / str(index), self.root / 'rules',
                                          task_selection=task, role='baseline')
                self.assertNotIn('sensitive-url', str(caught.exception))
                self.assertEqual(query.call_args.kwargs['environment'], 'prod')
                prepare.assert_not_called()

    def test_legacy_same_environment_final_can_resume_delivery_without_recomputation(self):
        path = self.root / 'legacy-final-task.json'
        result_path = path.with_suffix('.result.json')
        result = {'vin': REQUESTS['baseline']['vin'], 'request_sha256': 'synthetic',
                  'source_policy': worker.SOURCE_POLICY, 'delivery_format': worker.DELIVERY_FORMAT,
                  'pipeline': legacy_fixture()}
        result_path.write_text(json.dumps(result), encoding='utf-8')
        before = result_path.read_bytes()
        path.write_text(json.dumps({'kind': 'walkthrough_test_local', 'status': 'failed',
            'job_key': 'synthetic-legacy',
            'report_operator_open_id': 'synthetic-user', 'vin': result['vin'],
            'request_sha256': 'synthetic', 'result_sha256': worker.digest(result_path),
            'request': {'vin': result['vin'], 'baseline_metadata': REQUESTS['baseline'],
                        'replacement_metadata': REQUESTS['replacement']}}), encoding='utf-8')
        settings = {**self.settings, 'runtime_state_dir': str(self.root)}
        with patch.object(worker, 'load_settings', return_value=settings), \
                patch.object(worker.policy, 'activate_lark_environment'), \
                patch.object(worker.policy, 'ensure_user_authorization'), \
                patch.object(worker.product, 'validate_result'), \
                patch.object(worker, 'analyze_task') as analyze, patch.object(worker, 'publish') as publish:
            self.assertEqual(worker.run_worker(path, 'unused'), 0)
        analyze.assert_not_called()
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.args[3], result)
        self.assertEqual(result_path.read_bytes(), before)
        self.assertFalse((path.with_suffix('.work') / 'task-environment-selection.json').exists())


if __name__ == '__main__':
    unittest.main()
