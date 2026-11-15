"""Extraction-warning delivery contract; no live data, network or Feishu writes."""
import contextlib
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import walkthrough_worker as worker
from feishu_report import (METRICS, SCENARIOS, build_report_document,
                           build_report_notification, unparsed_metric_groups)
from performance_comparison import UNPARSED_LABEL
from request_model import InputError


def comparison(*, empty=False):
    pair = ({'baseline':None,'replacement':None,'baseline_status':'empty',
             'replacement_status':'empty','status':'missing','tone':'neutral','delta':None}
            if empty else
            {'baseline':1,'replacement':1,'baseline_status':'value',
             'replacement_status':'value','status':'ok','tone':'neutral','delta':0})
    return {'status':'partial' if empty else 'ok','comparisons':{
        scenario:{'metrics':{metric:copy.deepcopy(pair) for metric,_ in METRICS}}
        for scenario in SCENARIOS}}


class NotificationTests(unittest.TestCase):
    URL = 'https://example.feishu.cn/docx/OfflineReport'
    VIN = 'DEM0VEH1CLE000001'
    SETTINGS = {'report_lark_profile':'offline-report-bot'}

    def notification(self, result):
        return build_report_notification(result,vin=self.VIN,url=self.URL)['zh_cn']

    def test_confirmed_empty_only_has_report_link_and_ascii_dashes(self):
        result=comparison(empty=True)
        self.assertEqual(unparsed_metric_groups(result),[])
        notification=self.notification(result)
        self.assertEqual(len(notification['content']),2)
        self.assertNotIn('未解析',json.dumps(notification,ensure_ascii=False))
        xml=build_report_document(result)['xml']
        self.assertNotIn(UNPARSED_LABEL,xml)
        self.assertEqual(xml.count('>-</p>'),48)

    def test_mixed_only_unparsed_sides_scenarios_metrics_are_announced(self):
        result=comparison(empty=True)
        pair=result['comparisons']['parking']['metrics']['pcm_frame_rate_hz']
        pair['replacement_status']='unparsed'
        pair=result['comparisons']['slow_driver']['metrics']['p99_infer_delay_ms']
        pair['baseline_status']='unparsed'
        self.assertEqual(unparsed_metric_groups(result),[
            {'side':'Base','scenario':'低速人驾','metrics':['AD P99 Infer Delay']},
            {'side':'PCM更新','scenario':'泊车','metrics':['PCM Frame Rate']}])
        notification=self.notification(result)
        text=json.dumps(notification,ensure_ascii=False)
        self.assertIn('Base · 低速人驾：AD P99 Infer Delay',text)
        self.assertIn('PCM更新 · 泊车：PCM Frame Rate',text)
        self.assertNotIn('Sys CPU Usage',text)
        self.assertNotIn('高速人驾',text)
        # The warning still exists in its original table cells, not a paragraph.
        xml=build_report_document(result)['xml']
        self.assertEqual(xml.count(UNPARSED_LABEL),2)
        self.assertEqual(xml.count('>-</p>'),46)

    def test_valid_zero_is_value_even_with_contradictory_old_status(self):
        result=comparison()
        for row in result['comparisons'].values():
            for pair in row['metrics'].values():
                pair.update(baseline=0,replacement=0,baseline_status='unparsed',replacement_status='empty')
        self.assertEqual(unparsed_metric_groups(result),[])
        self.assertEqual(len(self.notification(result)['content']),2)

    def test_legacy_missing_side_status_is_unknown_not_confirmed_empty(self):
        result=comparison()
        pair=result['comparisons']['CNOA']['metrics']['system_cpu_percent']
        pair.update(baseline=None,status='missing')
        pair.pop('baseline_status')
        self.assertEqual(unparsed_metric_groups(result),[
            {'side':'Base','scenario':'CNOA','metrics':['Sys CPU Usage']}])
        self.assertIn(UNPARSED_LABEL,build_report_document(result)['xml'])

    def test_missing_or_malformed_comparison_slots_remain_unknown(self):
        for result in ({'status':'partial'}, {'comparisons':None}, {'comparisons':[]}):
            with self.subTest(result=result):
                groups=unparsed_metric_groups(result)
                self.assertEqual(len(groups),8)
                self.assertEqual(sum(len(g['metrics']) for g in groups),48)
        result=comparison()
        result['comparisons']['parking']['metrics']['pcm_cpu_percent']={'baseline':True,'replacement':float('nan')}
        self.assertEqual(unparsed_metric_groups(result),[
            {'side':'Base','scenario':'泊车','metrics':['PCM CPU Usage']},
            {'side':'PCM更新','scenario':'泊车','metrics':['PCM CPU Usage']}])

    def test_notice_never_forwards_arbitrary_issue_error_path_or_labels(self):
        result=comparison()
        result['comparisons']['parking'].update(label='D:/private/secrets malicious label')
        result['comparisons']['parking']['metrics']['pcm_cpu_percent'].update(
            baseline=None,baseline_status='unparsed')
        result['comparisons']['private-path']={'metrics':{'credential':{'baseline':None}}}
        result.update(error='https://private.invalid/?token=secret',issues=[{
            'kind':'field_unparsed','side':'replacement','scenario':'unknown',
            'metric':'private-key','message':'D:/private/secrets raw log'}])
        text=json.dumps(self.notification(result),ensure_ascii=False)
        self.assertIn('Base · 泊车：PCM CPU Usage',text)
        for forbidden in ('private','secret','raw log','credential','replacement'):
            self.assertNotIn(forbidden,text)

    def patches(self,stack):
        stack.enter_context(patch.object(worker.policy,'ensure_report_bot_authorization'))
        stack.enter_context(patch.object(worker.policy,'configure_report_secure_label'))
        stack.enter_context(patch.object(worker,'create_report',side_effect=AssertionError('must reuse report')))
        return stack.enter_context(patch.object(worker.policy,'run_lark',return_value={
            'ok':True,'data':{'message_id':'om_reportreply'}}))

    def record(self):
        return {'report_url':self.URL,'source_chat_id':'oc_source','sender_id':'ou_requester',
                'source_message_id':'om_original','source_thread_id':'om_otherthread',
                'vin':self.VIN,'status':'failed','report_permissions':{
                    'collaborator_management':True,'source_chat_view':True,'owner_transfer':True}}

    def test_one_post_contains_link_and_warning_without_extra_send(self):
        result=comparison(empty=True)
        result['comparisons']['parking']['metrics']['pcm_cpu_percent']['baseline_status']='unparsed'
        record=self.record()
        with tempfile.TemporaryDirectory() as directory,contextlib.ExitStack() as stack:
            send=self.patches(stack)
            worker.publish(self.SETTINGS,Path(directory)/'offline-task.json',record,result)
        send.assert_called_once()
        args=send.call_args.args[1]
        content=json.loads(args[args.index('--content')+1])
        self.assertEqual(content,build_report_notification(result,vin=self.VIN,url=self.URL))
        self.assertIn('Base · 泊车：PCM CPU Usage',json.dumps(content,ensure_ascii=False))
        self.assertNotIn('--reply-in-thread',args)
        self.assertEqual(args[args.index('--message-id')+1],'om_original')
        self.assertEqual(record['report_reply_message_id'],'om_reportreply')
        self.assertEqual(record['source_thread_id'],'om_otherthread')
        self.assertEqual(record['status'],'completed')

    def test_retry_same_post_same_stable_source_key_not_separate_warning_message(self):
        result=comparison(empty=True)
        result['comparisons']['parking']['metrics']['pcm_cpu_percent']['replacement_status']='unparsed'
        record=self.record()
        with tempfile.TemporaryDirectory() as directory,contextlib.ExitStack() as stack:
            send=self.patches(stack)
            send.side_effect=[InputError('temporary delivery failure'),{'ok':True,'data':{'message_id':'om_reportreply'}}]
            path=Path(directory)/'offline-task.json'
            with self.assertRaises(InputError):
                worker.publish(self.SETTINGS,path,record,result)
            worker.publish(self.SETTINGS,path,record,result)
        self.assertEqual(send.call_count,2)  # One report attempt per retry, never a warning-only send.
        first,second=[call.args[1] for call in send.call_args_list]
        self.assertEqual(first,second)
        expected=hashlib.sha256(b'om_original:walkthrough-report').hexdigest()[:32]
        self.assertEqual(first[first.index('--idempotency-key')+1],expected)


if __name__=='__main__':
    unittest.main()
