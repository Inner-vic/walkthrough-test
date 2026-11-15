"""Static compatibility guards with explicitly approved synthetic AST fixtures."""
import ast
import copy
from pathlib import Path
import stat
import textwrap
import types
import unittest
from unittest import mock

import analyzer_contract as contract


def synthetic_contract_audit(*, slim=False):
    """Legal receipt shape for cache tests; no source is imported or approved.

    All semantic digests come from the fixed production allowlist. The byte
    digests represent synthetic receipt data and do not attest to source files.
    """
    files = {name: {'ast_sha256': digest, 'canonical_ast_sha256': digest,
                    'file_sha256': 'd' * 64, 'variant': 'original'}
             for name, digest in contract.APPROVED_AST_SHA256.items()}
    if slim:
        files[contract.CONFIG_FILE].update(ast_sha256=contract.SLIM_CONFIG_AST_SHA256,
                                           variant='slim_m100_ultra')
    return {'schema_version': 1, 'status': 'compatible', 'profile': contract.PROFILE,
            'contract_id': contract.CONTRACT_ID, 'contract_fingerprint': contract.CONTRACT_FINGERPRINT,
            'extract_logs_ast_sha256': contract.EXTRACT_LOGS_AST_SHA256,
            'configuration_variant': files[contract.CONFIG_FILE]['variant'], 'files': files}


METHOD = '''
def extract_logs(self, theme, soc_type):
    results = []
    for item in sorted(soc_type):
        if not self.keep(item):
            continue
        results.append(item)
    return results
'''
COLLECTOR = 'raise RuntimeError("must never execute source")\nclass RecordPerformance:\n' + textwrap.indent(textwrap.dedent(METHOD), '    ')
ORIGINAL_CONFIG = 'CONFIG_PATH = {"A": ["a"]}\nCONFIG_MAPS = {"m100-ultra": 1, "other": 2}\n'
SLIM_CONFIG = 'CONFIG_PATH = {"A": ["a"]}\nCONFIG_MAPS = {"m100-ultra": 1}\n'


def ast_sha(source):
    return contract.ast_digest(ast.parse(textwrap.dedent(source)))


