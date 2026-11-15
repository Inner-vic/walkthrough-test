"""Validate the complete sanitized distribution and reject unexpected behavior."""
import ast,shutil,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SCRIPTS=ROOT/'workspace/runtime/wtr096-hardening/skill/scripts'
sys.path.insert(0,str(SCRIPTS))
import analyzer_contract as contract
from run_m100 import slim_platform_config

class BundledAnalyzerTests(unittest.TestCase):
    def test_full_and_job_slim_sources_are_pinned(self):
        source=ROOT/'workspace/runtime/approved-full-analyzer'
        receipt=contract.validate_analyzer_contract(source)
        self.assertEqual(len(receipt['files']),12)
        self.assertEqual(receipt['configuration_variant'],'archive_sanitized')
        with tempfile.TemporaryDirectory() as td:
            dest=Path(td)/'analyzer';shutil.copytree(source,dest)
            slim_platform_config(dest/'general_performance/config.py')
            slim=contract.validate_analyzer_contract(dest)
            self.assertEqual(slim['configuration_variant'],'archive_sanitized_slim')
            self.assertEqual(slim['contract_fingerprint'],receipt['contract_fingerprint'])
            p=dest/'common/perf_utils.py';p.write_bytes(p.read_bytes()+b'\nUNEXPECTED_CHANGE=True\n')
            with self.assertRaises(ValueError):contract.validate_analyzer_contract(dest)

    def test_no_private_fallbacks_and_original_extraction_method(self):
        source=ROOT/'workspace/runtime/approved-full-analyzer'
        for p in source.rglob('*.py'):
            tree=ast.parse(p.read_text('utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node,ast.keyword) and node.arg=='fallback' and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
                    self.assertFalse(node.value.value)
        tree=ast.parse((source/'common/perf_csv.py').read_text('utf-8-sig'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RecordPerformance')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='extract_logs')
        self.assertEqual(contract.ast_digest(ast.Module(body=[method],type_ignores=[])),contract.EXTRACT_LOGS_AST_SHA256)

if __name__=='__main__':unittest.main()
