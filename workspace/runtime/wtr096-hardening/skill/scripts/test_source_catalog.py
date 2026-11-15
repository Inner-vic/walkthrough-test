import copy
from datetime import datetime, timezone
import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

import source_catalog as catalog


class Selection:
    fingerprint = 'a' * 64
    time_range = '20261102172900/20261102180400'
    epochs = tuple(datetime.strptime(value, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()
                   for value in time_range.split('/'))
    prefixes = ['mach_']

    @staticmethod
    def archive_selected(path):
        return not path.name.startswith('outside_')

    @staticmethod
    def parse_time(path):
        import re
        stamps = re.findall(r'(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})', path.name)
        return datetime.strptime(stamps[-1], '%Y_%m_%d_%H_%M_%S').replace(tzinfo=timezone.utc) if stamps else None

    @classmethod
    def keep_log(cls, path, epochs, state):
        stamp = cls.parse_time(path)
        if stamp is None:
            return True
        seconds = stamp.timestamp()
        if seconds < epochs[0]:
            return False
        if seconds <= epochs[1]:
            return True
        if not state['kept_first_after_end']:
            state['kept_first_after_end'] = True
            return True
        return False


class SourceCatalogTests(unittest.TestCase):
    name = 'mach_vla_arch_container_2026_11_02_17_47_52.log'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'input'
        self.root.mkdir()
        self.output = Path(self.tmp.name) / 'perf'

    def archive(self, source='log_fsdA', item='item-0001', name='archive.zip', entries=None):
        path = self.root / source / item / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, content in entries if entries is not None else [(self.name, b'A native record\n')]:
                archive.writestr(name, content)
        return path

    def plain(self, name=None, content=b'plain', source='log_fsdA'):
        path = self.root / source / (name or self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def build(self, paths, selection=None, windows=False):
        return catalog.build_catalog(paths, selection or Selection(), self.root, windows=windows)

    def resign(self, value):
        value['catalog_sha256'] = catalog._digest({key: row for key, row in value.items() if key != 'catalog_sha256'})
        return value

    def test_cross_source_same_basename_different_contents_keeps_two_independent_members(self):
        first = self.archive()
        second = self.archive('log_fsdB', entries=[('nested/' + self.name, b'B distinct native record\n')])
        value = self.build([second, first], windows=True)
        self.assertEqual(value['selection_policy'], 'source-namespace-v3')
        self.assertEqual(len(value['selected_member_ids']), 2)
        self.assertEqual({row['source'] for row in value['members'].values()}, {'log_fsdA', 'log_fsdB'})
        receipt = catalog.materialize_catalog(value, self.root, self.output)
        self.assertEqual(receipt['member_count'], 2)
        self.assertEqual({path.read_bytes() for path in self.output.rglob('*.log')},
                         {b'A native record\n', b'B distinct native record\n'})
        for key, row in value['members'].items():
            self.assertEqual((self.output / row['namespace_path']).name, self.name)
            self.assertEqual(row['namespace_path'], 'members/' + key + '/' + self.name)

    def test_same_source_duplicate_archives_merge_logical_member_but_retain_all_origins(self):
        first = self.archive(entries=[('a/' + self.name, b'same')])
        second = self.archive(item='item-0002', entries=[('b/' + self.name, b'same')])
        value = self.build([second, first])
        self.assertEqual(len(value['members']), 1)
        row = next(iter(value['members'].values()))
        self.assertEqual([origin['member_path'] for origin in row['origins']], ['a/' + self.name, 'b/' + self.name])
        paths = [value['artifacts'][origin['artifact_id']]['relative_path'] for origin in row['origins']]
        self.assertEqual(paths, ['log_fsdA/item-0001/archive.zip', 'log_fsdA/item-0002/archive.zip'])
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)

    def test_cross_source_identical_bytes_remain_independent(self):
        value = self.build([self.archive(), self.archive('log_fsdB')])
        self.assertEqual(len(value['members']), 2)
        self.assertEqual(len({row['sha256'] for row in value['members'].values()}), 1)

    def test_same_source_same_name_conflicting_selected_contents_fails(self):
        with self.assertRaisesRegex(ValueError, '同一来源同名'):
            self.build([self.archive(), self.archive(item='item-0002', entries=[(self.name, b'different')])])

    def test_same_source_conflict_outside_exact_selection_is_preserved_and_staged(self):
        old_name = 'mach_2026_11_02_16_00_00.log'
        value = self.build([self.archive(entries=[(old_name, b'old-a'), ('vin_metadata.log', b'VIN visible')]),
                            self.archive(item='item-0002', entries=[(old_name, b'old-b')])])
        self.assertEqual(value['selected_member_ids'], [])
        self.assertEqual(len(value['members']), 3)
        receipt = catalog.materialize_catalog(value, self.root, self.output)
        self.assertEqual(receipt['member_count'], 3)
        self.assertEqual({path.read_bytes() for path in self.output.rglob('*.log')}, {b'old-a', b'old-b', b'VIN visible'})

    def test_archive_time_ineligible_origins_audited_but_not_materialized(self):
        value = self.build([self.archive(name='outside_archive.zip'), self.archive('log_fsdB')])
        self.assertEqual(len(value['members']), 2)
        self.assertEqual(value['totals']['eligible_members'], 1)
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)

    def test_one_logical_member_eligible_if_any_verified_origin_is_eligible(self):
        value = self.build([self.archive(name='outside_archive.zip'), self.archive(item='item-0002')])
        row = next(iter(value['members'].values()))
        self.assertTrue(row['eligible'])
        self.assertEqual(len(row['origins']), 2)
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)

    def test_first_after_end_exact_tie_is_stable_per_source_prefix(self):
        first = 'mach_A_2026_11_02_18_05_00.log'
        tied = 'mach_B_2026_11_02_18_05_00.log'
        late = 'mach_C_2026_11_02_18_06_00.log'
        value = self.build([self.archive(entries=[(first, b'a'), (tied, b'b'), (late, b'c')]),
                            self.archive('log_fsdB', entries=[(tied, b'source-b')])])
        chosen = [(value['members'][key]['source'], value['members'][key]['basename']) for key in value['selected_member_ids']]
        self.assertEqual(chosen, [('log_fsdA', first), ('log_fsdB', tied)])
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 4)

    def test_deterministic_input_order_and_full_member_paths(self):
        first, second = self.archive(), self.archive('log_fsdB')
        self.assertEqual(self.build([first, second]), self.build([second, first]))
        value = self.build([first])
        row = next(iter(value['members'].values()))
        self.assertEqual(next(iter(value['artifacts'].values()))['relative_path'], 'log_fsdA/item-0001/archive.zip')
        self.assertEqual(row['origins'][0]['member_path'], self.name)

    def test_exact_archive_member_spelling_is_kept_alongside_safe_full_path(self):
        raw = './nested/' + self.name
        value = self.build([self.archive(entries=[(raw, b'original spelling')])])
        origin = next(iter(value['members'].values()))['origins'][0]
        self.assertEqual(origin['archive_member_name'], raw)
        self.assertEqual(origin['member_path'], 'nested/' + self.name)
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)
        changed = copy.deepcopy(value)
        next(iter(changed['members'].values()))['origins'][0]['archive_member_name'] = './different/' + self.name
        with self.assertRaises(ValueError):
            catalog.validate_catalog(self.resign(changed))

    def test_repeated_full_identity_inside_one_archive_merges_logical_member(self):
        value = self.build([self.archive(entries=[('a/' + self.name, b'same'), ('b/' + self.name, b'same')])])
        self.assertEqual(len(value['members']), 1)
        self.assertEqual(len(next(iter(value['members'].values()))['origins']), 2)
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)

    def test_plain_log_preserves_original_basename_and_content_identity(self):
        value = self.build([self.plain()])
        row = next(iter(value['members'].values()))
        self.assertEqual(row['basename'], self.name)
        self.assertEqual(row['origins'][0]['archive_member_name'], self.name)
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)

    def test_windows_prefix_matching_retains_native_case_insensitivity(self):
        path = self.plain('MACH_2026_11_02_17_47_52.log')
        self.assertEqual(len(self.build([path], windows=True)['selected_member_ids']), 1)
        self.assertEqual(len(self.build([path], windows=False)['selected_member_ids']), 0)

    def test_tar_supported_without_extractall(self):
        path = self.root / 'log_fsdA' / 'archive.tar'
        path.parent.mkdir()
        with tarfile.open(path, 'w') as archive:
            row = tarfile.TarInfo('nested/' + self.name)
            row.size = 3
            archive.addfile(row, io.BytesIO(b'tar'))
        value = self.build([path])
        self.assertEqual(catalog.materialize_catalog(value, self.root, self.output)['member_count'], 1)
        self.assertEqual(next(self.output.rglob('*.log')).read_bytes(), b'tar')

    def test_nested_compression_message_binary_and_uppercase_log_fail(self):
        for name in ('nested.zip', 'nested.zst', 'message', 'data.pcap', 'upper.LOG'):
            with self.subTest(name=name):
                path = self.archive(entries=[(name, b'bad')])
                with self.assertRaises(ValueError):
                    self.build([path])

    def test_traversal_and_duplicate_archive_members_fail(self):
        for entries in ([('../' + self.name, b'bad')], [('a/' + self.name, b'a'), ('A/' + self.name, b'a')]):
            with self.subTest(entries=entries):
                path = self.archive(entries=entries)
                with self.assertRaises(ValueError):
                    self.build([path])

    def test_windows_unsafe_aliases_only_gate_archive_admitted_members(self):
        for name in ('CON.log', 'evil:ads.log', 'tail./a.log', 'LONGNA~1.LOG', 'cafe /a.log'):
            with self.subTest(name=name):
                path = self.archive(entries=[(name, b'bad'), (self.name, b'good')])
                with self.assertRaises(ValueError):
                    self.build([path], windows=True)
                path = self.archive(name='outside_archive.zip', entries=[(name, b'bad'), (self.name, b'good')])
                # Uppercase .LOG is independently unsupported even outside the window.
                if name.endswith('.LOG'):
                    with self.assertRaises(ValueError):
                        self.build([path], windows=True)
                else:
                    self.build([path], windows=True)

    def test_archive_symlink_and_tar_special_member_rejected(self):
        path = self.root / 'log_fsdA' / 'links.zip'
        path.parent.mkdir()
        with zipfile.ZipFile(path, 'w') as archive:
            row = zipfile.ZipInfo(self.name)
            row.create_system = 3
            row.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(row, b'target')
        with self.assertRaises(ValueError):
            self.build([path])
        path = path.with_suffix('.tar')
        with tarfile.open(path, 'w') as archive:
            row = tarfile.TarInfo(self.name)
            row.type = tarfile.SYMTYPE
            row.linkname = 'target'
            archive.addfile(row)
        with self.assertRaises(ValueError):
            self.build([path])

    def test_zip_crc_is_verified_in_build(self):
        path = self.archive(entries=[(self.name, b'UNIQUE-CONTENT-MARKER')])
        body = path.read_bytes().replace(b'UNIQUE-CONTENT-MARKER', b'CORRUPT-CONTENT-MARKER', 1)
        path.write_bytes(body)
        with self.assertRaises((zipfile.BadZipFile, ValueError)):
            self.build([path])

    def test_input_change_during_catalog_scan_is_rejected(self):
        path = self.plain()
        original = catalog._hash_stream
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'changed after read')
            return result
        with mock.patch.object(catalog, '_hash_stream', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                self.build([path])

    def test_changed_input_after_catalog_is_rejected_before_staging(self):
        path = self.archive()
        value = self.build([path])
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '不一致'):
            catalog.materialize_catalog(value, self.root, self.output)
        self.assertEqual(list(self.output.rglob('*.log')), [])

    def test_input_change_during_materialization_is_rejected(self):
        path = self.archive()
        value = self.build([path])
        original = catalog._hash_stream
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'changed after materialize')
            return result
        with mock.patch.object(catalog, '_hash_stream', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, '不一致'):
                catalog.materialize_catalog(value, self.root, self.output)

    def test_source_and_target_links_pollution_rejected(self):
        path = self.plain()
        value = self.build([path])
        self.output.mkdir()
        (self.output / 'pollution.log').write_bytes(b'polluted')
        with self.assertRaisesRegex(ValueError, '已存在内容'):
            catalog.materialize_catalog(value, self.root, self.output)
        try:
            linked = self.root / 'log_fsdB'
            linked.symlink_to(path.parent, target_is_directory=True)
        except OSError:
            return  # Link creation may be unavailable to an unprivileged Windows account.
        with self.assertRaises(ValueError):
            self.build([linked / path.name])

    def test_expanded_input_and_member_budgets_include_nonlog_members(self):
        path = self.archive(entries=[('notes.txt', b'x' * 20), (self.name, b'log')])
        with mock.patch.object(catalog, 'MAX_EXPANDED_BYTES', 10):
            with self.assertRaises(ValueError):
                self.build([path])
        with mock.patch.object(catalog, 'MAX_ARCHIVE_MEMBERS', 1):
            with self.assertRaises(ValueError):
                self.build([path])
        with mock.patch.object(catalog, 'MAX_INPUT_BYTES', 1):
            with self.assertRaises(ValueError):
                self.build([path])

    def test_invalid_catalog_digest_identity_origin_and_bool_counts_rejected(self):
        value = self.build([self.archive()])
        tampered = copy.deepcopy(value)
        next(iter(tampered['members'].values()))['bytes'] += 1
        with self.assertRaises(ValueError):
            catalog.validate_catalog(tampered)
        for mutation in (
                lambda row: row['totals'].__setitem__('input_bytes', True),
                lambda row: next(iter(row['members'].values())).__setitem__('namespace_path', '../escape.log'),
                lambda row: next(iter(row['members'].values()))['origins'][0].__setitem__('member_path', '../escape.log'),
                lambda row: next(iter(row['members'].values())).__setitem__('source', []),
                lambda row: next(iter(row['artifacts'].values())).__setitem__('source', []),
                lambda row: next(iter(row['members'].values())).__setitem__('ordinal', True),
                lambda row: row.__setitem__('selected_member_ids', ['not-a-member']),
                lambda row: next(iter(row['artifacts'].values())).__setitem__('relative_path', '../archive.zip')):
            tampered = copy.deepcopy(value)
            mutation(tampered)
            self.resign(tampered)
            with self.assertRaises(ValueError):
                catalog.validate_catalog(tampered)

    def test_materialization_verifies_individual_member_digest_even_valid_catalog_digest(self):
        value = self.build([self.archive()])
        key, row = next(iter(value['members'].items()))
        row['sha256'] = 'b' * 64
        new_key = catalog._member_id(row)
        row['namespace_path'] = 'members/' + new_key + '/' + row['basename']
        value['members'] = {new_key: row}
        value['selected_member_ids'] = [new_key]
        self.resign(value)
        with self.assertRaisesRegex(ValueError, '内容与来源目录不一致'):
            catalog.materialize_catalog(value, self.root, self.output)


if __name__ == '__main__':
    unittest.main()
