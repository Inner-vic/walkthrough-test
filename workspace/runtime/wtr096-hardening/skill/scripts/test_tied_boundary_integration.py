"""Actual approved selector + production installers, synthetic files only.

The fixture below embeds only the approved extraction method and original pure
time predicates. It is executed in an in-memory module/linecache, never offered
as a production analyzer_root or a bypass of the whole-source contract. This
tests selection/input identity, not all 201 statistical calculations.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import fnmatch
import hashlib
import inspect
import json
import linecache
import os
from pathlib import Path
import shutil
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from unittest.mock import patch
import uuid

import analyzer_contract
import native_selection
import run_m100
import runtime_audit

APPROVED_METHOD_SOURCE = '    def extract_logs(self, theme, soc_type):\n        """按 theme/soc_type 提取日志文件到 {log_path}/log/{log_dir_name} 目录\n\n        同一 log_dir_name 加锁串行提取，不同目录可并发。已提取过则直接返回现有列表。\n        每个文件名按时间范围过滤，第一个超过 end 的文件保留作为边界。\n\n        Args:\n            theme (str): 主题名\n            soc_type (str | list): 单个前缀或多个前缀列表；列表时 log_dir_name 取 theme\n\n        Returns:\n            list[str]: 提取后的 .log 文件绝对路径列表，按文件名排序\n        """\n        # soc_type 可以是 str（单个前缀）或 list（多个前缀，逗号分隔而来）\n        log_dir_name = self._resolve_log_dir_name(theme, soc_type)\n        if isinstance(soc_type, list):\n            soc_type_list = soc_type\n        else:\n            soc_type_list = [soc_type]\n\n        # 仅对当前的 log_dir_name 加锁，不影响其他目录的并发执行\n        with soc_locks_dict_lock:\n            if log_dir_name not in soc_locks_dict:\n                soc_locks_dict[log_dir_name] = threading.Lock()\n            current_soc_lock = soc_locks_dict[log_dir_name]\n\n        with current_soc_lock:\n            extract_log_path = join(self.log_path, \'log\', log_dir_name)\n\n            # 已提取过则直接返回对应列表\n            if log_dir_name in log_type_list:\n                extract_log_path = list(f.as_posix() for f in Path(extract_log_path).glob(\'**/*.log\'))\n                return sorted(extract_log_path, key=lambda x: Path(x).name)\n\n            logger.warning(f"🚩 extract theme:{theme} to {log_dir_name} doing")\n            log_type_list.append(log_dir_name)\n            make_dirs(extract_log_path)\n\n            # 遍历所有 soc_type，复用 path_config 将日志放入目录\n            for single_soc_type in soc_type_list:\n                # 若 single_soc_type 是具体日志文件名则直接使用，否则从 path_config 获取文件名列表\n                valid_filenames = self.path_config.get(single_soc_type, [single_soc_type])\n\n                for file_name in valid_filenames:\n                    have_file_name = False\n                    state = {"kept_first_after_end": False}\n\n                    log_paths_list = list(Path(self.log_path).glob(f"{file_name}*.log"))\n                    log_paths_list.sort(key=lambda p: (lambda dt: (dt is not None, dt))(_parse_cn_dt_from_log_path(p)))\n\n                    for log_path in log_paths_list:\n                        if not should_keep_log(log_path, self.time_range, state):\n                            continue\n                        try:\n                            target_path = os.path.join(extract_log_path, os.path.basename(log_path))\n                            if not os.path.exists(target_path):\n                                # 优先硬链接（快且不占空间）；跨分区退化到软链接或复制\n                                try:\n                                    os.link(log_path, target_path)\n                                except OSError:\n                                    try:\n                                        os.symlink(log_path, target_path)\n                                    except Exception:\n                                        shutil.copy2(log_path, target_path)\n                            have_file_name = True\n                        except Exception as e:\n                            logger.error(f"复制日志文件失败: {log_path}")\n                            logger.exception(e)\n                    if have_file_name:\n                        logger.warning(f"🚩 extract {file_name} => {extract_log_path}")\n\n            extract_log_path = list(f.as_posix() for f in Path(extract_log_path).glob(\'**/*.log\'))\n            return sorted(extract_log_path, key=lambda x: Path(x).name)\n\n'
PURE_TIME_SOURCE = '_TS_RE = re.compile(r"(\\d{4}_\\d{2}_\\d{2}_\\d{2}_\\d{2}_\\d{2})(?=\\D|$)")\n\ndef _parse_cn_dt_from_log_path(log_path) -> datetime:\n    """从文件名提取 YYYY_MM_DD_HH_MM_SS 时间戳并解析为中国时区时间\n\n    Args:\n        log_path (Path | str): 日志文件路径，取其 name 部分匹配时间戳\n\n    Returns:\n        datetime | None: 解析成功返回中国时区（Asia/Shanghai）的 datetime；\n                         文件名无时间戳返回 None\n    """\n    name = getattr(log_path, "name", None) or str(log_path)\n    matches = list(_TS_RE.finditer(name))\n    if not matches:\n        return None\n    dt = datetime.strptime(matches[-1].group(1), "%Y_%m_%d_%H_%M_%S")\n    return CN_TZ.localize(dt)\n\ndef should_keep_log(log_path, time_range, state: dict) -> bool:\n    """判断 log_path 是否需要保留（把所有判断过程封装在这里）\n\n    规则：\n    - 文件名无时间：保留\n    - 文件名时间 < start：丢弃\n    - 文件名时间 > end：第一个超过 end 的保留；第二个及之后丢弃\n\n    Args:\n        log_path (Path | str): 日志文件路径\n        time_range (tuple | None): (start_ts, end_ts) 的 epoch seconds（中国时区解释），None 表示不过滤\n        state (dict): 跨文件复用的状态字典，需在循环外创建并传入，\n                      用于记录 \'kept_first_after_end\' 标志位\n\n    Returns:\n        bool: True 保留该文件，False 跳过该文件\n    """\n    if not time_range:\n        return True\n\n    start_ts, end_ts = time_range\n    start_dt = datetime.fromtimestamp(start_ts, tz=CN_TZ)\n    end_dt = datetime.fromtimestamp(end_ts, tz=CN_TZ)\n\n    file_dt = _parse_cn_dt_from_log_path(log_path)\n    if file_dt is None:\n        return True\n\n    if file_dt < start_dt:\n        return False\n\n    if file_dt > end_dt:\n        if state is None:\n            state = {}\n        if not state.get("kept_first_after_end", False):\n            state["kept_first_after_end"] = True\n            return True\n        return False\n\n    return True\n\n'
ARCHIVE_TIME_SOURCE = 'def is_file_time_in_range(filename, time_range):\n    """判断文件名中的时间是否在 time_range 范围内，end 向后扩展 1 小时\n\n    Args:\n        filename (str): 文件名，需包含 YYYY_MM_DD_HH_MM_SS 格式的时间戳\n        time_range (str | None): \'start/end\' 格式的时间范围，如 \'20260710123300/20260710174100\'；\n                                  None 表示不校验，直接返回 True\n\n    Returns:\n        bool: True 表示在范围内（含 end + 1 小时扩展）\n\n    Raises:\n        ValueError: 文件名中未找到 YYYY_MM_DD_HH_MM_SS 时间信息\n    """\n    if time_range is None:\n        return True\n    start_time_str, end_time_str = time_range.split(\'/\')\n    start_time = datetime.strptime(start_time_str, "%Y%m%d%H%M%S")\n    end_time = datetime.strptime(end_time_str, "%Y%m%d%H%M%S") + timedelta(hours=1)\n\n    match = re.search(r"(\\d{4})_(\\d{2})_(\\d{2})_(\\d{2})_(\\d{2})_(\\d{2})", filename)\n    if not match:\n        raise ValueError("文件名中未找到时间信息")\n\n    file_time = datetime.strptime(\'\'.join(match.groups()), "%Y%m%d%H%M%S")\n    return start_time <= file_time <= end_time\n\n'
WINDOW = '20261023150000/20261023153500'
CASE_EVIDENCE = []

