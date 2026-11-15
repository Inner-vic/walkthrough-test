"""Synthetic regression of the exact portable brake conversion boundary."""
from __future__ import annotations

import copy
import shutil
import subprocess
import types
import unittest

from native_execution_compat import (
    ORIGINAL_CLASSIFIER, PATCHED_CLASSIFIER, POLICY, _epochs, _object_sha, expected_execution_compat_audit,
    install_execution_compat, patch_awk_program, validate_execution_compat_audit,
    validate_execution_compat_runtime_audit,
)


def synthetic_runtime_audit(request_time_range,contract_fingerprint):
    """Explicit test-only mock; never represents a real AWK invocation."""
    from test_analyzer_contract import synthetic_contract_audit
    contract=synthetic_contract_audit()
    if contract['contract_fingerprint']!=contract_fingerprint:raise ValueError('unknown synthetic contract')
    epoch=_epochs(request_time_range)
    session={'schema_version':1,'policy':POLICY,'nonce':'a'*32,'parent_pid':100,
        'request_time_range':request_time_range,'epoch_range':epoch,
        'source_root':'/synthetic/analyzer','expected_log_root':'/synthetic/input/perf',
        'analyzer_contract':contract,'installation':expected_execution_compat_audit(),
        'pools':[{'start_method':'spawn','requested_max_workers':2,'actual_max_workers':2}],
        'submissions':[{'function':'common.perf_utils.txt_process_tool','theme':'synthetic',
                        'epoch_range':epoch,'input_path_count':1,'expected_mawk_call':True}],
        'errors':[]}
    ini={'time_range':request_time_range,'log_root':'/synthetic/input/perf','sha256':'b'*64}
    record={'schema_version':1,'policy':POLICY,'nonce':session['nonce'],
        'parent_pid':100,'pid':101,'request_time_range':request_time_range,
        'analyzer_contract':copy.deepcopy(contract),'initial_ini':ini,
        'calls':[{'generation':{'original_program_sha256':'c'*64,'patched_program_sha256':'d'*64,
                               'context_sha256':'e'*64,'year':int(request_time_range[:4]),
                               'outside_classifier_bytes_unchanged':True},
                  'consumption':{'program_sha256':'d'*64,'program_bytes':120,
                       'input_path_count':1,'input_paths_sha256':'f'*64,'actual_ini':copy.deepcopy(ini),
                       'returncode':0,'program_after_sha256':'d'*64}}],
        'errors':[]}
    return {'schema_version':1,'policy':POLICY,'session':session,'session_sha256':_object_sha(session),
            'workers':[{'record':record,'sha256':_object_sha(record)}],'total_mawk_calls':1}


def _rehash(audit):
    audit['session_sha256']=_object_sha(audit['session'])
    for worker in audit['workers']:worker['sha256']=_object_sha(worker['record'])
    return audit


