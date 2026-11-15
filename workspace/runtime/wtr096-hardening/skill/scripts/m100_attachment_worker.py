"""Intercept the native statistics-to-publication boundary and export HTML.

Uses the deployment analyzer's parser, filters and percentile implementation.
Does not invoke another business log pipeline or create an online report.
"""
from __future__ import annotations

import argparse
import configparser
from collections import Counter
from datetime import datetime, timezone, timedelta
import copy
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import runpy
import sys
import threading
from urllib.parse import urlsplit

SCENARIOS=(('slow_driver','低速人驾','0'),('fast_driver','高速人驾','4'),('CNOA','CNOA','5'),('parking','泊车','1'))
ANALYSIS_PROFILE='m100-full-native-audited-v8-native-sheets'
TARGET_METRICS=frozenset(('header_delay','vla_parking_header_delay','vlamodel_fps','vlaparkingdlm_fps'))


def validate_full_rules(rules):
    """Validate structure without pruning or interpreting native rule expressions."""
    if not isinstance(rules,dict) or not rules:
        raise ValueError('须提供完整、非空的统计规则快照。')
    for theme,metrics in rules.items():
        if not isinstance(theme,str) or not theme.strip() or not isinstance(metrics,dict) or not metrics:
            raise ValueError('完整统计规则须为非空 theme→metric→字段字典。')
        for metric,fields in metrics.items():
            if not isinstance(metric,str) or not metric.strip() or not isinstance(fields,dict):
                raise ValueError('完整统计规则指标结构无效。')
    return copy.deepcopy(rules)


