"""Report contract: readable units without conversion or role/value drift."""
from __future__ import annotations

from copy import deepcopy
import unittest
from xml.etree import ElementTree as ET

from feishu_report import build_report_document, build_report_notification
from performance_comparison import compare_pair
from report_attachments import artifact_filename
from request_model import InputError


class ReportUnitTests(unittest.TestCase):
    def test_attachment_description_uses_the_published_card_filename(self):
        result = {'full_workbooks':{role:{'path':'unused-'+role+'.xlsx',
            'request_sha256':('a' if role == 'baseline' else 'b')*64,
            'sha256':('c' if role == 'baseline' else 'd')*64}
            for role in ('baseline','replacement')}}
        xml = build_report_document(result)['xml']
        root = ET.fromstring('<report>'+xml+'</report>')
        visible = ''.join(root.itertext())
        self.assertEqual([node.tag for node in root],['title','table'])
        self.assertEqual(artifact_filename('baseline','a'*64,'c'*64),
                         'Base_aaaaaaaaaaaa_cccccccccccc.xlsx')
        self.assertEqual(artifact_filename('replacement','b'*64,'d'*64),
                         'PCM_update_bbbbbbbbbbbb_dddddddddddd.xlsx')
        self.assertNotIn('.xlsx',visible)
        self.assertNotIn('unused-',visible)

    def test_attachment_name_requires_known_role_and_verified_fingerprints(self):
        for role,request_sha,file_sha in (
            ('unknown','a'*64,'b'*64),('baseline',None,'b'*64),
            ('baseline','a'*64,'not-a-hash'),('replacement','A'*64,'b'*64)):
            with self.subTest(role=role,request_sha=request_sha,file_sha=file_sha):
                with self.assertRaises(InputError):
                    artifact_filename(role,request_sha,file_sha)

    def test_units_and_percentage_points_preserve_the_report_values(self):
        keys = (
            "p99_infer_delay_ms", "frame_rate_hz", "system_cpu_percent",
            "pcm_p99_infer_delay_ms", "pcm_frame_rate_hz", "pcm_cpu_percent",
        )
        pairs = (
            compare_pair(120, 100, direction="lower"),
            compare_pair(5, 6, direction="higher"),
            compare_pair(60, 55, direction="lower"),
            compare_pair(20, 23, direction="lower"),
            compare_pair(None, 4, direction="higher"),
            compare_pair(3, None, direction="lower"),
        )
        # Missing statistics only become an ASCII dash with explicit evidence.
        pairs[4]['baseline_status']='empty'
        pairs[5]['replacement_status']='empty'
        result = {"comparisons": {"slow_driver": {
            "label": "低速人驾", "metrics": dict(zip(keys, pairs))}}}
        original = deepcopy(result)
        xml = build_report_document(result)["xml"]
        root = ET.fromstring("<report>" + xml + "</report>")
        headers = root.findall("./table/thead/tr")[0].findall("th")
        self.assertEqual(["".join(cell.itertext()) for cell in headers], [
            "车辆模式", "AD P99 Infer Delay", "AD Frame Rate",
            "Sys CPU Usage", "PCM P99 Infer Delay",
            "PCM Frame Rate", "PCM CPU Usage",
        ])
        role_headers = root.findall("./table/thead/tr")[1].findall("th")
        self.assertEqual(["".join(cell.itertext()) for cell in role_headers],
                         ["base", "PCM更新"] * 6)
        rows = root.findall("./table/tbody/tr")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row.findall("td")) == 13 for row in rows))
        cells = rows[0].findall("td")
        self.assertEqual(["".join(cell.itertext()) for cell in cells[1:]], [
            "120", "100 (-20)", "5", "6 (+1)", "60", "55 (-5)",
            "20", "23 (+3)", "-", "4", "3", "-",
        ])
        self.assertEqual([span.attrib["text-color"] for span in rows[0].findall(".//span")],
                         ["green", "green", "green", "red"])
        self.assertEqual([node.tag for node in root],['title','table'])
        native_result={**result,'delivery_format':'docx_with_native_sheet_details',
            'native_details':{'baseline':{'url':'https://example.feishu.cn/sheets/SyntheticBase'},
                              'replacement':{'url':'https://example.feishu.cn/sheets/SyntheticPcm'}}}
        notice=build_report_notification(native_result,vin='SYNTHETICVIN',
                                        url='https://example.feishu.cn/docx/SyntheticMain')
        self.assertIn('时延 ms、帧率 Hz、CPU %',str(notice))
        self.assertIn('CPU 差值为百分点',str(notice))
        self.assertIn('Δ = PCM更新 − Base',str(notice))
        self.assertEqual(result, original)


if __name__ == "__main__":
    unittest.main()