def _python_brake(a, b):
    first, second = int(a, 16), int(b, 16)
    bits = (first // 2 % 8, second % 8)
    return '自研EMB' if bits == (1, 0) else '博世EMB' if bits == (1, 1) else '其他制动'


def _run(command, classifier, cases):
    program = ('BEGIN { FS="\\t" }\n' + classifier
               + '\n{ print classify_brake($1, $2) }\n')
    body = ''.join(a + '\t' + b + '\n' for a, b in cases)
    result = subprocess.run(command + [program], input=body.encode('utf-8'),
                            capture_output=True, timeout=20, check=True)
    if result.stderr:
        raise AssertionError(result.stderr.decode('utf-8'))
    rows = result.stdout.decode('utf-8').splitlines()
    if len(rows) != len(cases):
        raise AssertionError('AWK output did not preserve one classification per input')
    return rows


class ExecutionCompatContractTests(unittest.TestCase):
    def test_replace_only_classifier_bytes(self):
        before = 'unchanged-prefix\n' + ORIGINAL_CLASSIFIER + '\nunchanged-suffix'
        after = patch_awk_program(before)
        self.assertEqual(after, 'unchanged-prefix\n' + PATCHED_CLASSIFIER + '\nunchanged-suffix')

    def test_missing_classifier_rejected(self):
        for value in (None, {}, '', ORIGINAL_CLASSIFIER.replace('b1/2', 'b1/4')):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                patch_awk_program(value)

    def test_duplicate_or_already_patched_rejected(self):
        for value in (ORIGINAL_CLASSIFIER * 2, PATCHED_CLASSIFIER,
                      ORIGINAL_CLASSIFIER + 'function classify_brake(x) {}',
                      ORIGINAL_CLASSIFIER + '_walkthrough_hex_decimal_v1'):
            with self.subTest(value=value[-25:]), self.assertRaises(ValueError):
                patch_awk_program(value)

    def test_audit_accepts_exact_algorithm(self):
        audit = expected_execution_compat_audit()
        self.assertEqual(validate_execution_compat_audit(audit), audit)

    def test_audit_rejects_tampered_fields(self):
        audit = expected_execution_compat_audit()
        for key, value in audit.items():
            changed = dict(audit)
            changed[key] = not value if isinstance(value, bool) else value + 'changed'
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_execution_compat_audit(changed)

    def test_audit_rejects_missing_extra_and_bool_integer(self):
        audit = expected_execution_compat_audit()
        for changed in (None, {}, dict(audit, extra=True),
                        {k:v for k,v in audit.items() if k != 'policy'},
                        dict(audit, clean_hex_only=1)):
            with self.subTest(value=changed), self.assertRaises(ValueError):
                validate_execution_compat_audit(changed)

    def test_unknown_builder_not_installed(self):
        namespace = types.SimpleNamespace(_build_awk_program=lambda c, y: ORIGINAL_CLASSIFIER)
        original = namespace._build_awk_program
        with self.assertRaises(ValueError):
            install_execution_compat(namespace)
        self.assertIs(namespace._build_awk_program, original)

    def test_missing_builder_rejected(self):
        for namespace in (types.SimpleNamespace(), types.SimpleNamespace(_build_awk_program=3)):
            with self.assertRaises(ValueError):
                install_execution_compat(namespace)


class ActualConsumptionReceiptTests(unittest.TestCase):
    request='20261021154800/20261021162200'
    def fresh(self):
        from test_analyzer_contract import synthetic_contract_audit
        contract=synthetic_contract_audit()
        return synthetic_runtime_audit(self.request,contract['contract_fingerprint'])
    def validate(self,value):
        from test_analyzer_contract import synthetic_contract_audit
        return validate_execution_compat_runtime_audit(value,request_time_range=self.request,
                    contract_fingerprint=synthetic_contract_audit()['contract_fingerprint'])
    def test_exact_test_receipt_accepted(self):
        self.assertEqual(self.validate(self.fresh())['total_mawk_calls'],1)
    def test_parent_only_or_missing_child_rejected(self):
        for value in (expected_execution_compat_audit(),dict(self.fresh(),workers=[]),
                      dict(self.fresh(),total_mawk_calls=0)):
            with self.subTest(value=value.keys()),self.assertRaises(ValueError):self.validate(value)
    def test_wrong_window_nonce_pid_or_contract_rejected(self):
        for field,value in (('request_time_range','20261021145000/20261021152400'),
                            ('nonce','b'*32),('parent_pid',200),('pid',100)):
            audit=self.fresh();audit['workers'][0]['record'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate(_rehash(audit))
    def test_unconsumed_generated_program_rejected(self):
        audit=self.fresh();audit['workers'][0]['record']['calls'][0]['consumption']=None
        with self.assertRaises(ValueError):self.validate(_rehash(audit))
    def test_failed_mawk_and_swallowed_exception_rejected(self):
        for kind in ('nonzero','child_error','parent_error'):
            audit=self.fresh();record=audit['workers'][0]['record']
            if kind=='nonzero':record['calls'][0]['consumption']['returncode']=1
            elif kind=='child_error':record['errors'].append('caught_by_native')
            else:audit['session']['errors'].append('caught_by_native')
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.validate(_rehash(audit))
    def test_program_before_after_and_ini_tampering_rejected(self):
        for field,value in (('program_sha256','a'*64),('program_after_sha256','a'*64),
                            ('actual_ini',{'time_range':'wrong'})):
            audit=self.fresh();audit['workers'][0]['record']['calls'][0]['consumption'][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.validate(_rehash(audit))
    def test_actual_task_or_spawn_constraints_rejected(self):
        for kind in ('window','function','context','workers','count'):
            audit=self.fresh();session=audit['session']
            if kind=='window':session['submissions'][0]['epoch_range']=[0,1]
            elif kind=='function':session['submissions'][0]['function']='other.task'
            elif kind=='context':session['pools'][0]['start_method']='fork'
            elif kind=='workers':session['pools'][0]['actual_max_workers']=3
            else:session['submissions'][0]['input_path_count']=0
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.validate(_rehash(audit))
    def test_old_v6_static_only_receipt_rejected(self):
        audit=self.fresh();audit['session']['installation']['policy']='portable-brake-hex-v1'
        with self.assertRaises(ValueError):self.validate(_rehash(audit))


@unittest.skipUnless(shutil.which('mawk'), 'native mawk required; covered in Linux/WSL')
class RealMawkCompatTests(unittest.TestCase):
    def test_all_65536_byte_pairs_equal_independent_integer_oracle(self):
        cases = [(f'{a:02X}', f'{b:02x}') for a in range(256) for b in range(256)]
        self.assertEqual(_run(['mawk'], PATCHED_CLASSIFIER, cases),
                         [_python_brake(a, b) for a, b in cases])

    def test_case_and_leading_zero_variants(self):
        cases = [('53', '30'), ('53', '31'), ('aB', 'cD'),
                 ('Ab', 'Cd'), ('00000053', '00000030'), ('2', '0'), ('3', '1')]
        self.assertEqual(_run(['mawk'], PATCHED_CLASSIFIER, cases),
                         [_python_brake(a, b) for a, b in cases])

    def test_malformed_and_empty_inputs_keep_original_mawk_coercion(self):
        invalid = ['', 'GG', '0x53', ' 53', '-53', '53.5', '53GG', '30tail', '+53']
        cases = [(a,b) for a in invalid for b in invalid + ['30','31']]
        cases += [('53', b) for b in invalid]
        self.assertEqual(_run(['mawk'], PATCHED_CLASSIFIER, cases),
                         _run(['mawk'], ORIGINAL_CLASSIFIER, cases))

    @unittest.skipUnless(shutil.which('gawk'), 'optional independent hex-capable AWK oracle')
    def test_hex_capable_original_classifier_is_unchanged(self):
        cases = [(f'{a:02X}', f'{b:02x}') for a in range(256) for b in range(256)]
        invalid = ['', 'GG', '0x53', ' 53', '-53', '53.5', '53GG', '30tail', '+53']
        cases += [(a,b) for a in invalid for b in invalid + ['30','31']]
        cases += [('53', b) for b in invalid]
        self.assertEqual(_run(['gawk','--non-decimal-data'], PATCHED_CLASSIFIER, cases),
                         _run(['gawk','--non-decimal-data'], ORIGINAL_CLASSIFIER, cases))


if __name__ == '__main__':
    unittest.main()