def full_analysis_scope(rules):
    rules=validate_full_rules(rules)
    return {'profile':ANALYSIS_PROFILE,'source_theme_count':len(rules),
            'source_metric_count':sum(len(v) for v in rules.values()),
            'selected_metrics':{theme:list(metrics) for theme,metrics in rules.items()},
            'selected_rules_sha256':hashlib.sha256(json.dumps(rules,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest(),
            'complete_rule_inventory':True}


def _native_metric_name(key):
    if isinstance(key,str):
        return key
    if isinstance(key,Path) and '+' in key.stem:
        return key.stem.split('+')[1]
    raise ValueError('原分析器统计键结构变化，无法准确关联规则。')


def capture_full_statistics(report,theme_data,rules=None):
    """Use the native formatter, preserving its fields and all rule outcomes.

    Global formatting and the native publication predicate determine what would
    have been published. Per-entry formatting retains diagnostics and statistics
    that that publication step would suppress; it never recomputes a statistic.
    """
    if not isinstance(theme_data,dict):
        raise ValueError('原分析器主题统计结构变化。')
    flattened={}
    raw_owners={}
    sources={}
    for theme,contents in theme_data.items():
        if contents is None:
            continue
        if not isinstance(contents,dict):
            raise ValueError('原分析器主题结果须为字典。')
        for key,fields in contents.items():
            metric=_native_metric_name(key)
            sources.setdefault((str(theme),metric),[]).append((key,fields))
            raw_owners[key]=str(theme)
        flattened.update(contents)
    formatted=report._format_report_data(flattened)
    if not isinstance(formatted,dict):
        raise ValueError('原分析器统计格式化结果须为字典。')
    published_owner={}
    for key,fields in flattened.items():
        metric=_native_metric_name(key)
        if metric in report._format_report_data({key:fields}):
            published_owner.setdefault(metric,raw_owners[key])
    inventory=[(str(theme),metric,fields) for theme,metrics in (rules or {}).items() for metric,fields in metrics.items()]
    known={(theme,metric) for theme,metric,_ in inventory}
    inventory.extend((theme,metric,None) for theme,metric in sources if (theme,metric) not in known)
    rows=[]
    for theme,metric,rule in inventory:
        entries=sources.get((theme,metric),[])
        if len(entries)>1:
            raise ValueError('同一主题内出现重复统计指标，拒绝任意覆盖。')
        local=(report._format_report_data(dict(entries)) if entries else {})
        fields=local.get(metric,{})
        if not isinstance(fields,dict):
            raise ValueError('原分析器指标统计字段结构变化。')
        statistics={str(k):scalar(v) for k,v in fields.items() if k!='image'}
        diagnostic=str(statistics.get('diagnostic') or '')
        missing=not statistics or bool(diagnostic)
        reason=diagnostic
        if not entries:
            reason='原分析器未返回该规则的统计结果；可能缺少源日志或依赖，不能视为零。'
        elif not statistics:
            reason='原生 formatter 未输出该指标（统计全空或无有效字段）；保留规则行供复核。'
        should_skip=(hasattr(report,'_should_skip_emb_type_variable') and
                     report._should_skip_emb_type_variable(metric,formatted))
        skip_reason=''
        if should_skip:
            skip_reason='原生 _should_skip_emb_type_variable 判定该指标不参与上报。'
            if hasattr(report,'_get_emb_type_str'):
                emb=str(report._get_emb_type_str(formatted))
                skip_reason+=' emb_type='+ (emb or '缺失')+'；calculate_type='+str((rule or {}).get('calculate_type',''))+'。'
        duplicate_suppressed=metric in published_owner and published_owner[metric]!=theme
        if duplicate_suppressed:
            skip_reason+=' 原生汇总中的同名指标由主题 '+published_owner[metric]+' 提供；本主题完整字段仍保留。'
        row={'theme':theme,'metric':metric,'statistics':statistics,
             'status':'conditional_skip' if should_skip or duplicate_suppressed else ('missing' if missing else 'available'),
             'data_status':'missing' if missing else 'available',
             'native_publish':metric in formatted and not should_skip and not duplicate_suppressed,
             'diagnostic':reason,'skip_reason':skip_reason,
             'rule':copy.deepcopy(rule) if rule is not None else None}
        if rule is None:
            row['unconfigured_output']=True
        rows.append(row)
    return rows


def select_required_rules(rules):
    """Keep native direct extraction rules verbatim; reject unknown dependencies.

    Current targets are independent custom expressions with embedded state.
    A future derived metric must have its dependencies reviewed before pruning.
    Never hard-code a column number, log tag, threshold or container prefix here.
    """
    if not isinstance(rules,dict) or not rules:
        raise ValueError('须提供完整、非空的统计规则快照。')
    selected={};seen=set()
    for theme,metrics in rules.items():
        if not isinstance(metrics,dict):
            raise ValueError('统计规则主题结构变化，需要适配。')
        for name,fields in metrics.items():
            if not isinstance(name,str) or name.casefold() not in TARGET_METRICS:
                continue
            key=name.casefold()
            if key in seen:
                raise ValueError('完整规则中存在重复目标指标，拒绝任意选择。')
            if (not isinstance(theme,str) or not theme.strip() or not isinstance(fields,dict)
                    or fields.get('func')!='custom' or fields.get('calculate_type')!=''
                    or fields.get('ps_type')!='ps_time'
                    or any(not isinstance(fields.get(k),str) or not fields[k].strip()
                           for k in ('soc_type','log_tag','input'))):
                raise ValueError('目标指标不再是独立 custom/ps_time 规则；须先核对依赖再裁剪。')
            seen.add(key)
            selected.setdefault(theme,{})[name]=copy.deepcopy(fields)
    if seen!=TARGET_METRICS:
        raise ValueError('完整规则缺少必需目标指标，不能将规则缺失当作日志无数据。')
    return selected


def scalar(value):
    if hasattr(value,'item'):
        value=value.item()
    if isinstance(value,float) and not math.isfinite(value):
        return None
    return value if value is None or isinstance(value,(str,int,float,bool)) else str(value)


def number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        value=float(value)
    except (ValueError,TypeError):
        return None
    return value if math.isfinite(value) and value>=0 else None


def collect_coverage(log_dir, time_range, *, runtime_execution=None, rules=None):
    """Observe extracted target CSV rows; this does not assert continuous coverage."""
    tz = timezone(timedelta(hours=8))
    start,end = [datetime.strptime(t,'%Y%m%d%H%M%S').replace(tzinfo=tz).timestamp() for t in time_range.split('/')]
    required = {'header_delay','vla_parking_header_delay','vlamodel_fps','vlaparkingdlm_fps'}
    found = {name:{'rows':0,'state_counts':Counter(),'first':None,'last':None,'unrecognized_rows':0} for name in required}
    for path in Path(log_dir).rglob('*.csv'):
        name = path.stem.split('+')[-1].casefold()
        if name not in required or 'logs_traned' not in path.parts:
            continue
        row = found[name]
        with path.open(encoding='utf-8',errors='replace') as handle:
            for line in handle:
                fields = line.split()
                try:
                    stamp = float(fields[3])
                    state = fields[4].rsplit(',',1)[-1]
                except (ValueError,IndexError):
                    row['unrecognized_rows'] += 1
                    continue
                if start <= stamp <= end:
                    row['rows'] += 1
                    row['state_counts'][state] += 1
                    row['first'] = stamp if row['first'] is None else min(row['first'],stamp)
                    row['last'] = stamp if row['last'] is None else max(row['last'],stamp)
    for row in found.values():
        row['state_counts'] = dict(row['state_counts'])
        for key in ('first','last'):
            if row[key] is not None:
                row[key] = datetime.fromtimestamp(row[key],tz).isoformat(timespec='milliseconds')
    result = {'method':'extracted_target_csv_rows_before_native_cleaning',
              'continuous_coverage_verified':False,'targets':found}
    if runtime_execution is not None and rules is not None:
        result['field_presence'] = _collect_target_field_presence(
            log_dir, time_range, runtime_execution, rules)
    return result


def _collect_target_field_presence(log_dir, time_range, runtime_execution, rules):
    """Observe the completed native CSVs; never run a second target parser.

    Only exact rule-owned outputs in the finalized native runtime are read.
    An extracted state and explicit numeric/empty value are required evidence;
    a tag hit, generated filename, missing column or an unknown token is not.
    """
    from runtime_audit import canonical_sha, request_epochs
    epochs = request_epochs(time_range)
    root = (Path(log_dir).resolve() / 'perf')
    if (runtime_execution.get('status') != 'verified'
            or runtime_execution.get('request_time_range') != time_range
            or runtime_execution.get('expected_epoch_range') != epochs
            or Path(runtime_execution.get('flat_log_root', '')).resolve() != root
            or runtime_execution.get('rules_sha256') != canonical_sha(rules)):
        raise ValueError('字段识别证据与完整原生执行窗口或规则不一致。')
    bindings = {}
    for theme, rows in rules.items():
        if Path(theme).name != theme or theme in ('.', '..') or '/' in theme or '\\' in theme:
            raise ValueError('字段识别证据的主题路径无效。')
        for metric, rule in rows.items():
            name = metric.casefold()
            if name not in TARGET_METRICS:
                continue
            if name in bindings:
                raise ValueError('字段识别证据含重复目标规则。')
            bindings[name] = (theme, metric, rule)
    number_token = re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
    empty_tokens = frozenset(('-', 'n/a', 'nan', 'null', 'none'))
    observed_themes = {row.get('theme') for row in runtime_execution.get('extractions', [])}
    result = {}
    for name in sorted(TARGET_METRICS):
        evidence = {'status':'unparsed', 'observations':0, 'numeric_observations':0,
                    'empty_observations':0, 'scope':'requested_window',
                    'evidence':'audited_native_target_csv_value_and_state',
                    'request_time_range':time_range, 'rules_sha256':canonical_sha(rules),
                    'runtime_execution_sha256':canonical_sha(runtime_execution),
                    'csv_files':[], 'scan_complete':True}
        binding = bindings.get(name)
        if binding is not None:
            evidence['rule_sha256'] = canonical_sha({'theme':binding[0], 'metric':binding[1], 'rule':binding[2]})
        if binding is None or binding[0] not in observed_themes:
            evidence['scan_complete'] = False
        else:
            theme, metric, rule = binding
            folder = root / 'logs_traned' / theme
            paths = sorted(folder.glob('*+' + metric + '.csv')) if folder.is_dir() else []
            if len(paths) > 1:
                raise ValueError('字段识别证据含重复原生目标CSV。')
            for path in paths:
                if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root)
                        or any(parent.is_symlink() or getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400
                               for parent in (path, folder, folder.parent, root))):
                    raise ValueError('字段识别证据的原生CSV路径无效。')
                before = path.stat()
                checksum = hashlib.sha256()
                try:
                    with path.open('rb') as handle:
                        for raw in handle:
                            checksum.update(raw)
                            try:
                                fields = raw.decode('utf-8', errors='strict').split()
                                stamp = float(fields[3])
                                value, state = fields[4].rsplit(',', 1)
                            except (UnicodeDecodeError, ValueError, IndexError):
                                continue
                            if (not math.isfinite(stamp) or not epochs[0] <= stamp <= epochs[1]
                                    or not number_token.fullmatch(state) or not math.isfinite(float(state))):
                                continue
                            if value.casefold() in empty_tokens:
                                evidence['empty_observations'] += 1
                            elif number_token.fullmatch(value) and math.isfinite(float(value)):
                                evidence['numeric_observations'] += 1
                    after = path.stat()
                except OSError:
                    evidence.update(scan_complete=False, numeric_observations=0, empty_observations=0)
                    continue
                if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
                        != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)):
                    raise ValueError('字段识别证据收集期间原生CSV发生变化。')
                evidence['csv_files'].append({'relative_path':path.relative_to(root).as_posix(),
                    'bytes':after.st_size, 'sha256':checksum.hexdigest()})
        evidence['observations'] = evidence['numeric_observations'] + evidence['empty_observations']
        if evidence['scan_complete'] and evidence['observations']:
            evidence['status'] = 'parsed'
        evidence['evidence_sha256'] = canonical_sha(evidence)
        result[name] = evidence
    return result


