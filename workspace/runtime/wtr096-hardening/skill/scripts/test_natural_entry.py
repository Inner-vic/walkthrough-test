"""Natural input and exact-source clock recovery; no cloud or Feishu IO."""
import argparse
from copy import deepcopy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import walkthrough_route as routing
import walkthrough_worker as worker


VIN = 'DEM0VEH1CLEX00001'
CONTEXT = ('[cc-connect request_context_v=1 sender_id=ou_synthetic platform=feishu '
           'chat_id=oc_synthetic message_id=om_synthetic thread_id=]')
ANCHOR = '2026-10-23T23:59:59+08:00'
TEXT = f'走查测试 {VIN}，昨天15点48到16点22；昨天14点50到15点24'


class NaturalEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = {'workspace_root':str(self.root),'runtime_state_dir':str(self.root/'runtime')}
        self.identity = routing.request_identity(CONTEXT)
        self.key = 'walkthrough-'+hashlib.sha256(self.identity['source_message_id'].encode()).hexdigest()[:24]
        self.path = Path(self.settings['runtime_state_dir'])/(self.key+'.json')

    def save(self, request, **extra):
        material = json.dumps([self.identity,request['baseline_metadata'],request['replacement_metadata']],sort_keys=True)
        record = {'job_key':self.key,'kind':'walkthrough_test_local','status':'completed',
                  'request_sha256':hashlib.sha256(material.encode()).hexdigest(),
                  'request':request,'created_at':ANCHOR,**self.identity,**extra}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
        return record

    def test_fullwidth_punctuation_preserves_original_text_and_default_roles(self):
        text = f'走查测试 {VIN}，2026年10月21日 15：48 到 16：22；2026年10月21日 14：50 到 15：24'
        decision = routing.route(text,received_at=ANCHOR)
        self.assertEqual(decision['status'],'ready',decision)
        request = decision['normalized']
        self.assertEqual(request['baseline_metadata']['start'],'2026-10-21T15:48:00')
        self.assertEqual(request['replacement_metadata']['start'],'2026-10-21T14:50:00')
        self.assertEqual(request['input_roles'],['baseline','replacement'])
        self.assertEqual(request['normalization']['original_text'],text)
        self.assertTrue(request['normalization']['received_at'])

    def test_relative_spoken_time_and_explicit_roles_are_preserved(self):
        decision = routing.route(TEXT.replace(VIN,VIN+' PCM更新',1),received_at=ANCHOR)
        self.assertEqual(decision['status'],'ready',decision)
        request = decision['normalized']
        self.assertEqual(request['replacement_metadata']['start'],'2026-10-22T15:48:00')
        self.assertEqual(request['baseline_metadata']['start'],'2026-10-22T14:50:00')
        self.assertEqual(request['role_assignment_source'],'explicit')

    def test_no_date_and_invalid_calendar_require_input(self):
        for text in (f'走查测试 {VIN} 下午3点到4点，下午1点到2点',
                     f'走查测试 {VIN} 2026年2月30日 15：48 到16：22，2026年2月30日14：50 到15：24'):
            with self.subTest(text=text):
                decision = routing.route(text,received_at=ANCHOR)
                self.assertEqual(decision['status'],'needs_input',decision)
                self.assertTrue(decision['missing'])

    def test_shared_normalizer_loaded_only_from_absolute_sibling(self):
        module = routing._natural_input_module()
        workspace = next(p for p in Path(__file__).resolve().parents
                         if (p/'AGENTS.md').is_file() and (p/'.agents/skills/chassis-business-routing').is_dir())
        expected = workspace/'.agents/skills/chassis-business-routing/scripts/natural_input.py'
        self.assertEqual(Path(module.__file__).resolve(),expected.resolve())

    def test_next_day_duplicate_uses_original_clock_without_process_or_auth(self):
        request = routing.route(TEXT,received_at=ANCHOR)['normalized']
        saved = self.save(request)
        before = self.path.read_bytes()
        args = argparse.Namespace(text=TEXT,ccconnect_context=CONTEXT,
                                  settings=self.root/'settings.json',received_at='2026-10-24T00:00:01+08:00')
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker,'load_settings',return_value=self.settings))
            auth = stack.enter_context(patch.object(worker.policy,'ensure_user_authorization',side_effect=AssertionError('auth')))
            launch = stack.enter_context(patch.object(worker.subprocess,'Popen',side_effect=AssertionError('launch')))
            decision = worker.start_job(args)
        self.assertEqual(decision['status'],'completed')
        self.assertTrue(decision['idempotent'])
        auth.assert_not_called()
        launch.assert_not_called()
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(saved['request']['normalization']['original_text'],TEXT)

    def test_source_anchor_rejects_same_message_cross_user(self):
        request = routing.route(TEXT,received_at=ANCHOR)['normalized']
        self.save(request)
        with self.assertRaises(worker.InputError):
            routing.source_normalization_anchor(self.settings,CONTEXT.replace('ou_synthetic','ou_other'))

    def test_legacy_anchor_is_created_at_not_replay_time(self):
        request = routing.route(TEXT,received_at=ANCHOR)['normalized']
        request.pop('normalization')
        self.save(request)
        self.assertEqual(routing.source_normalization_anchor(self.settings,CONTEXT,'2026-10-24T00:01:00+08:00'),ANCHOR)

    def test_ready_start_keeps_raw_text_and_fixed_host_clock_in_audit(self):
        args = argparse.Namespace(text=TEXT,ccconnect_context=CONTEXT,
                                  settings=self.root/'settings.json',received_at=ANCHOR)
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker,'load_settings',return_value=self.settings))
            for name in ('activate_lark_environment','ensure_report_bot_authorization','ensure_secure_label_authorization'):
                stack.enter_context(patch.object(worker.policy,name))
            stack.enter_context(patch.object(worker.policy,'ensure_user_authorization',return_value={'open_id':'ou_operator'}))
            stack.enter_context(patch.object(worker.policy,'resolve_report_secure_label',return_value={'id':'synthetic'}))
            launch = stack.enter_context(patch.object(worker.subprocess,'Popen',return_value=Mock()))
            result = worker.start_job(args)
        self.assertEqual(result['status'],'queued')
        record = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(record['request']['normalization']['original_text'],TEXT)
        self.assertEqual(record['request']['normalization']['received_at'],
                         routing.route(TEXT,received_at=ANCHOR)['normalized']['normalization']['received_at'])
        self.assertEqual(args.text,TEXT)
        self.assertEqual(args.ccconnect_context,CONTEXT)
        launch.assert_called_once()

    @staticmethod
    def semantic_request():
        first = '昨天下午，开始15：48，结束16：22'
        second = '昨天下午，开始14：50，结束15：24'
        text = f'走查测试 {VIN}，Base：{first}；PCM更新：{second}'
        proposal = {'source_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                    'received_at':ANCHOR,'windows':[
                        {'quote':first,'start':'2026-10-22T15:48:00+08:00','end':'2026-10-22T16:22:00+08:00'},
                        {'quote':second,'start':'2026-10-22T14:50:00+08:00','end':'2026-10-22T15:24:00+08:00'}]}
        return text, proposal

    def test_ai_quote_based_free_sentence_keeps_roles_and_audit(self):
        text, proposal = self.semantic_request()
        self.assertEqual(routing.route(text,received_at=ANCHOR)['status'],'needs_input')
        decision = routing.route(text,received_at=ANCHOR,interpretation=proposal)
        self.assertEqual(decision['status'],'ready',decision)
        request = decision['normalized']
        self.assertEqual(request['baseline_metadata']['start'],'2026-10-22T15:48:00')
        self.assertEqual(request['replacement_metadata']['start'],'2026-10-22T14:50:00')
        self.assertEqual(request['role_assignment_source'],'explicit')
        self.assertEqual(request['normalization']['original_text'],text)
        self.assertEqual(request['normalization']['interpretation'],proposal)

    def test_ai_invented_time_role_rewrite_or_clock_drift_require_confirmation(self):
        text, proposal = self.semantic_request()
        candidates = []
        changed = deepcopy(proposal)
        changed['windows'][0]['start'] = '2026-10-22T15:49:00+08:00'
        candidates.append(changed)
        changed = deepcopy(proposal)
        changed['windows'][0]['quote'] = 'Base：'+changed['windows'][0]['quote']
        candidates.append(changed)
        changed = deepcopy(proposal)
        changed['received_at'] = '2026-10-24T00:00:01+08:00'
        candidates.append(changed)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                decision = routing.route(text,received_at=ANCHOR,interpretation=candidate)
                self.assertEqual(decision['status'],'needs_input',decision)
                self.assertTrue(decision['missing'])

    def test_ai_same_source_next_day_replay_reuses_bound_proposal_clock(self):
        text, proposal = self.semantic_request()
        request = routing.route(text,received_at=ANCHOR,interpretation=proposal)['normalized']
        self.save(request)
        before = self.path.read_bytes()
        args = argparse.Namespace(text=text,ccconnect_context=CONTEXT,settings=self.root/'settings.json',
                                  received_at='2026-10-24T00:00:01+08:00',interpretation=proposal)
        with patch.object(worker,'load_settings',return_value=self.settings), \
                patch.object(worker.subprocess,'Popen',side_effect=AssertionError('relaunch')):
            result = worker.start_job(args)
        self.assertEqual(result['status'],'completed')
        self.assertTrue(result['idempotent'])
        self.assertEqual(self.path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
