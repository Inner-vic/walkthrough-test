import unittest
from unittest.mock import patch

import native_log_source as native
import environment_selection as selection
import service_log_source as service
from request_model import InputError

META={'vin':'DEM0VEH1CLE000001','start':'2026-10-30T17:31:00','end':'2026-10-30T19:06:00'}


class SourceCapacityTests(unittest.TestCase):
    def rows(self, count):
        return [{'vin':META['vin'],'fileKey':'synthetic-'+str(n),
                 'fileName':'synthetic-'+str(n)+'.zip','collectTime':'2026-10-30 18:00:00'}
                for n in range(count)]

    def test_actual_234_archive_case_returns_all_three_pages(self):
        rows=self.rows(234)
        responses=[{'code':200,'data':{'total':234,'list':rows[n:n+100]}}
                   for n in range(0,234,100)]
        with patch.object(native.cloud,'request_json',side_effect=responses) as call:
            result=native.query_class(META,'log_fsdA','synthetic')
        self.assertEqual(result,rows)
        self.assertEqual([v.args[0]['pageNo'] for v in call.call_args_list],[1,2,3])

    def test_capacity_boundary_returns_all_four_pages(self):
        rows=self.rows(native.MAX_FILES_PER_CLASS)
        responses=[{'data':{'total':len(rows),'list':rows[n:n+100]}}
                   for n in range(0,len(rows),100)]
        with patch.object(native.cloud,'request_json',side_effect=responses):
            self.assertEqual(native.query_class(META,'log_fsdA','synthetic'),rows)

    def test_above_capacity_is_rejected_before_partial_acceptance(self):
        value={'data':{'total':native.MAX_FILES_PER_CLASS+1,'list':self.rows(100)}}
        with patch.object(native.cloud,'request_json',return_value=value) as call:
            with self.assertRaises(InputError):native.query_class(META,'log_fsdA','synthetic')
        self.assertEqual(call.call_count,1)

    def test_234_case_still_rejects_missing_last_page(self):
        rows=self.rows(200)
        responses=[{'data':{'total':234,'list':rows[:100]}},
                   {'data':{'total':234,'list':rows[100:]}},
                   {'data':{'total':234,'list':[]}}]
        with patch.object(native.cloud,'request_json',side_effect=responses):
            with self.assertRaises(InputError):native.query_class(META,'log_fsdA','synthetic')

    def test_environment_decision_accepts_complete_234_archive_evidence(self):
        counts={name:0 for name in selection.CLASSES};counts['log_fsdA']=234
        value={'version':selection.VERSION,'policy':'prod','request':META,
               'environment':'prod','provider_environment':'prod',
               'attempts':[{'environment':'prod','counts':counts,'pcm_status':'ok'}]}
        self.assertEqual(selection.validate_decision(value,META,'prod'),value)
        counts[service.LOG_CLASS]=201
        with self.assertRaises(InputError):selection.validate_decision(value,META,'prod')


if __name__=='__main__':unittest.main()
