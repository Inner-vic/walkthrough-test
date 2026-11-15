"""Regression for the actual preflight source boundary and overwrite contract."""
from pathlib import Path
import tempfile
import unittest
import zipfile

import run_m100 as runner
import test_full_native as fixtures


class SourceInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.selector=fixtures.FullTests.native_selection(self)

    def archive(self,source,stamp,body=b'valid',member=None):
        path=self.root/source/('item_'+stamp+'.zip')
        path.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(path,'w') as out:
            out.writestr(member or 'mach_'+stamp+'.log',body)
        return path

    def test_each_source_keeps_its_own_first_boundary(self):
        a=self.archive('log_fsdA','2026_10_21_15_25_00')
        b=self.archive('log_fsdB','2026_10_21_15_39_00')
        a2=self.archive('log_fsdA','2026_10_21_15_40_00')
        b2=self.archive('log_fsdB','2026_10_21_15_41_00')
        inventory=runner.validate_log_inventory([a,b,a2,b2],self.selector,self.root)
        selected=self.selector.selected_names(inventory['source_by_basename'],inventory['source_by_basename'])
        self.assertEqual(selected,{'mach_2026_10_21_15_25_00.log','mach_2026_10_21_15_39_00.log'})
        self.assertEqual(inventory['selected_log_count'],2)

    def test_newly_consumed_other_source_boundary_conflict_is_rejected(self):
        a=self.archive('log_fsdA','2026_10_21_15_25_00')
        b=self.archive('log_fsdB','2026_10_21_15_39_00',b'one')
        other=self.root/'log_fsdB'/'other_2026_10_21_15_39_00.zip'
        with zipfile.ZipFile(other,'w') as out:out.writestr('mach_2026_10_21_15_39_00.log',b'two')
        with self.assertRaisesRegex(ValueError,'实际选集'):
            runner.validate_log_inventory([a,b,other],self.selector,self.root)

    def test_same_basename_across_sources_is_ambiguous_even_when_bytes_equal(self):
        a=self.archive('log_fsdA','2026_10_21_15_10_00')
        b=self.archive('log_fsdB','2026_10_21_15_10_00')
        with self.assertRaisesRegex(ValueError,'同名日志跨来源'):
            runner.validate_log_inventory([a,b],self.selector,self.root)

    def test_member_directory_does_not_override_outer_source(self):
        name='mach_2026_10_21_15_10_00.log'
        a=self.archive('log_fsdA','2026_10_21_15_10_00',member='log_fsdB/'+name)
        inventory=runner.validate_log_inventory([a],self.selector,self.root)
        self.assertEqual(inventory['source_by_basename'],{name:'log_fsdA'})

    def test_later_cross_source_conflict_outside_both_selections_is_excluded(self):
        first_a=self.archive('log_fsdA','2026_10_21_15_25_00')
        first_b=self.archive('log_fsdB','2026_10_21_15_26_00')
        late_a=self.archive('log_fsdA','2026_10_21_15_39_00',b'one')
        late_b=self.archive('log_fsdB','2026_10_21_15_39_00',b'two')
        inventory=runner.validate_log_inventory([first_a,first_b,late_a,late_b],self.selector,self.root)
        name='mach_2026_10_21_15_39_00.log'
        self.assertNotIn(name,inventory['source_by_basename'])
        self.assertEqual(inventory['selected_log_count'],2)
        self.assertEqual([row['basename'] for row in inventory['excluded_conflicts']],[name])
        self.assertEqual({row['source'] for row in inventory['excluded_conflicts'][0]['variants']},{'log_fsdA','log_fsdB'})
        self.assertEqual(len(inventory['basename_hashes'][name]),2)

    def test_conflict_selected_by_only_one_source_still_rejected(self):
        first_a=self.archive('log_fsdA','2026_10_21_15_25_00')
        a=self.archive('log_fsdA','2026_10_21_15_39_00',b'one')
        b=self.archive('log_fsdB','2026_10_21_15_39_00',b'two')
        with self.assertRaisesRegex(ValueError,'实际选集'):
            runner.validate_log_inventory([first_a,a,b],self.selector,self.root)

    def test_ambiguous_outer_source_is_rejected(self):
        a=self.archive('log_fsdA/log_fsdB','2026_10_21_15_10_00')
        with self.assertRaisesRegex(ValueError,'多个来源类别'):
            runner.validate_log_inventory([a],self.selector,self.root)

    def test_flat_legacy_input_remains_single_stream(self):
        names=['mach_2026_10_21_15_25_00.log','mach_2026_10_21_15_39_00.log']
        paths=[]
        for name in names:
            path=self.root/name;path.write_bytes(b'valid');paths.append(path)
        inventory=runner.validate_log_inventory(paths,self.selector,self.root)
        self.assertEqual(inventory['selected_log_count'],1)
        self.assertEqual(set(inventory['source_by_basename'].values()),{'legacy'})


if __name__=='__main__':unittest.main()