def export_result(records,metadata,output,coverage=None,analysis_scope=None,origin='native_statistics_before_feishu_publish'):
    indexed={}
    required={'header_delay','vla_parking_header_delay','vlamodel_fps','vlaparkingdlm_fps'}
    for row in records:
        name=row['metric'].casefold()
        # Native summaries may contain distinct non-target metrics differing
        # only in case, e.g. trafficlight_fps and TrafficLight_FPS. Preserve
        # both in the full report; only target lookups need case folding.
        if name not in required:
            continue
        if row.get('native_publish') is False:
            continue
        if name in indexed:
            raise ValueError('汇总统计含同名目标指标，不能任意选一个。')
        indexed[name]=row['statistics']
    metrics={}
    for key,label,state in SCENARIOS:
        delay='vla_parking_header_delay' if key=='parking' else 'header_delay'
        fps='vlaparkingdlm_fps' if key=='parking' else 'vlamodel_fps'
        p99=number(indexed.get(delay,{}).get('P99_'+state))
        mean=number(indexed.get(fps,{}).get('mean_'+state))
        metrics[key]={'label':label,'p99_ms':p99,'frame_rate_hz':mean,
                      'p99_source':{'metric':delay,'statistic':'P99_'+state},
                      'fps_source':{'metric':fps,'statistic':'mean_'+state}}
    count=sum(value is not None for row in metrics.values() for value in (row['p99_ms'],row['frame_rate_hz']))
    if not count:
        raise ValueError('本地统计没有得到所需的有效 P99/FPS；不生成空成功报告。')
    full_missing=any(row.get('status')=='missing' for row in records)
    result={'status':'ok' if count==8 and not full_missing else 'partial','platform':'m100-ultra','metadata':metadata,
            'metrics':metrics,'statistics':records,'origin':origin}
    result['statistics_summary']={'total':len(records),'by_status':dict(Counter(row.get('status','available') for row in records))}
    if coverage is not None:
        result['coverage'] = coverage
        if isinstance(coverage.get('field_presence'), dict):
            result['field_presence'] = copy.deepcopy(coverage['field_presence'])
    if analysis_scope is not None:
        result['analysis_scope'] = analysis_scope
    def display(value):
        return '—' if value is None else escape(str(value))
    summary=''.join('<tr><th>'+escape(row['label'])+'</th><td>'+display(row['p99_ms'])+'</td><td>'+display(row['frame_rate_hz'])+'</td></tr>' for row in metrics.values())
    rows=''.join('<tr><th>'+escape(row.get('theme',''))+'</th><th>'+escape(row['metric'])+'</th><td>'+escape(row.get('status','available'))+'</td>'+''.join('<td>'+display(row['statistics'].get(k))+'</td>' for k in ('mean','max','min','P90','P99'))+'<td>'+escape(str(row.get('diagnostic') or row['statistics'].get('diagnostic',''))+' '+row.get('skip_reason',''))+'</td></tr>' for row in records)
    context=escape('VIN 尾号 '+metadata['vin'][-4:]+'；'+metadata['time_range']+'；m100-ultra')
    raw=escape(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False))
    html=''.join(('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>m100-ultra 本地负载统计</title>',
        '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{font:15px system-ui;margin:32px auto;max-width:1200px;padding:0 20px;color:#172338}table{border-collapse:collapse;width:100%;margin:20px 0}th,td{padding:10px;border:1px solid #dce3eb;text-align:center}th{background:#edf2f7}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{overflow:auto}</style></head><body>',
        '<h1>m100-ultra 本地负载统计</h1><p>',context,'</p><p>状态：',result['status'],
        '。统计沿用已验证的 AD 口径；缺失值显示 —，不填零。</p><h2>AD 指标</h2><table id="asgard-metrics"><thead><tr><th>场景</th><th>P99 (ms)</th><th>mean FPS (Hz)</th></tr></thead><tbody>',summary,
        '</tbody></table><h2>原分析器完整统计</h2><section><table id="native-statistics"><thead><tr><th>主题</th><th>变量</th><th>状态</th><th>mean</th><th>max</th><th>min</th><th>P90</th><th>P99</th><th>缺失或跳过原因</th></tr></thead><tbody>',rows,
        '</tbody></table></section><p>多场景字符串及单位含义沿用原指标定义。下方保留原统计字段，包括按状态拆分的 P99/mean；本附件不包含原始日志。</p>',
        '<h2>目标记录覆盖</h2><p>以下为提取后的目标记录范围，不能证明整个请求时段连续覆盖。</p><pre>',
        escape(json.dumps(coverage,ensure_ascii=False,indent=2)) if coverage is not None else '未收集覆盖记录',
        '</pre><details><summary>完整统计字段与状态列</summary><pre>',raw,'</pre></details></body></html>'))
    output=Path(output)
    output.write_text(html,encoding='utf-8')
    output.with_name('report-data.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return result


def install_hooks(plot,value,client,config,rules=None,*,native_sheet_capture=None):
    """Call before importing performance/common_wrapper. Return captured rows."""
    required=((plot,'FeishuTemplateReport'),(plot.FeishuTemplateReport,'summary_report'),
              (plot.FeishuTemplateReport,'_format_report_data'),(value,'update_last_valid_time_to_feishu'),
              (value,'load_config_from_feishu'),(client,'request'))
    if any(not hasattr(obj,name) for obj,name in required):
        raise ValueError('部署分析器的统计/发布边界已变化，需要适配。')
    config.CONFIG_MAPS['m100-ultra']['summary']['not_plot']=True
    captured=[]
    lock=threading.Lock()
    original_summary=plot.FeishuTemplateReport.summary_report
    if native_sheet_capture is not None:
        if not hasattr(plot.FeishuTemplateReport,'generate_report'):
            raise ValueError('原生 generate_report 数据边界缺失。')
        def forbidden_graph(*args,**kwargs):
            raise RuntimeError('无图原生明细模式禁止调用实际绘图入口。')
        if hasattr(plot,'Plot_Singal'):
            for name in ('plot','plot_two_columns'):
                if hasattr(plot.Plot_Singal,name):
                    setattr(plot.Plot_Singal,name,forbidden_graph)
        class LocalCaptureClient:
            def get_tenant_access_token(self):
                return None
            def __getattr__(self,name):
                raise RuntimeError('原生数据捕获禁止远程客户端操作：'+name)
        def capture_generate_report(self,sheet_token,sheet_id,data,title,read_range='!B:O'):
            if not native_sheet_capture.get('_active') or 'rd' in native_sheet_capture:
                raise RuntimeError('原生明细生成边界重复调用或脱离受检汇总。')
            if not isinstance(data,dict) or data.get('title')!=title:
                raise ValueError('原生 generate_report 入参结构变化。')
            native_sheet_capture.update(rd=copy.deepcopy(data),
                native_template_reference={'token':sheet_token,'sheet_id':sheet_id})
            return None
    def capture(self,theme_data,title):
        if native_sheet_capture is not None:
            for contents in theme_data.values():
                for fields in (contents or {}).values():
                    if (isinstance(fields,dict) and (fields.get('img_path') is not None or
                            str(fields.get('diagnostic','')).startswith('图片异常'))):
                        raise RuntimeError('无图原生统计出现图像产物或绘图异常，拒绝更改统计结果。')
        rows=capture_full_statistics(self,theme_data,rules)
        with lock:
            if captured:
                raise ValueError('原分析器重复调用全量汇总，拒绝混合两批结果。')
            captured.extend(rows)
        if native_sheet_capture is not None:
            previous_client=getattr(self,'client',None)
            previous_token=getattr(self,'access_token',None)
            self.client=LocalCaptureClient()
            native_sheet_capture['_active']=True
            try:
                # The original method performs its own native formatter, EMB
                # selection, title and version logic. Only its final network
                # boundary is intercepted, before copy/write/image operations.
                original_summary(self,theme_data,title)
            finally:
                native_sheet_capture['_active']=False
                self.client=previous_client
                self.access_token=previous_token
            if 'rd' not in native_sheet_capture:
                raise ValueError('原生 summary_report 未到达 generate_report 数据边界。')
        return None  # Native caller will not append a report URL to its summary.
    plot.FeishuTemplateReport.summary_report=capture
    def forbidden_publish(*args,**kwargs):
        raise RuntimeError('本地完整附件模式禁止原生远程发布或模板写入。')
    for name in ('theme_report','generate_report','generate_template'):
        if hasattr(plot.FeishuTemplateReport,name):
            setattr(plot.FeishuTemplateReport,name,
                capture_generate_report if native_sheet_capture is not None and name=='generate_report' else forbidden_publish)
    value.update_last_valid_time_to_feishu=lambda *a,**kw:None
    if rules is not None:
        rules=validate_full_rules(rules)
        value.load_config_from_feishu=lambda *a,**kw:copy.deepcopy(rules)
    original=client.request
    def read_only_request(method,url,*args,**kwargs):
        path=urlsplit(url).path
        auth=method.upper()=='POST' and path in ('/open-apis/auth/v3/tenant_access_token/internal','/open-apis/auth/v3/tenant_access_token')
        if rules is not None or method.upper()!='GET' and not auth:
            raise RuntimeError('本地附件模式禁止飞书发布、上传及配置写入。')
        return original(method,url,*args,**kwargs)
    client.request=read_only_request
    return captured


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,required=True)
    p.add_argument('--log-dir',type=Path,required=True)
    p.add_argument('--time-range',required=True)
    p.add_argument('--expected-vin',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--rules-json',type=Path)
    p.add_argument('--selection-manifest',type=Path,required=True)
    p.add_argument('--native-execution',choices=('linux','windows'),default='linux')
    args=p.parse_args()
    from analyzer_contract import validate_analyzer_contract
    contract=validate_analyzer_contract(args.source_root)
    sys.path[:0]=[str(args.source_root/'general_performance'),str(args.source_root)]
    import config
    from common import perf_plot,perf_value,feishu_client,perf_csv,perf_utils
    from native_execution_compat import install_execution_compat, finalize_execution_compat
    execution_trace=args.output.parent/'execution-compat-trace'
    execution_compat=install_execution_compat(perf_utils,perf_csv,
        trace_dir=execution_trace,request_time_range=args.time_range)
    if args.native_execution == 'windows':
        from windows_native_tools import install_windows_native
        install_windows_native(perf_utils,execution_trace)
    selection=json.loads(args.selection_manifest.read_text(encoding='utf-8'))
    inventory=selection.get('input_inventory')
    collector_receipts=[]
    if selection.get('policy') == 'source-namespace-v3':
        from source_catalog import validate_catalog, materialize_catalog
        from source_namespace import install_source_namespace_patch, validate_selection_audit
        validate_catalog(inventory)
        selection_audit=install_source_namespace_patch(perf_csv,inventory)
        from common import perf_path
        expected_root=args.log_dir.resolve()
        expected_target=expected_root/'perf'
        def collect_namespaced(root,interest_dirs,target,time_range):
            if (Path(root).resolve()!=expected_root or Path(target).resolve()!=expected_target
                    or time_range!=args.time_range or collector_receipts
                    or set(interest_dirs)!={'app','soc','OrinX','log_fsdA','log_fsdB','log_xcu_mcu','log_bzcu_eth'}):
                raise ValueError('原生收集请求与已核验的来源清单不一致。')
            receipt=materialize_catalog(inventory,expected_root,expected_target)
            collector_receipts.append(receipt)
            return True
        perf_path.extract_and_move_logs=collect_namespaced
    else:
        from native_selection import POLICY, install_source_boundary_patch, validate_selection_audit
        if (selection.get('policy')!=POLICY or not isinstance(inventory,dict)
                or inventory.get('selection_policy')!=POLICY
                or inventory.get('source_by_basename')!=selection.get('source_by_basename')):
            raise ValueError('日志来源映射与受检输入清单不一致。')
        excluded=[row['basename'] for row in inventory.get('excluded_conflicts',[])]
        selection_audit=install_source_boundary_patch(perf_csv,selection.get('source_by_basename'),excluded_basenames=excluded)
        selection_audit['input_inventory']=inventory
        selection_audit['input_inventory_sha256']=hashlib.sha256(json.dumps(
            inventory,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    validate_selection_audit(selection_audit)
    rules=(json.loads(args.rules_json.read_text(encoding='utf-8-sig')) if args.rules_json else
           perf_value.load_config_from_feishu(config.CONFIG_MAPS['m100-ultra']['summary']['value_config_url'][0],None))
    rules=validate_full_rules(rules)
    from runtime_audit import install_runtime_audit
    runtime_options=({'collector_receipt':lambda:collector_receipts[0] if len(collector_receipts)==1 else None}
                     if selection.get('policy')=='source-namespace-v3' else {})
    runtime=install_runtime_audit(perf_csv,perf_value,
        request_time_range=args.time_range,inventory=inventory,rules=rules,
        path_config=config.CONFIG_PATH,flat_log_root=args.log_dir/'perf',
        ini_path=args.source_root/'general_performance/o.ini',**runtime_options)
    scope=full_analysis_scope(rules)
    from native_windows_host import analysis_profile
    scope['profile']=analysis_profile({'execution':args.native_execution})
    args.output.with_name('analysis-scope.json').write_text(json.dumps(scope,ensure_ascii=False,indent=2),encoding='utf-8')
    native_sheet_capture={}
    captured=install_hooks(perf_plot,perf_value,feishu_client,config,rules,
        native_sheet_capture=native_sheet_capture)
    sys.argv=['start.py',str(args.log_dir),args.time_range,'m100-ultra']
    runpy.run_path(str(args.source_root/'general_performance/start.py'),run_name='__main__')
    runtime_execution=runtime.finalize()
    execution_compat_runtime=finalize_execution_compat(execution_trace,
        request_time_range=args.time_range)
    if not captured:
        raise ValueError('原分析器未调用完整统计汇总边界；拒绝空报告。')
    ini=configparser.ConfigParser()
    ini.read(args.source_root/'general_performance/o.ini',encoding='utf-8')
    if ini.get('PARAMETER','vin',fallback='').strip().upper()!=args.expected_vin.upper():
        raise ValueError('原分析器识别的 VIN 与请求不一致或缺失。')
    args.output.with_name('native-statistics.json').write_text(
        json.dumps({'metadata':{'vin':args.expected_vin,'time_range':args.time_range},
                    'statistics':captured},ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    coverage = collect_coverage(args.log_dir,args.time_range,
                                runtime_execution=runtime_execution,rules=rules)
    exported=export_result(captured,{'vin':args.expected_vin,'time_range':args.time_range},args.output,coverage,scope)
    exported['source_selection']=selection_audit
    exported['runtime_execution']=runtime_execution
    exported['analyzer_contract']=contract
    exported['execution_compat']=execution_compat
    exported['execution_compat_runtime']=execution_compat_runtime
    if args.native_execution == 'windows':
        from windows_native_tools import finalize_windows_native
        exported['windows_execution']=finalize_windows_native(execution_trace,require_statistics=True)
    from native_metric_diagnostics import attach_metric_input_diagnostics
    exported=attach_metric_input_diagnostics(exported)
    from native_sheet_data import build_native_sheet_data, renderer_bundle, canonical_sha256
    request_times=[datetime.strptime(x,'%Y%m%d%H%M%S').isoformat()
        for x in args.time_range.split('/')]
    metadata={'vin':args.expected_vin,'time_range':args.time_range,
        'request':{'vin':args.expected_vin,'start':request_times[0],'end':request_times[1]},
        'environment':None,
        'rules_sha256':hashlib.sha256(args.rules_json.read_bytes()).hexdigest() if args.rules_json else None,
        'rules_content_sha256':canonical_sha256(rules)}
    exported['native_sheet_data']=build_native_sheet_data(native_sheet_capture['rd'],
        exported['statistics'],rules,metadata,renderer_bundle(args.source_root),
        native_sheet_capture['native_template_reference'])
    args.output.with_name('report-data.json').write_text(
        json.dumps(exported,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    args.output.with_name('source-selection-audit.json').write_text(
        json.dumps(selection_audit,ensure_ascii=False,indent=2),encoding='utf-8')
    args.output.with_name('runtime-execution-audit.json').write_text(
        json.dumps(runtime_execution,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
