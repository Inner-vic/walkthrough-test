"""Report requirements: table only, proven empty versus unparsed, independent sides."""
import unittest
from xml.etree import ElementTree as ET

from feishu_report import build_report_document, _baseline_cell, _replacement_cell
from performance_comparison import (compare_performance, sheet_field_status,
                                    state_field_status, UNPARSED_LABEL)

SCENARIOS = ('slow_driver','fast_driver','parking','CNOA')
FIELDS = ('system_cpu_percent','pcm_cpu_percent','pcm_p99_infer_delay_ms','pcm_frame_rate_hz')
BASE = {'vin':'DEM0VEH1CLE000001','start':'2026-10-21T15:48:00','end':'2026-10-21T16:22:00'}
OTHER = {**BASE,'start':'2026-10-21T14:50:00','end':'2026-10-21T15:24:00'}


def ad():
    return {'metrics':{s:{'p99_ms':100,'frame_rate_hz':5,'status':'ok','frame_rate_status':'ok',
                          'p99_source':{'metric':'vla_parking_header_delay' if s=='parking' else 'header_delay'},
                          'fps_source':{'metric':'VlaParkingDLM_FPS' if s=='parking' else 'VlaModel_FPS'}}
                       for s in SCENARIOS}}


def pcm():
    return {'metrics':{s:{f:{'mean':10,'p99':10,'count':1} for f in FIELDS} for s in SCENARIOS}}


def result(a,b,p,q):
    return compare_performance(a,b,baseline_metadata=BASE,replacement_metadata=OTHER,
                               baseline_state=p,replacement_state=q)


class Tests(unittest.TestCase):
    def test_body_contains_only_table_despite_local_diagnostics(self):
        r=result(ad(),ad(),pcm(),pcm())
        r.update(issues=[{'message':'private diagnostic'}],pipeline={'manifest_sha256':'secret'},
                 local_sources={'baseline':{'download':{'counts':{'log_fsdA':7}}}})
        d=build_report_document(r)
        doc=ET.fromstring('<doc>'+d['xml']+'</doc>')
        self.assertEqual([c.tag for c in doc],['title','table'])
        self.assertEqual(len(doc.findall('.//tbody/tr')),4)
        self.assertNotIn('private diagnostic',d['xml'])
        self.assertNotIn('secret',d['xml'])
        self.assertEqual(len(r['issues']),1)

    def test_parsed_empty_is_ascii_dash(self):
        a,b=ad(),ad()
        for source in (a,b):
            source['metrics']['parking'].update(p99_ms=None,status='missing')
            source['field_presence']={'vla_parking_header_delay':{'status':'parsed','observations':3}}
        r=result(a,b,pcm(),pcm());pair=r['comparisons']['parking']['metrics']['p99_infer_delay_ms']
        self.assertEqual((pair['baseline_status'],pair['replacement_status']),('empty','empty'))
        self.assertEqual(r['table']['rows'][2][1:3],['-','-'])
        self.assertIn('>-</p>',_baseline_cell(pair))
        self.assertNotIn(UNPARSED_LABEL,_replacement_cell(pair))

    def test_unparsed_field_is_visible_in_exact_cell(self):
        a=ad();a['metrics']['parking'].update(p99_ms=None,status='missing')
        a['field_presence']={'vla_parking_header_delay':{'status':'unparsed','observations':0}}
        r=result(a,ad(),pcm(),pcm());pair=r['comparisons']['parking']['metrics']['p99_infer_delay_ms']
        self.assertEqual(pair['baseline_status'],'unparsed')
        self.assertEqual(pair['replacement_status'],'value')
        self.assertIn(UNPARSED_LABEL,_baseline_cell(pair))
        self.assertIn('100',_replacement_cell(pair))
        self.assertNotIn('(',_replacement_cell(pair))
        self.assertTrue(any(i.get('kind')=='field_unparsed' and i['side']=='baseline' for i in r['issues']))

    def test_valid_baseline_is_kept_when_replacement_missing(self):
        pair={'baseline':0,'replacement':None,'status':'missing','replacement_status':'empty'}
        self.assertIn('>0</p>',_baseline_cell(pair))
        self.assertIn('>-</p>',_replacement_cell(pair))

    def test_unknown_legacy_empty_never_becomes_dash(self):
        self.assertEqual(sheet_field_status({'metrics':{'parking':{}}},'parking','p99_ms'),'unparsed')
        self.assertEqual(state_field_status({'metrics':{'parking':{}}},'parking','pcm_cpu_percent'),'unparsed')
        self.assertIn(UNPARSED_LABEL,_baseline_cell({'baseline':None,'status':'missing'}))

    def test_ad_rows_and_placeholder_statistics_are_not_presence(self):
        x={'metrics':{},'coverage':{'targets':{'header_delay':{'rows':20,'unrecognized_rows':0}}},
           'statistics':[{'metric':'header_delay','statistics':{'P99_0':'-','P99_1':'-'}}]}
        self.assertEqual(sheet_field_status(x,'slow_driver','p99_ms'),'unparsed')

    def test_exact_ad_target_positive_legacy_stat_proves_presence(self):
        x={'metrics':{},'statistics':[{'metric':'VlaParkingDLM_FPS','statistics':{'mean_0':'4.06','mean_1':'-'}}]}
        self.assertEqual(sheet_field_status(x,'parking','frame_rate_hz'),'empty')
        self.assertEqual(sheet_field_status(x,'parking','p99_ms'),'unparsed')

    def test_pcm_same_field_other_state_or_positive_samples_proves_presence(self):
        x=pcm();x['metrics']['parking']['pcm_frame_rate_hz']={'mean':None,'count':0}
        self.assertEqual(state_field_status(x,'parking','pcm_frame_rate_hz'),'empty')
        self.assertEqual(state_field_status({'metrics':{},'coverage':{'latency_samples':5}},'parking','pcm_p99_infer_delay_ms','p99'),'empty')
        self.assertEqual(state_field_status({'metrics':{},'coverage':{'fps_samples':0}},'parking','pcm_frame_rate_hz'),'unparsed')

    def test_source_failure_remains_warning(self):
        x={'metrics':{},'error':'source failed','field_presence':{'pcm_cpu_percent':{'status':'parsed','observations':2}}}
        self.assertEqual(state_field_status(x,'parking','pcm_cpu_percent'),'unparsed')

    def test_finite_zero_is_value_and_delta_remains_colored(self):
        a,b=ad(),ad();a['metrics']['slow_driver']['p99_ms']=0;b['metrics']['slow_driver']['p99_ms']=1
        r=result(a,b,pcm(),pcm());pair=r['comparisons']['slow_driver']['metrics']['p99_infer_delay_ms']
        self.assertEqual(pair['baseline_status'],'value')
        self.assertEqual(pair['delta'],1)
        self.assertIn('red',_replacement_cell(pair))

    def test_invalid_declared_presence_does_not_mask_unknown(self):
        for item in ({'status':'parsed','observations':0},{'status':'parsed','observations':True},{'status':'parsed'},{}):
            with self.subTest(item=item):
                x={'metrics':{},'field_presence':{'pcm_cpu_percent':item}}
                self.assertEqual(state_field_status(x,'parking','pcm_cpu_percent'),'unparsed')

    def test_infinite_or_boolean_numbers_are_not_reported_as_values(self):
        for value in (float('inf'),float('nan'),True):
            with self.subTest(value=value):
                self.assertIn(UNPARSED_LABEL,_baseline_cell({'baseline':value}))


if __name__=='__main__':
    unittest.main()
