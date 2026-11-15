"""Verify mixed sources through final-data binding and report rendering, offline."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import environment_selection as selection
from feishu_report import _environment_evidence
from native_sheet_data import validate_native_sheet_data
from request_model import InputError
import test_environment_report as legacy_report_fixture
from test_environment_report import build_report_document
from test_task_environment_selection import per_window_task_fixture


class PerWindowReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy_report_fixture.Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        requests = {role: f.request[role + '_metadata'] for role in selection.ROLES}
        task = per_window_task_fixture({'baseline': 'test', 'replacement': 'prod'},
                                      policy='prod_then_test', requests=requests)
        f.settings['local_analysis']['environment'] = task['policy']
        f.pipeline.update(version=selection.PER_WINDOW_PIPELINE_VERSION,
            environment='per_window', environment_policy=task['policy'],
            task_environment_selection=task,
            task_selection_sha256=selection.task_selection_sha256(task))
        import window_pipeline
        for role, window in f.pipeline['windows'].items():
            proof = selection.window_decision(task, role)
            actual = proof['provider_environment']
            window.update(environment=actual, environment_selection=proof,
                          sources=window_pipeline.source_plan(window['request'], proof['environment']))
            window['ad']['environment'] = actual
            window['ad']['source']['environment'] = actual
            window['pcm']['source']['environment'] = actual

    def test_mixed_sources_finalize_render_and_bind_each_native_detail(self):
        result = self.fixture.result()
        self.assertEqual(result['pipeline']['environment'], 'per_window')
        for role, expected in [('baseline', 'testtwo'), ('replacement', 'prod')]:
            self.assertEqual(result['pipeline']['windows'][role]['environment'], expected)
            payload = json.loads(Path(result['native_sheet_data'][role]['path']).read_text(encoding='utf-8'))
            validate_native_sheet_data(payload)
            self.assertEqual(payload['metadata']['environment'], expected)
        document = build_report_document(result)
        self.assertEqual(len(document['source_links']), 2)

    def test_fallback_reason_is_specific_to_the_window(self):
        result = self.fixture.result()
        actual, note = _environment_evidence(result['pipeline'], 'baseline', {})
        self.assertEqual(actual, 'testtwo')
        self.assertIn('prod', note)
        self.assertIn('五类所需日志候选均为空', note)
        actual, note = _environment_evidence(result['pipeline'], 'replacement', {})
        self.assertEqual(actual, 'prod')
        self.assertNotIn('回退原因', note)
        self.assertNotIn('统一', note)

    def test_changed_role_source_is_rejected_before_rendering(self):
        result = self.fixture.result()
        result['pipeline']['windows']['baseline']['environment'] = 'prod'
        with self.assertRaises(InputError):
            build_report_document(result)

    def test_failed_query_cannot_support_a_fallback_report(self):
        result = self.fixture.result()
        task = result['pipeline']['task_environment_selection']
        task['windows']['baseline']['attempts'][0]['pcm_status'] = 'failed'
        with self.assertRaises(InputError):
            build_report_document(result)

    def test_unproven_mixed_legacy_sources_still_fail(self):
        result = self.fixture.result()
        result['pipeline'].pop('task_environment_selection')
        with self.assertRaises(InputError):
            build_report_document(result)


if __name__ == '__main__':
    unittest.main()