SUPPORT = '''
from pathlib import Path
from os.path import join
from datetime import datetime, timedelta
import os, shutil, threading, re, pytz
from types import SimpleNamespace
CN_TZ = pytz.timezone("Asia/Shanghai")
soc_locks_dict = {}
soc_locks_dict_lock = threading.Lock()
log_type_list = []
logger = SimpleNamespace(warning=lambda *a: None, error=lambda *a: None, exception=lambda *a: None)
def make_dirs(path):
    os.makedirs(path, exist_ok=True)
'''
CLASS_SUPPORT = '''
class RecordPerformance:
    def __init__(self, log_path, theme_lists, time_range, path_config, config_date):
        self.log_path = log_path
        self.theme_lists = theme_lists
        self.time_range = time_range
        self.path_config = path_config
        self.config_date = config_date
    def _resolve_log_dir_name(self, theme, soc_type):
        return theme if isinstance(soc_type, list) else soc_type
'''


def signature(paths):
    return {Path(path).name: {'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                              'bytes': Path(path).stat().st_size} for path in paths}


def rows_for(window=WINDOW, *, tied=True, both_sources=False):
    start, end = [datetime.strptime(value, '%Y%m%d%H%M%S') for value in window.split('/')]
    stamp = lambda value: value.strftime('%Y_%m_%d_%H_%M_%S')
    rows = [('vla_A_inside_' + stamp(start + timedelta(minutes=1)) + '.log', 'log_fsdA')]
    boundary = end + timedelta(minutes=1)
    rows += [('vla_A_first_' + stamp(boundary) + '.log', 'log_fsdA'),
             ('vla_A_second_' + stamp(boundary if tied else boundary + timedelta(minutes=1)) + '.log', 'log_fsdA'),
             ('vla_A_later_' + stamp(boundary + timedelta(minutes=5)) + '.log', 'log_fsdA')]
    if both_sources:
        rows += [('vla_B_inside_' + stamp(start + timedelta(minutes=2)) + '.log', 'log_fsdB'),
                 ('vla_B_first_' + stamp(boundary + timedelta(minutes=2)) + '.log', 'log_fsdB'),
                 ('vla_B_second_' + stamp(boundary + timedelta(minutes=2)) + '.log', 'log_fsdB'),
                 ('vla_B_later_' + stamp(boundary + timedelta(minutes=6)) + '.log', 'log_fsdB')]
    return rows


class NativeCase:
    def __init__(self, root, *, window=WINDOW, tied=True, reverse=False,
                 both_sources=False, overlap=False, data_label='same-fixture'):
        self.root = Path(root).resolve()
        self.raw = self.root / 'raw'
        self.flat = self.root / 'native-input' / 'perf'
        self.flat.mkdir(parents=True)
        self.window = window
        self.rows = rows_for(window, tied=tied, both_sources=both_sources)
        self.order = [name for name, _ in self.rows]
        if reverse:
            for source in ('A', 'B'):
                left = next((i for i, name in enumerate(self.order) if name.startswith('vla_' + source + '_first_')), None)
                right = next((i for i, name in enumerate(self.order) if name.startswith('vla_' + source + '_second_')), None)
                if left is not None and right is not None:
                    self.order[left], self.order[right] = self.order[right], self.order[left]
        self.config = {'ALL': ['vla_'], 'AONLY': ['vla_A_']}
        self.rules = {'first': {'sample': {'soc_type': 'ALL'}}}
        if overlap:
            self.rules.update(again={'sample': {'soc_type': 'ALL'}}, only_A={'sample': {'soc_type': 'AONLY'}})
        files = []
        for index, (name, source) in enumerate(self.rows):
            payload = (data_label + ':' + name + '\n').encode()
            raw = self.raw / source / ('item-' + str(index)) / name
            raw.parent.mkdir(parents=True)
            raw.write_bytes(payload)
            (self.flat / name).write_bytes(payload)
            files.append(raw)
        self.selector_root = self.root / 'pure-selector-source'
        (self.selector_root / 'common').mkdir(parents=True)
        (self.selector_root / 'general_performance').mkdir()
        (self.selector_root / 'common/perf_utils.py').write_text(PURE_TIME_SOURCE, encoding='utf-8')
        (self.selector_root / 'common/perf_path.py').write_text(ARCHIVE_TIME_SOURCE, encoding='utf-8')
        (self.selector_root / 'general_performance/config.py').write_text('CONFIG_PATH=' + repr(self.config), encoding='utf-8')
        start, end = window.split('/')
        selector = run_m100.NativeLogSelection(self.selector_root, self.rules, start, end)
        self.inventory = run_m100.validate_log_inventory(files, selector, self.raw)
        self.ini = self.root / 'o.ini'
        self.ini.write_text('[PARAMETER]\ntime=' + window + '\nlogPath=' + str(self.flat) + '\n', encoding='utf-8')
        self.module = self._module()
        self.guard = analyzer_contract.validate_extract_logs_contract(inspect.getsource(self.module.RecordPerformance.extract_logs))
        if self.guard['runtime_extract_logs_ast_sha256'] != analyzer_contract.RUNTIME_EXTRACT_LOGS_AST_SHA256:
            raise AssertionError('Fixture did not pass the production complete-method AST guard')
        self.source_audit = native_selection.install_source_boundary_patch(
            self.module, self.inventory['source_by_basename'],
            excluded_basenames=[row['basename'] for row in self.inventory.get('excluded_conflicts', [])])
        self.value = types.SimpleNamespace(get_theme_lists=lambda values: list(values),
            get_theme=lambda values, theme: (True, values[theme]['sample']['soc_type'], []))

    def _module(self):
        source = SUPPORT + '\n' + PURE_TIME_SOURCE + '\n' + CLASS_SUPPORT + APPROVED_METHOD_SOURCE
        name = '_approved_tie_selector_' + uuid.uuid4().hex
        filename = str(self.root / (name + '.py'))
        module = types.ModuleType(name)
        module.__file__ = filename
        sys.modules[name] = module
        linecache.cache[filename] = (len(source), None, source.splitlines(keepends=True), filename)
        exec(compile(source, filename, 'exec'), module.__dict__)
        self.module_name, self.fixture_file = name, filename
        return module

    def close(self):
        sys.modules.pop(self.module_name, None)
        linecache.cache.pop(self.fixture_file, None)

    def runtime_kwargs(self):
        return dict(request_time_range=self.window, inventory=self.inventory, rules=self.rules,
                    path_config=self.config, flat_log_root=self.flat, ini_path=self.ini)

    def observe(self, module=runtime_audit):
        self.controller = module.install_runtime_audit(self.module, self.value, **self.runtime_kwargs())
        return self.controller

    def record(self):
        return self.module.RecordPerformance(str(self.flat), list(self.rules),
            runtime_audit.request_epochs(self.window), self.config, self.rules)

    def identities(self, paths):
        return {name: {**row, 'source': self.inventory['source_by_basename'][name]}
                for name, row in signature(paths).items()}

    def run(self, *, observer=True):
        controller = self.observe() if observer else None
        record = self.record()
        results = {theme: self.identities(record.extract_logs(theme, self.rules[theme]['sample']['soc_type'])) for theme in self.rules}
        return results, controller.finalize() if controller else None


@contextmanager
def ordered_globs(cases, callback=None):
    original = Path.glob
    by_root = {str(case.flat): case for case in cases}
    def controlled(path, pattern, *args, **kwargs):
        case = by_root.get(str(path.absolute()))
        if case is None:
            return original(path, pattern, *args, **kwargs)
        if callback is not None:
            callback(case, pattern)
        return iter([case.flat / name for name in case.order if fnmatch.fnmatchcase(name, pattern)])
    with patch.object(Path, 'glob', controlled):
        yield


def evidence(case_id, order, baseline, actual, receipt):
    if baseline != actual:
        raise AssertionError('Actual extraction inputs changed from approved native stable selection')
    flat = {name: row for values in actual.values() for name, row in values.items()}
    baseline_flat = {name: row for values in baseline.values() for name, row in values.items()}
    body_sha = lambda values: hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    row = {'case_id': case_id, 'order': list(order), 'baseline_same': True,
           'actual_selected_names': sorted(flat), 'actual_members': flat,
           'baseline_members': baseline_flat, 'actual_body_signature': body_sha(flat),
           'baseline_body_signature': body_sha(baseline_flat),
           'runtime_policy': receipt['policy'],
           'scope_proofs_sha256': hashlib.sha256(json.dumps(receipt['scope_proofs'], sort_keys=True).encode()).hexdigest(),
           'scope_proofs_count': len(receipt['scope_proofs']),
           'selection_modes': sorted({row['selection_mode'] for row in receipt['extractions']})}
    CASE_EVIDENCE.append(row)
    return row


class TiedBoundaryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def case(self, label, **kwargs):
        result = NativeCase(self.root / label, **kwargs)
        self.addCleanup(result.close)
        return result

    def compare(self, case_id, **kwargs):
        before = self.case(case_id + '-native', **kwargs)
        after = self.case(case_id + '-audited', **kwargs)
        with ordered_globs([before, after]):
            baseline, _ = before.run(observer=False)
            actual, receipt = after.run(observer=True)
        self.assertEqual(baseline, actual)
        evidence(case_id, after.order, baseline, actual, receipt)
        return after, actual, receipt

    def test_exact_approved_method_guard_rejects_modified_selector(self):
        changed = APPROVED_METHOD_SOURCE.replace('if not should_keep_log(', 'if False and not should_keep_log(', 1)
        with self.assertRaises(ValueError):
            analyzer_contract.validate_extract_logs_contract(changed)

    def test_no_tie_matches_approved_native_input_selection(self):
        self.compare('no_tie', tied=False)

    def test_same_source_boundary_forward_keeps_first_native_glob_entry(self):
        case, actual, receipt = self.compare('same_source_forward', tied=True, reverse=False)
        self.assertTrue(any('_first_' in name for name in actual['first']))
        self.assertFalse(any('_second_' in name for name in actual['first']))

    def test_same_source_boundary_reverse_keeps_first_native_glob_entry(self):
        case, actual, receipt = self.compare('same_source_reverse', tied=True, reverse=True)
        self.assertTrue(any('_second_' in name for name in actual['first']))
        self.assertFalse(any('_first_' in name for name in actual['first']))

    def test_each_source_has_its_own_native_tied_boundary(self):
        case, actual, receipt = self.compare('both_sources_reverse', tied=True, reverse=True, both_sources=True)
        names = set(actual['first'])
        self.assertEqual(sum('_second_' in name for name in names), 2)
        self.assertEqual(sum('_first_' in name for name in names), 0)

    def test_overlapping_prefixes_and_cached_calls_keep_the_same_inputs(self):
        case, actual, receipt = self.compare('overlap_and_cache', tied=True, reverse=True, both_sources=True, overlap=True)
        self.assertEqual(actual['first'], actual['again'])
        self.assertEqual(actual['only_A'], {name: row for name, row in actual['first'].items() if name.startswith('vla_A_')})
        cached = next(row for row in receipt['extractions'] if row['theme'] == 'again')
        original = next(row for row in receipt['extractions'] if row['theme'] == 'first')
        self.assertEqual(cached['selection_mode'], 'cached_scope')
        self.assertEqual(cached['scope_proof_id'], original['scope_proof_id'])

    def test_preexisting_unobserved_cache_is_rejected(self):
        case = self.case('unobserved-cache', tied=True)
        controller = case.observe()
        record = case.record()
        target = case.flat / 'log' / 'ALL'
        target.mkdir(parents=True)
        case.module.log_type_list.append('ALL')
        with ordered_globs([case]), self.assertRaises(runtime_audit.RuntimeAuditError):
            record.extract_logs('first', 'ALL')
        with self.assertRaises(runtime_audit.RuntimeAuditError):
            controller.finalize()

    def test_lost_tied_candidate_is_rejected_before_becoming_no_samples(self):
        case = self.case('lost-candidate', tied=True)
        controller = case.observe()
        name = next(name for name in case.order if '_second_' in name)
        (case.flat / name).unlink()
        with self.assertRaises((runtime_audit.RuntimeAuditError, FileNotFoundError)):
            case.record()
        with self.assertRaises((runtime_audit.RuntimeAuditError, FileNotFoundError)):
            controller.finalize()

    def test_changed_actual_selected_content_is_rejected(self):
        case = self.case('changed-selected', tied=True)
        controller = case.observe()
        record = case.record()
        name = next(name for name in case.order if '_first_' in name)
        (case.flat / name).write_bytes(b'tampered selected body')
        with ordered_globs([case]), self.assertRaises(runtime_audit.RuntimeAuditError):
            record.extract_logs('first', 'ALL')
        with self.assertRaises(runtime_audit.RuntimeAuditError):
            controller.finalize()

    def test_swallowed_actual_native_copy_failure_is_rejected(self):
        case = self.case('native-copy-failure', tied=True)
        controller = case.observe()
        record = case.record()
        with ordered_globs([case]), patch.object(os, 'link', side_effect=OSError('synthetic link failure')), \
                patch.object(os, 'symlink', side_effect=OSError('synthetic symlink failure')), \
                patch.object(shutil, 'copy2', side_effect=OSError('synthetic copy failure')):
            with self.assertRaises(runtime_audit.RuntimeAuditError):
                record.extract_logs('first', 'ALL')
        with self.assertRaises(runtime_audit.RuntimeAuditError):
            controller.finalize()

    def test_concurrent_same_scope_cache_requires_completed_proof(self):
        native = self.case('same-scope-native', tied=True, overlap=True)
        case = self.case('same-scope-audited', tied=True, overlap=True)
        with ordered_globs([native]):
            baseline, _ = native.run(observer=False)
        controller = case.observe()
        record = case.record()
        entered, allow, second_started = threading.Event(), threading.Event(), threading.Event()
        blocked = [False]
        def callback(which, pattern):
            if which is case and pattern == 'vla_*.log' and not blocked[0]:
                blocked[0] = True
                entered.set()
                if not allow.wait(5):
                    raise AssertionError('Synthetic concurrent cache rendezvous timed out')
        def second():
            second_started.set()
            return record.extract_logs('again', 'ALL')
        with ordered_globs([case], callback), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(record.extract_logs, 'first', 'ALL')
            self.assertTrue(entered.wait(5))
            cached = pool.submit(second)
            self.assertTrue(second_started.wait(5))
            allow.set()
            actual = {'first': case.identities(first.result(timeout=5)), 'again': case.identities(cached.result(timeout=5))}
            actual['only_A'] = case.identities(record.extract_logs('only_A', 'AONLY'))
        receipt = controller.finalize()
        evidence('concurrent_same_scope_cache', case.order, baseline, actual, receipt)

    def test_two_window_modules_execute_in_parallel_without_crossed_inputs(self):
        windows = (WINDOW, '20261023153900/20261023161300')
        originals, cases, baselines = [], [], []
        for index, window in enumerate(windows):
            kwargs = dict(window=window, tied=True, reverse=bool(index), both_sources=True, data_label='window-' + str(index))
            original = self.case('parallel-native-' + str(index), **kwargs)
            case = self.case('parallel-audited-' + str(index), **kwargs)
            originals.append(original)
            cases.append(case)
            with ordered_globs([original]):
                baseline, _ = original.run(observer=False)
                baselines.append(baseline)
        barrier = threading.Barrier(2, timeout=5)
        observed = set()
        lock = threading.Lock()
        def callback(case, pattern):
            if pattern != 'vla_*.log':
                return
            with lock:
                first = str(case.flat) not in observed
                observed.add(str(case.flat))
            if first:
                barrier.wait()
        with ordered_globs(cases, callback), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(case.run) for case in cases]
            results = [future.result(timeout=10) for future in futures]
        for index, (case, baseline, (actual, receipt)) in enumerate(zip(cases, baselines, results)):
            self.assertEqual(receipt['request_time_range'], windows[index])
            evidence('parallel_window_' + str(index), case.order, baseline, actual, receipt)
        self.assertNotEqual(results[0][0], results[1][0])


if __name__ == '__main__':
    unittest.main()
