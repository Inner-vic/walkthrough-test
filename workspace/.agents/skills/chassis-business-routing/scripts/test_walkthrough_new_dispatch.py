"""New chain wins priority without changing existing route decisions."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import intent_router

TEXT = 'DEM0VEH1CLE000001,2026-11-02 17:29-18:04\nDEM0VEH1CLE000001,2026-11-02 18:30-19:03'
ANCHOR = datetime.fromisoformat('2026-11-02T20:00:00+08:00')


class NewDispatchTests(unittest.TestCase):
    def test_suspended_new_name_never_dispatches_or_falls_back(self):
        for trigger in ('走查测试New','走查测试 New','走查测试 nEw'):
            with self.subTest(trigger=trigger),patch.object(intent_router.subprocess,'run') as run:
                result=intent_router.route(trigger+' 时延分析 日志 '+TEXT,ANCHOR)
                self.assertEqual((result['route'],result['status']),('none','no_match'))
                self.assertIn('停用',result['message'])
                run.assert_not_called()

    def test_suspended_new_ignores_business_fields_and_interpretation(self):
        for text in ('走查测试New','走查测试New '+TEXT):
            with patch.object(intent_router.subprocess,'run') as run:
                result=intent_router.route(text,ANCHOR,interpretation={'invalid':'proposal'})
            self.assertEqual(result['missing'],[])
            self.assertEqual(result['normalized'],{})
            run.assert_not_called()

    def test_original_walkthrough_entry_unchanged(self):
        output = {'route': 'walkthrough-test', 'status': 'ready', 'message':'ok'}
        with patch.object(intent_router.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps(output), '')) as run:
            result = intent_router.route('走查测试 '+TEXT, ANCHOR)
        self.assertEqual(result['route'], 'walkthrough-test')
        self.assertTrue(any(str(item).endswith('walkthrough_route.py') for item in run.call_args.args[0]))

    def test_original_latency_uroad_and_nonbusiness_unchanged(self):
        self.assertEqual(intent_router.route('时延分析 '+TEXT, ANCHOR)['route'], 'ad-p99-asgard')
        self.assertEqual(intent_router.route('日志 DEM0VEH1CLE000001 2026-11-02 17:29-18:04', ANCHOR)['route'], 'uroad')
        self.assertEqual(intent_router.route('你好', ANCHOR)['route'], 'none')


if __name__ == '__main__':
    unittest.main()
