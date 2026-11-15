"""Synthetic cache/source identity regressions; no analyzer or network calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import native_log_source as native
from m100_attachment_worker import full_analysis_scope
from request_model import InputError
from test_source_selection_fixture import synthetic_selection_audit, attach_synthetic_execution


class NativeCacheIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.metadata = {'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:48:00',
                         'end':'2026-10-21T16:22:00'}
        self.rules = self.root/'rules.json'
        rule_data = {'synthetic':{name:{'soc_type':'synthetic'} for name in
                                 ('header_delay','vlamodel_fps','extra_metric')}}
        native.write_json(self.rules,rule_data)
        self.report = {
            'status':'partial',
            'source_selection':synthetic_selection_audit(),
            'metadata':{'vin':self.metadata['vin'],'time_range':'20261021154800/20261021162200'},
            'analysis_scope':full_analysis_scope(rule_data),
            'metrics':{'slow_driver':{'label':'synthetic','p99_ms':5.0,'frame_rate_hz':10.0},
                       'parking':{'label':'missing synthetic parking','p99_ms':None,'frame_rate_hz':None}},
            'statistics':[{'theme':'synthetic','metric':'header_delay','statistics':{'P99_0':5.0}}]}
        attach_synthetic_execution(self.report,rule_data)
        self.report['statistics'] = [
            {'theme':theme,'metric':name,'rule':fields,'native_publish':True,
             'status':'available','statistics':({'P99_0':5.0} if name=='header_delay' else {'mean':1.0})}
            for theme,metrics in rule_data.items() for name,fields in metrics.items()]
        from native_sheet_test_fixture import synthetic_native_sheet_payload
        self.report['native_sheet_data'] = synthetic_native_sheet_payload(
            self.report,rule_data,self.metadata,native.digest(self.rules))
        self.detail = self.root/'report-data.json'
        native.write_json(self.detail,self.report)
        html = self.root/'native-report.html'
        html.write_text('<html>Synthetic cache</html>',encoding='utf-8')
        self.cache = {
            'status':self.report['status'],'request':copy.deepcopy(self.metadata),
            'rules_sha256':native.digest(self.rules),'environment':'testtwo',
            'source_policy':native.SOURCE_POLICY,
            'analysis_scope':copy.deepcopy(self.report['analysis_scope']),
            'source_selection':copy.deepcopy(self.report['source_selection']),
            'runtime_execution':copy.deepcopy(self.report['runtime_execution']),
            'analyzer_contract':copy.deepcopy(self.report['analyzer_contract']),
            'execution_compat':copy.deepcopy(self.report['execution_compat']),
            'execution_compat_runtime':copy.deepcopy(self.report['execution_compat_runtime']),
            'source':{'environment':'testtwo','sha256':native.digest(self.detail)},
            'artifact':{'path':str(html),'sha256':native.digest(html)},
            'full_statistics':{'path':str(self.detail),'sha256':native.digest(self.detail),
                               'bytes':self.detail.stat().st_size},
            'metrics':{key:{**row,'status':'ok' if row['p99_ms'] is not None else 'missing',
                            'frame_rate_status':'ok' if row['frame_rate_hz'] is not None else 'missing'}
                       for key,row in self.report['metrics'].items()}}
        self.cached_path = self.root/'native-result.json'
        self.save()

    def save(self):
        native.write_json(self.cached_path,self.cache)

    def read(self):
        return native.cached_result(self.metadata,self.root,self.rules)

    def rewrite_detail_with_valid_hashes(self):
        """Exercise identity checks after byte-level artifact validation passes."""
        native.write_json(self.detail,self.report)
        self.cache['full_statistics'].update(sha256=native.digest(self.detail),bytes=self.detail.stat().st_size)
        self.cache['source']['sha256'] = native.digest(self.detail)
        self.save()

    def test_verified_cache_preserves_nulls_and_never_reanalyzes(self):
        settings = {'cloud_settings_path':'unused','local_analysis':{
            'backend':'native_full','environment':'test','execution':'wsl','distribution':'Synthetic',
            'python':'/venv/python','analyzer_root':'/latest','rules_json':str(self.rules),'keep_raw_logs':True}}
        before = {path:path.read_bytes() for path in (self.detail,self.cached_path)}
        with (patch.object(native,'fetch_window',side_effect=AssertionError('cache downloaded')),
              patch.object(native.subprocess,'run',side_effect=AssertionError('cache reanalyzed'))):
            self.assertEqual(native.analyze_window(self.metadata,settings,self.root,self.rules),self.cache)
        self.assertEqual({path:path.read_bytes() for path in before},before)
        self.assertIsNone(self.read()['metrics']['parking']['p99_ms'])

    def test_altered_metric_values_are_rejected(self):
        original = copy.deepcopy(self.cache)
        for field,value in [('p99_ms',999.0),('frame_rate_hz',999.0),('label','other role')]:
            with self.subTest(field=field):
                self.cache = copy.deepcopy(original)
                self.cache['metrics']['slow_driver'][field] = value
                self.save()
                with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_missing_actual_execution_proof_cannot_be_replaced_by_correct_labels(self):
        self.report.pop('runtime_execution')
        self.cache.pop('runtime_execution')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_wrong_observed_window_rejected_even_with_agreeing_cache_and_hashes(self):
        self.report['runtime_execution']['initializations'][0]['record_epoch_range']=[0,1]
        self.cache['runtime_execution']=copy.deepcopy(self.report['runtime_execution'])
        self.rewrite_detail_with_valid_hashes()
        self.assertEqual(self.report['metadata']['time_range'],'20261021154800/20261021162200')
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_missing_actual_selected_file_is_rejected_after_valid_hash_rewrite(self):
        self.report['runtime_execution']['actual_selected_members'].clear()
        self.cache['runtime_execution']=copy.deepcopy(self.report['runtime_execution'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_candidate_envelope_cannot_be_dropped_after_valid_hash_rewrite(self):
        self.report['runtime_execution']['collector_candidate_members'].clear()
        self.cache['runtime_execution']=copy.deepcopy(self.report['runtime_execution'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_previous_profile_is_not_reused_as_new_runtime_proof(self):
        for profile in ('m100-full-native-audited-v5','m100-full-native-audited-v6'):
            with self.subTest(profile=profile):
                self.cache['analysis_scope']['profile']=profile
                self.save()
                with self.assertRaisesRegex(InputError,'请求、规则或解析范围'):
                    self.read()

    def test_parent_only_compat_receipt_cannot_replace_actual_spawn_proof(self):
        self.report.pop('execution_compat_runtime')
        self.cache.pop('execution_compat_runtime')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_rewritten_hashes_do_not_accept_missing_actual_mawk_consumption(self):
        self.report['execution_compat_runtime']['workers'][0]['record']['calls'][0]['consumption']=None
        self.cache['execution_compat_runtime']=copy.deepcopy(self.report['execution_compat_runtime'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_cache_cannot_drop_actual_spawn_proof_kept_in_report(self):
        self.cache.pop('execution_compat_runtime')
        self.save()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_missing_portable_hex_execution_proof_is_rejected(self):
        self.report.pop('execution_compat')
        self.cache.pop('execution_compat')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_rewritten_hashes_do_not_accept_changed_hex_algorithm(self):
        self.report['execution_compat']['brake_bit_tests_preserved']=False
        self.cache['execution_compat']=copy.deepcopy(self.report['execution_compat'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_cache_cannot_drop_compatibility_proof_kept_in_report(self):
        self.cache.pop('execution_compat')
        self.save()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_previous_execution_schema_is_rejected_even_with_new_profile(self):
        self.report['runtime_execution'].update(schema_version=1,policy='runtime-input-window-v1')
        self.cache['runtime_execution']=copy.deepcopy(self.report['runtime_execution'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_unapproved_source_contract_cannot_be_replaced_by_correct_profile(self):
        self.report['analyzer_contract']['contract_fingerprint']='f'*64
        self.cache['analyzer_contract']=copy.deepcopy(self.report['analyzer_contract'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'实际执行审计'):
            self.read()

    def test_altered_missing_status_and_removed_scenario_are_rejected(self):
        self.cache['metrics']['parking']['status'] = 'ok'
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()
        self.cache['metrics'].pop('parking')
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_altered_overall_status_is_rejected(self):
        self.cache['status'] = 'ok'
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_altered_request_vehicle_or_window_is_rejected(self):
        for key,value in [('vin','DEM0VEH1CLE000002'),('start','2026-10-21T14:50:00'),('end','2026-10-21T15:24:00')]:
            with self.subTest(field=key):
                self.cache['request'] = {**self.metadata,key:value}
                self.save()
                with self.assertRaisesRegex(InputError,'请求'):self.read()

    def test_altered_scope_with_unchanged_profile_is_rejected(self):
        self.cache['analysis_scope']['selected_metrics']['synthetic'].remove('extra_metric')
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_altered_source_sha_is_rejected(self):
        self.cache['source']['sha256'] = 'f'*64
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_missing_source_sha_is_rejected(self):
        self.cache['source'].pop('sha256')
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_wrong_full_statistics_size_is_rejected(self):
        self.cache['full_statistics']['bytes'] += 1
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_full_statistics_vehicle_identity_checked_after_valid_sha(self):
        self.report['metadata']['vin'] = 'DEM0VEH1CLE000002'
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'车辆'):self.read()

    def test_full_statistics_window_identity_checked_after_valid_sha(self):
        self.report['metadata']['time_range'] = '20261021145000/20261021152400'
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'时段'):self.read()

    def test_full_statistics_scope_checked_after_valid_sha(self):
        self.report['analysis_scope']['complete_rule_inventory'] = False
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_full_statistics_invalid_status_rejected_even_if_cache_agrees(self):
        self.report['status'] = self.cache['status'] = 'failed'
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'状态'):self.read()

    def test_full_statistics_missing_metric_field_rejected(self):
        self.report['metrics']['slow_driver'].pop('frame_rate_hz')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'投影'):self.read()

    def test_full_statistics_byte_tamper_is_rejected(self):
        self.detail.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(InputError,'不完整'):self.read()

    def test_other_environment_cannot_reuse_cache(self):
        with self.assertRaisesRegex(InputError,'请求、规则或解析范围'):
            native.cached_result(self.metadata,self.root,self.rules,environment='prod')

    def test_legacy_profile_cannot_reuse_cache(self):
        for profile in ('m100-full-native-v1', 'm100-full-native-source-boundary-v2'):
            with self.subTest(profile=profile):
                self.cache['analysis_scope']['profile'] = profile
                self.save()
                with self.assertRaisesRegex(InputError,'请求、规则或解析范围'):
                    self.read()

    def test_cache_cannot_drop_source_selection_audit(self):
        self.cache.pop('source_selection')
        self.save()
        with self.assertRaises(InputError):
            self.read()

    def test_full_statistics_cannot_drop_source_selection_even_with_valid_hashes(self):
        self.report.pop('source_selection')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaises(InputError):
            self.read()

    def test_cache_cannot_change_source_mapping_independently(self):
        name = next(iter(self.cache['source_selection']['source_by_basename']))
        self.cache['source_selection']['source_by_basename'][name] = 'log_fsdB'
        self.save()
        with self.assertRaises(InputError):
            self.read()

    def test_agreeing_but_invalid_source_inventory_hash_is_rejected(self):
        self.report['source_selection']['input_inventory_sha256'] = 'f'*64
        self.cache['source_selection'] = copy.deepcopy(self.report['source_selection'])
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaises(InputError):
            self.read()

    def normalized(self, *, partial=True):
        issues = [{'kind':'incomplete_zstd_frame','relative_path':'mcu/XVM_MCU-synthetic.log',
                   'source_sha256':'a'*64,'recovery':'complete_decoded_lines','discarded_tail_bytes':7}] if partial else []
        return {'version':'native-input-v2','input_sha256':'b'*64,'xvm_expanded_bytes':100,
                'policy':{'allow_incomplete_frames':True,'incomplete_frame_recovery':'complete_decoded_lines'},
                'quality':{'status':'partial' if partial else 'ok','issues':issues},
                'files':[{'relative_path':'mcu/XVM_MCU-synthetic.log','sha256':'c'*64,'bytes':100,
                          'original_sha256':'a'*64,'log_class':'log_xcu_mcu','normalization':'xvm_zstd_utf8_text',
                          'frame_complete':not partial,'recovery':'complete_decoded_lines' if partial else None,
                          'discarded_tail_bytes':7 if partial else 0}]}

    def fresh_analysis(self, *, partial=True):
        self.cached_path.unlink()
        original = copy.deepcopy(self.report)
        original['status'] = 'ok'
        original['metrics']['parking'].update(p99_ms=8.0,frame_rate_hz=12.0)
        original['statistics'].append({'theme':'synthetic','metric':'mcu_cycle',
            'rule':{'soc_type':'XVM_MCU'},'statistics':{'P99':12.5,'count':50},
            'diagnostic':'original native diagnostic','status':'available',
            'native_publish':True,'unconfigured_output':True})
        from native_sheet_test_fixture import synthetic_native_sheet_payload
        original['native_sheet_data'] = synthetic_native_sheet_payload(original,
            json.loads(self.rules.read_text(encoding='utf-8')),self.metadata,native.digest(self.rules))
        normalized = self.normalized(partial=partial)
        settings = {'cloud_settings_path':'unused','local_analysis':{
            'backend':'native_full','environment':'test','execution':'wsl','distribution':'Synthetic',
            'python':'/venv/python','analyzer_root':'/latest','rules_json':str(self.rules),'keep_raw_logs':True}}
        downloaded = {'request':self.metadata,'environment':'testtwo','source_policy':native.SOURCE_POLICY,
                      'archive_query':native.archive_query_metadata(self.metadata),
                      'counts':dict.fromkeys(native.LOG_CLASSES,1),'bytes':100,'files':[]}
        upstream_dir = self.root/'analysis'/'synthetic'
        def run(command, **kwargs):
            upstream_dir.mkdir(parents=True)
            upstream = upstream_dir/'report-data.json'
            native.write_json(upstream,original)
            html = upstream_dir/'native-report.html'
            html.write_text('<html>Internal original status: ok</html>',encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'ok',
                'artifact':{'path':str(html),'sha256':native.digest(html)}}),stderr='')
        with (patch.object(native,'fetch_window',return_value=downloaded),
              patch('native_inputs.prepare_native_inputs',return_value=normalized) as prepare,
              patch.object(native.subprocess,'run',side_effect=run)):
            result = native.analyze_window(self.metadata,settings,self.root,self.rules)
        prepare.assert_called_once_with(self.root/'input',self.root/'native-prepared',downloaded,
                                        allow_incomplete_frames=True)
        return result, original, normalized, json.loads((upstream_dir/'report-data.json').read_text(encoding='utf-8'))

    def load_fresh_cache(self):
        self.cache = json.loads(self.cached_path.read_text(encoding='utf-8'))
        self.report = json.loads(self.detail.read_text(encoding='utf-8'))

    def test_recovered_frame_partial_preserves_values_and_all_source_audits(self):
        result, original, normalized, upstream = self.fresh_analysis()
        report = json.loads(self.detail.read_text(encoding='utf-8'))
        self.assertEqual(upstream,original)
        self.assertEqual(report['status'],result['status'])
        self.assertEqual(result['status'],'partial')
        self.assertEqual(report['metrics'],original['metrics'])
        self.assertEqual(report['source_quality'],normalized['quality'])
        self.assertEqual(result['source_quality'],normalized['quality'])
        self.assertEqual(report['normalization'],normalized)
        self.assertEqual(result['normalization'],normalized)
        for before, after in zip(original['statistics'],report['statistics']):
            self.assertEqual(before['statistics'],after['statistics'])
            if 'XVM_' in before.get('rule',{}).get('soc_type',''):
                self.assertEqual(after['source_quality'],normalized['quality'])
                self.assertEqual(after['diagnostic'],before['diagnostic'])
                self.assertIn('partial',after['source_quality_diagnostic'])
            else:
                self.assertNotIn('source_quality',after)
        self.assertEqual(result['full_statistics']['sha256'],native.digest(self.detail))
        self.assertEqual(result['source']['sha256'],native.digest(self.detail))
        self.assertEqual(result['full_statistics']['bytes'],self.detail.stat().st_size)
        self.assertEqual(result['artifact']['sha256'],native.digest(result['artifact']['path']))
        self.assertEqual(self.read(),result)

    def test_complete_frame_quality_does_not_downgrade_status(self):
        result, original, normalized, upstream = self.fresh_analysis(partial=False)
        report = json.loads(self.detail.read_text(encoding='utf-8'))
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['source_quality'],{'status':'ok','issues':[]})
        self.assertEqual(report['statistics'],original['statistics'])
        self.assertEqual(report['metrics'],original['metrics'])
        self.assertEqual(self.read(),result)

    def test_cache_cannot_drop_source_quality_marker(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.cache.pop('source_quality')
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_cache_cannot_downgrade_normalization_quality(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.cache['normalization']['quality'] = {'status':'ok','issues':[]}
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_cache_cannot_change_per_file_recovery_audit(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.cache['normalization']['files'][0]['discarded_tail_bytes'] = 0
        self.save()
        with self.assertRaisesRegex(InputError,'完整统计不一致'):self.read()

    def test_full_statistics_cannot_drop_xvm_quality_even_with_updated_hash(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.report['statistics'][-1].pop('source_quality')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'XVM 统计'):self.read()

    def test_partial_source_cannot_claim_ok_even_if_cache_agrees(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.report['status'] = self.cache['status'] = 'ok'
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'partial'):self.read()

    def test_full_statistics_cannot_drop_top_quality_even_with_updated_hash(self):
        self.fresh_analysis();self.load_fresh_cache()
        self.report.pop('source_quality')
        self.rewrite_detail_with_valid_hashes()
        with self.assertRaisesRegex(InputError,'丢失输入质量'):self.read()


if __name__ == '__main__':
    unittest.main()