class StaticContractTests(unittest.TestCase):
    def setUp(self):
        self.sources = {'common/perf_csv.py': COLLECTOR.encode(), contract.CONFIG_FILE: ORIGINAL_CONFIG.encode()}
        approved = {name: ast_sha(raw.decode()) for name, raw in self.sources.items()}
        patches = [mock.patch.object(contract, 'APPROVED_AST_SHA256', approved),
                   mock.patch.object(contract, 'SLIM_CONFIG_AST_SHA256', ast_sha(SLIM_CONFIG)),
                   mock.patch.object(contract, 'EXTRACT_LOGS_AST_SHA256', ast_sha(METHOD)),
                   mock.patch.object(contract, 'RUNTIME_EXTRACT_LOGS_AST_SHA256', ast_sha(METHOD))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        fingerprint = contract._digest({'profile': contract.PROFILE, 'contract_id': contract.CONTRACT_ID,
                                       'canonical_ast_sha256': approved, 'extract_logs_ast_sha256': ast_sha(METHOD)})
        patch = mock.patch.object(contract, 'CONTRACT_FINGERPRINT', fingerprint)
        patch.start()
        self.addCleanup(patch.stop)

    def validate_sources(self, sources=None):
        with mock.patch.object(contract, '_read_sources', return_value=sources or self.sources):
            return contract.validate_analyzer_contract('synthetic-root')

    def test_static_validation_has_no_import_or_execution_side_effects(self):
        receipt = self.validate_sources()
        self.assertEqual(receipt['status'], 'compatible')
        self.assertEqual(receipt['extract_logs_ast_sha256'], ast_sha(METHOD))

    def test_original_and_explicit_slim_share_canonical_fingerprint(self):
        original = self.validate_sources()
        slim = self.validate_sources({**self.sources, contract.CONFIG_FILE: SLIM_CONFIG.encode()})
        self.assertEqual(original['contract_fingerprint'], slim['contract_fingerprint'])
        self.assertEqual(slim['configuration_variant'], 'slim_m100_ultra')
        self.assertNotEqual(original['files'][contract.CONFIG_FILE]['ast_sha256'], slim['files'][contract.CONFIG_FILE]['ast_sha256'])
        self.assertEqual(original['files'][contract.CONFIG_FILE]['canonical_ast_sha256'], slim['files'][contract.CONFIG_FILE]['canonical_ast_sha256'])

    def test_comments_bom_crlf_and_spacing_preserve_contract(self):
        changed = {name: ('\ufeff# comment\r\n' + raw.decode().replace('\n', '\r\n')).encode() for name, raw in self.sources.items()}
        original = self.validate_sources()
        receipt = self.validate_sources(changed)
        self.assertEqual(original['contract_fingerprint'], receipt['contract_fingerprint'])
        self.assertNotEqual(original['files']['common/perf_csv.py']['file_sha256'], receipt['files']['common/perf_csv.py']['file_sha256'])

    def test_whole_module_unknown_code_and_config_alias_changes_fail(self):
        for name, raw in self.sources.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '完整模块 AST'):
                self.validate_sources({**self.sources, name: raw + b'NEW_BEHAVIOR = True\n'})
        with self.assertRaises(ValueError):
            self.validate_sources({**self.sources, contract.CONFIG_FILE: SLIM_CONFIG.replace('"a"', '"b"').encode()})

    def test_unknown_sources_are_never_learned_after_failure(self):
        changed = {**self.sources, 'common/perf_csv.py': (COLLECTOR + '\nCHANGED = 1\n').encode()}
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.validate_sources(changed)
        self.assertEqual(self.validate_sources()['status'], 'compatible')

    def test_complete_method_rejects_reverse_sort_and_condition_bypass(self):
        contract.validate_extract_logs_contract(METHOD)
        for source in (METHOD.replace('sorted(soc_type)', 'sorted(soc_type, reverse=True)'),
                       METHOD.replace('if not self.keep(item):', 'if False and not self.keep(item):'),
                       METHOD.replace('return results', 'return []')):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, '完整 AST'):
                contract.validate_extract_logs_contract(source)

    def test_invalid_python_fails_without_importing(self):
        for raw in (b'not valid Python :', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.validate_sources({**self.sources, contract.CONFIG_FILE: raw})


class RetainedContractAuditTests(unittest.TestCase):
    def test_fixed_allowlist_receipts_round_trip_and_detach(self):
        for slim in (False, True):
            receipt = synthetic_contract_audit(slim=slim)
            receipt['extra_diagnostic'] = ['synthetic']
            result = contract.validate_contract_audit(receipt)
            self.assertEqual(result, receipt)
            result['files'].clear()
            self.assertTrue(receipt['files'])

    def test_missing_required_top_level_fields_fail(self):
        for key in synthetic_contract_audit():
            receipt = synthetic_contract_audit()
            receipt.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)

    def test_arbitrary_well_formed_hashes_are_not_approved(self):
        for key in ('contract_fingerprint', 'extract_logs_ast_sha256'):
            receipt = synthetic_contract_audit()
            receipt[key] = 'a' * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)
        for key in ('ast_sha256', 'canonical_ast_sha256'):
            receipt = synthetic_contract_audit()
            receipt['files']['common/perf_csv.py'][key] = 'a' * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)

    def test_module_set_missing_extra_or_variant_mismatch_fail(self):
        receipts = []
        value = synthetic_contract_audit(); value['files'].pop('common/perf_csv.py'); receipts.append(value)
        value = synthetic_contract_audit(); value['files']['common/extra.py'] = copy.deepcopy(value['files']['common/perf_csv.py']); receipts.append(value)
        value = synthetic_contract_audit(); value['configuration_variant'] = 'slim_m100_ultra'; receipts.append(value)
        value = synthetic_contract_audit(); value['files']['common/perf_csv.py']['variant'] = 'slim_m100_ultra'; receipts.append(value)
        value = synthetic_contract_audit(slim=True); value['files'][contract.CONFIG_FILE]['ast_sha256'] = contract.APPROVED_AST_SHA256[contract.CONFIG_FILE]; receipts.append(value)
        for index, receipt in enumerate(receipts):
            with self.subTest(index=index), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)

    def test_missing_module_fields_and_invalid_byte_hash_fail(self):
        for key in synthetic_contract_audit()['files']['common/perf_csv.py']:
            receipt = synthetic_contract_audit()
            receipt['files']['common/perf_csv.py'].pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)
        for value in (None, 'A' * 64, 'bad', 1):
            receipt = synthetic_contract_audit()
            receipt['files']['common/perf_csv.py']['file_sha256'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)

    def test_profile_status_and_bool_schema_mismatch_fail(self):
        for key, value in (('profile', 'unknown'), ('contract_id', 'unknown'), ('status', 'compatible-ish'), ('schema_version', True)):
            receipt = synthetic_contract_audit()
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                contract.validate_contract_audit(receipt)


