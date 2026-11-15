import json
from pathlib import Path
import unittest
from openpyxl import load_workbook
import test_environment_report as environment_fixture
from test_environment_report import build_report_document
from feishu_report import build_report_notification
import full_workbook as fw

QUALITY={'status':'partial','issues':[{'kind':'incomplete_zstd_frame','relative_path':'log_xcu_mcu/synthetic.log',
          'source_sha256':'a'*64,'recovery':'complete_decoded_lines','discarded_tail_bytes':12}]}

class Tests(unittest.TestCase):
    def test_quality_survives_comparison_workbook_and_main_report(self):
        fixture=environment_fixture.Tests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.pipeline['windows']['baseline']['ad']['source_quality']=QUALITY
        result=fixture.result()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['local_sources']['baseline']['source_quality'],QUALITY)
        xml=build_report_document(result)['xml']
        self.assertNotIn('MCU 来源存在不完整日志',xml)
        notice=build_report_notification({**result,'native_details':{
            'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
            'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}},
            vin='SYNTHETICVIN',url='https://example.feishu.cn/docx/SyntheticMain')
        self.assertIn('Base 的 MCU 来源存在不完整日志',str(notice))
        self.assertEqual(str(notice).count('相关统计仅供参考'),1)
        self.assertNotIn('synthetic.log',str(notice))
        self.assertNotIn('discarded_tail_bytes',str(notice))
        manifest=result['native_sheet_data']['baseline']
        from native_sheet_data import load_native_sheet_data
        payload=load_native_sheet_data(manifest['path'],manifest['sha256'])
        self.assertEqual(payload['delivery_context']['scope']['source_quality'],QUALITY)

    def test_available_statistic_with_partial_source_is_never_labeled_complete(self):
        record={'status':'available','statistics':{'mean':1.25},
                'source_quality':QUALITY,'source_quality_diagnostic':'MCU 来源日志不完整'}
        result=fw._audit({},record)
        self.assertEqual(result[0],'available_incomplete_source')
        self.assertIn('不完整',result[1])
        self.assertEqual(record['statistics'],{'mean':1.25})

if __name__=='__main__':unittest.main()

