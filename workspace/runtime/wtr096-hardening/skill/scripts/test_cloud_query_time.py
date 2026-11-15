import os
import unittest
from unittest.mock import patch

from cloud_query_time import bind_collect_time_ms


class QueryTimeTests(unittest.TestCase):
    def sample(self):
        return {'collectTimeMin':'2026-10-30 00:00:00',
                'collectTimeMax':'2026-10-30 23:59:59', 'userName':'synthetic', 'logClass':'msg_xcu'}

    def test_user_supplied_actual_url_epochs_match_exactly(self):
        source=self.sample(); result=bind_collect_time_ms(source)
        self.assertEqual(result['collectTimeMinMs'],1793289600000)
        self.assertEqual(result['collectTimeMaxMs'],1793375999000)
        self.assertEqual(source,self.sample())
        self.assertEqual({k:result[k] for k in source},source)

    def test_does_not_depend_on_process_timezone(self):
        expected=bind_collect_time_ms(self.sample())
        for zone in ('UTC','America/Los_Angeles','Asia/Tokyo'):
            with self.subTest(zone=zone), patch.dict(os.environ,{'TZ':zone}):
                self.assertEqual(bind_collect_time_ms(self.sample()),expected)

    def test_cross_midnight_and_native_archive_extension(self):
        original={'collectTimeMin':'2026-10-30 23:45:00','collectTimeMax':'2026-10-31 01:15:00'}
        result=bind_collect_time_ms(original)
        self.assertEqual(result['collectTimeMaxMs']-result['collectTimeMinMs'],90*60*1000)
        self.assertEqual(result['collectTimeMax'],'2026-10-31 01:15:00')

    def test_internal_iso_metadata_is_sent_as_api_wall_time(self):
        source={'collectTimeMin':'2026-10-30T00:00:00','collectTimeMax':'2026-10-30T23:59:59'}
        result=bind_collect_time_ms(source)
        self.assertEqual(result['collectTimeMin'],'2026-10-30 00:00:00')
        self.assertEqual(result['collectTimeMinMs'],1793289600000)
        self.assertEqual(source['collectTimeMin'],'2026-10-30T00:00:00')

    def test_consistent_existing_epochs_are_normalized(self):
        source=self.sample();source.update(collectTimeMinMs='1793289600000',collectTimeMaxMs=1793375999000)
        result=bind_collect_time_ms(source)
        self.assertIs(type(result['collectTimeMinMs']),int)

    def test_mismatched_epoch_or_bad_type_is_rejected(self):
        for value in (1793289600001,'1793289600001',True,1793289600000.0,None):
            with self.subTest(value=value):
                source=self.sample();source['collectTimeMinMs']=value
                with self.assertRaises(ValueError):bind_collect_time_ms(source)

    def test_malformed_dates_and_partial_bounds_are_rejected(self):
        for value in ('2026-02-30 00:00:00','2026-10-30 00:00:00+00:00','2026-10-30 00:00:00.001','secret\n2026-10-30 00:00:00',None):
            with self.subTest(value=value):
                source=self.sample();source['collectTimeMin']=value
                with self.assertRaises(ValueError):bind_collect_time_ms(source)
        for source in ({'collectTimeMin':'2026-10-30 00:00:00'},{'collectTimeMaxMs':1793375999000}):
            with self.assertRaises(ValueError):bind_collect_time_ms(source)

    def test_reverse_bound_is_rejected(self):
        with self.assertRaises(ValueError):
            bind_collect_time_ms({'collectTimeMin':'2026-10-31 00:00:00','collectTimeMax':'2026-10-30 00:00:00'})

    def test_unrelated_probe_parameters_are_copied(self):
        source={'userName':'synthetic'}
        result=bind_collect_time_ms(source)
        self.assertEqual(result,source)
        self.assertIsNot(result,source)


if __name__=='__main__':unittest.main()