class PlainSourcePathTests(unittest.TestCase):
    def test_symlink_reparse_and_nonregular_sources_are_rejected(self):
        for mode, attrs in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400), (stat.S_IFIFO, 0)):
            path = mock.Mock(name='source-path')
            path.name = 'source.py'
            path.lstat.return_value = types.SimpleNamespace(st_mode=mode, st_file_attributes=attrs)
            with self.subTest(mode=mode, attrs=attrs), self.assertRaises(ValueError):
                contract._plain(path)

    def test_plain_file_and_directory_are_accepted(self):
        for directory, mode in ((False, stat.S_IFREG), (True, stat.S_IFDIR)):
            path = mock.Mock()
            path.lstat.return_value = types.SimpleNamespace(st_mode=mode, st_file_attributes=0)
            contract._plain(path, directory=directory)

    def test_extra_package_initializer_is_rejected_before_source_reads(self):
        root = Path('synthetic-root').absolute()
        with mock.patch.object(contract, '_plain'), mock.patch.object(contract.os, 'walk', side_effect=[
            [(str(root / 'common'), [], ['__init__.py'])], [(str(root / 'general_performance'), [], [])]
        ]), self.assertRaisesRegex(ValueError, '模块集合变化'):
            contract._read_sources(root)


class CanonicalAstTests(unittest.TestCase):
    def test_dump_matches_approved_cpython_311_full_fields(self):
        for source in (COLLECTOR, ORIGINAL_CONFIG, 'def f(a=None, *args, **kwargs):\n    return None\n',
                       'x = {**a}\ny = f(**kwargs)\ntry:\n    pass\nexcept Exception:\n    pass\n'):
            tree = ast.parse(source)
            # This equality asserts the reference serialization on the tested
            # 3.11 runtime; added empty type_params is the sole normalization.
            stripped = copy.deepcopy(tree)
            for node in ast.walk(stripped):
                if 'type_params' in node._fields and getattr(node, 'type_params', None) == []:
                    node._fields = tuple(name for name in node._fields if name != 'type_params')
            self.assertEqual(contract.canonical_ast_dump(tree), ast.dump(stripped, include_attributes=False))

    def test_only_empty_new_type_params_is_normalized(self):
        baseline = ast.parse(METHOD)
        expected = contract.ast_digest(baseline)
        changed = copy.deepcopy(baseline)
        method = changed.body[0]
        method._fields = tuple(name for name in method._fields if name != 'type_params') + ('type_params',)
        method.type_params = []
        self.assertEqual(contract.ast_digest(changed), expected)
        method.type_params = [ast.Name(id='NewSemanticType', ctx=ast.Load())]
        self.assertNotEqual(contract.ast_digest(changed), expected)
        method.type_params = []
        method._fields += ('new_semantic_field',)
        method.new_semantic_field = []
        self.assertNotEqual(contract.ast_digest(changed), expected)


if __name__ == '__main__':
    unittest.main()
