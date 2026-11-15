"""Finite parsing budgets and their actual spawn receipts; no cloud or reports."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from windows_native_tools import install_parsing_scheduler, parsing_worker_budget
from native_windows_host import validate_parsing_scheduler


class ParsingSchedulerTests(unittest.TestCase):
    def test_two_window_budget_is_finite_on_small_unknown_and_large_hosts(self):
        for cpu,expected in [(None,1),(1,1),(2,1),(4,1),(8,2),(16,4),(32,4),(128,4)]:
            with self.subTest(cpu=cpu):
                self.assertEqual(parsing_worker_budget(cpu),expected)
                if cpu and cpu>=4:self.assertLessEqual(2*expected,cpu//2)
        for value in (0,-1,True,1.0,'16'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                parsing_worker_budget(value)

    def test_only_scheduler_is_replaced_and_every_request_has_a_persisted_receipt(self):
        with TemporaryDirectory() as temporary:
            trace=Path(temporary)/'compat-trace'
            trace.mkdir()
            (trace/'session.json').write_text('{"pools":[]}',encoding='utf-8')
            original_trace={p.name:p.read_bytes() for p in trace.iterdir()}
            untouched=object()
            module=SimpleNamespace(get_dynamic_max_processes=lambda:2,txt_process_tool=untouched)
            with patch('windows_native_tools.os.cpu_count',return_value=16):
                install_parsing_scheduler(module,trace)
            self.assertIs(module.txt_process_tool,untouched)
            path=trace.with_name('windows-parsing-scheduler.json')
            self.assertEqual(json.loads(path.read_text())['requested_workers'],[])
            self.assertEqual(module.get_dynamic_max_processes(),4)
            receipt=json.loads(path.read_text())
            self.assertEqual({p.name:p.read_bytes() for p in trace.iterdir()},original_trace,
                'Scheduling evidence must not alter the strict native consumption trace inventory')
            pools=[{'start_method':'spawn','requested_max_workers':4,'actual_max_workers':4}]
            self.assertEqual(validate_parsing_scheduler(receipt,pools),receipt)
            with self.assertRaises(ValueError):install_parsing_scheduler(module,trace)

    def test_unconsumed_mutated_or_unbounded_receipts_are_rejected(self):
        receipt={'policy':'windows-native-bounded-parsing-v1','cpu_count':16,'concurrent_windows':2,
                 'max_workers_per_window':4,'requested_workers':[4]}
        pools=[{'start_method':'spawn','requested_max_workers':4,'actual_max_workers':4}]
        cases=[]
        for key,value in [('cpu_count',True),('concurrent_windows',1),('max_workers_per_window',8),
                          ('requested_workers',[]),('requested_workers',[8])]:
            bad=deepcopy(receipt);bad[key]=value;cases.append((bad,pools))
        for key,value in [('start_method','fork'),('requested_max_workers',2),('actual_max_workers',8)]:
            bad=deepcopy(pools);bad[0][key]=value;cases.append((receipt,bad))
        cases.extend([(receipt,[]),(receipt,pools*2),(None,pools)])
        for received,actual in cases:
            with self.subTest(received=received,actual=actual),self.assertRaises(ValueError):
                validate_parsing_scheduler(received,actual)


if __name__=='__main__':unittest.main()
