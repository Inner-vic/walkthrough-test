"""Experimental ZIP member streaming and in-memory native-equivalent AD statistics.

No source extraction, subprocess, WSL, CSV, eval or arbitrary rule execution.
Only the reviewed four direct custom/ps_time rules are accepted.
"""
from collections import Counter
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
from pathlib import Path,PurePosixPath
import re
import time
import zipfile
import numpy as np
import pandas as pd

TZ=timezone(timedelta(hours=8))
STATES=('0','1','2','3','4','5','6','9')
PROFILE='m100-memory-four-v1'
STAMP=re.compile(r'(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})')
DIGIT=re.compile(r'-?[0-9]+(.[0-9]+)?') # Preserve original find_digit semantics.
THRESHOLD=re.compile(r';\s*(max_threshold|min_threshold)\s*=\s*([^;]+)\s*$',re.I)
TERM=r'(find_digit\(\$(\d+)\)|\$(\d+))'
EXPRESSION=re.compile(r'var\s*=\s*""\s*'+TERM+r'\s*","\s*'+TERM+r'\s*;?')
FIELD_NUMBER=re.compile(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
EMPTY_FIELD_TOKENS=frozenset(('-', 'n/a', 'nan', 'null', 'none'))

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def digest(p):
    h=hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

def compile_rules(rules):
    from m100_attachment_worker import select_required_rules
    selected=select_required_rules(rules);compiled={}
    for theme,metrics in selected.items():
        if theme.startswith('Tc397'):raise ValueError('Unsupported date order')
        for name,fields in metrics.items():
            expression=fields['input'];thresholds={}
            while match:=THRESHOLD.search(expression):
                value=float(match[2])
                if not np.isfinite(value):raise ValueError('Non-finite threshold')
                thresholds[match[1].lower()]=value;expression=expression[:match.start()]
            match=EXPRESSION.fullmatch(expression.strip())
            if not match:raise ValueError('Unsupported custom expression; native adapter required')
            # Only literal tags in this profile; never silently translate arbitrary AWK regex.
            if not re.fullmatch(r'[A-Za-z0-9_ #:\-]+',fields['log_tag']):raise ValueError('Unsupported log-tag grammar')
            prefixes=fields['soc_type'].split(',')
            if any(not re.fullmatch(r'[A-Za-z0-9_-]+_container',p.strip()) for p in prefixes):
                raise ValueError('Container alias requires explicit mapping review')
            terms=[]
            for pos in (1,4):
                column=int(match[pos+1] or match[pos+2])
                if not 1<=column<=1000:raise ValueError('Invalid rule column')
                terms.append((column-1,match[pos].startswith('find_digit')))
            compiled[name]={'theme':theme,'tag':fields['log_tag'].encode(),
                'prefixes':[p.strip() for p in prefixes],'terms':terms,'thresholds':thresholds}
    return selected,compiled

def file_stamp(name,last=False):
    matches=STAMP.findall(name)
    return datetime.strptime(matches[-1] if last else matches[0],'%Y_%m_%d_%H_%M_%S').replace(tzinfo=TZ) if matches else None

def index_members(root,metadata,prefixes=None,environment='testtwo'):
    manifest=read(root/'download-manifest.json')
    if environment not in ('testtwo','prod') or manifest['request']!=metadata or manifest['environment']!=environment:raise ValueError('Wrong source identity')
    start=datetime.fromisoformat(metadata['start']).replace(tzinfo=TZ)
    end=datetime.fromisoformat(metadata['end']).replace(tzinfo=TZ)
    members={};vins=set();archives=0
    for item in manifest['files']:
        path=(root/item['relative_path']).resolve()
        if not path.is_relative_to(root.resolve()) or path.is_symlink() or digest(path)!=item['sha256']:raise ValueError('Source hash/path mismatch')
        if path.suffix!='.zip':raise ValueError('Experimental direct parser supports ZIP archives only')
        stamp=file_stamp(path.name)
        # Native preprocess ignores archives without its timestamp pattern.
        if stamp is None:continue
        if not start<=stamp<=end+timedelta(hours=1):continue
        archives+=1
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                parts=PurePosixPath(info.filename).parts
                if info.is_dir():continue
                if '..' in parts or info.flag_bits&1:raise ValueError('Unsupported archive member')
                name=PurePosixPath(info.filename).name
                if not name.endswith('.log'):continue
                if name.startswith(('FSDA_M100','FSDB_M100','Thor_soc','FSDA_OrinX','FSDB_OrinX')):
                    match=re.search(r'(?<![A-Z0-9])([A-Z0-9]{17})(?![A-Z0-9])',name)
                    if match:vins.add(match[1])
                if prefixes is not None and not any(name.startswith(p) for p in prefixes):continue
                candidate={'archive':str(path),'member':info.filename,'crc':info.CRC,'bytes':info.file_size,'name':name}
                if name in members and (members[name]['crc'],members[name]['bytes'])!=(info.CRC,info.file_size):
                    members[name].setdefault('conflicts',[]).append(candidate)
                    continue
                if name in members:continue
                members[name]=candidate
    if vins!={metadata['vin']}:raise ValueError('Native SOC filename VIN absent or ambiguous')
    return members,archives,start,end

def choose_members(members,prefixes,start,end):
    selected={}
    for prefix in prefixes:
        files=[m for name,m in members.items() if name.startswith(prefix)]
        files.sort(key=lambda m:(file_stamp(m['name'],True) is not None,file_stamp(m['name'],True)))
        after=False
        for member in files:
            stamp=file_stamp(member['name'],True)
            if stamp and stamp<start:continue
            if stamp and stamp>end:
                if after:continue
                after=True
            selected[member['name']]=member
    result=[selected[name] for name in sorted(selected)]
    if any(m.get('conflicts') for m in result):
        raise ValueError('Selected log filename has conflicting contents; native winner requires review')
    return result

def extract_rows(root,metadata,rules,deadline=None,environment='testtwo'):
    selected,compiled=compile_rules(rules)
    members,archives,start,end=index_members(Path(root),metadata,{p for r in compiled.values() for p in r['prefixes']},environment)
    if start.year!=end.year:raise ValueError('Cross-year native date inference needs separate validation')
    by_theme={}
    for name,rule in compiled.items():by_theme.setdefault(rule['theme'],[]).append(name)
    rows={name:[] for name in compiled};sources=[];bytes_scanned=0
    for theme,names in by_theme.items():
        prefixes=list(dict.fromkeys(p for name in names for p in compiled[name]['prefixes']))
        chosen=choose_members(members,prefixes,start,end)
        for member in chosen:
            sources.append({**member,'theme':theme})
            with zipfile.ZipFile(member['archive']) as archive,archive.open(member['member']) as stream:
                for line_index,line in enumerate(io.BufferedReader(stream,1024*1024)):
                    if line_index%16384==0 and deadline is not None and time.monotonic()>deadline:
                        raise TimeoutError('Direct AD analysis exceeded configured timeout')
                    bytes_scanned+=len(line)
                    matching=[name for name in names if compiled[name]['tag'] in line]
                    if not matching:continue
                    fields=line.decode('utf-8',errors='strict').split()
                    # Native mawk prints MM-DD HH:MM:SS.frac pid epoch value,state.
                    date=datetime.strptime(str(start.year)+'-'+fields[0]+' '+fields[1].split('.')[0],'%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
                    stamp_text=str(int(date.timestamp()))+'.'+(fields[1].split('.',1)[1] if '.' in fields[1] else '')
                    for name in matching:
                        values=[]
                        for column,find in compiled[name]['terms']:
                            value=fields[column] if column<len(fields) else ''
                            if find:
                                found=DIGIT.search(value);value=found[0] if found else ''
                            values.append(value)
                        raw=' '.join((fields[0],fields[1],fields[2],stamp_text,','.join(values)))
                        rows[name].append((fields[0],fields[1],values[1],float(stamp_text),values[0],raw))
                        if len(rows[name])>1000000:raise ValueError('Target sample count exceeds memory trial limit')
    return rows,compiled,{'archives_considered':archives,'target_members':len(sources),
        'target_bytes_scanned':bytes_scanned,'members':sources},start.timestamp(),end.timestamp()

def statistics(rows,thresholds,start,end,source_present=True):
    """Port of native ps_time branch, same pandas/NumPy operators and order."""
    def missing(reason,raw_count=None,range_count=None,data_range=None):
        result={k:'-' for k in ('mean','max','min','P90','P99','HGR')}
        counts=[]
        if raw_count is not None:counts.append(f'原始 {raw_count} 条')
        if range_count is not None:counts.append(f'时间范围内 {range_count} 条')
        lines=['无数据：'+reason]
        if counts:lines.append('，'.join(counts))
        if data_range:lines.append('数据范围：'+data_range)
        result['diagnostic']='\n'.join(lines)
        return result
    if not rows:return missing('日志标签未命中',0) if source_present else missing('未找到源日志文件')
    data=pd.DataFrame([r[:5] for r in rows],columns=['date','time','pid','timestamp','value'])
    # Native head_tail_index removes [change-1,change,change+1,change+2], including final row boundary.
    changes=data.pid.fillna('!');idx=changes.loc[changes!=changes.shift(-1)].index
    excluded=set(i+offset for i in idx for offset in (-1,0,1,2))
    data=data.loc[~data.index.isin(excluded)].copy()
    data['value']=pd.to_numeric(data.value,errors='coerce')
    times=data.timestamp.dropna()
    time_text=(datetime.fromtimestamp(times.min(),TZ).strftime('%Y-%m-%d %H:%M:%S')+' 至 '+
               datetime.fromtimestamp(times.max(),TZ).strftime('%Y-%m-%d %H:%M:%S')) if len(times) else None
    in_range=data[(data.timestamp>=start)&(data.timestamp<=end)]
    if in_range.empty:return missing('统计时间范围内无数据',len(rows),0,time_text)
    if not in_range.value.notna().any():return missing('解析后无有效数值',len(rows),len(in_range),time_text)
    if 'max_threshold' in thresholds:data=data[data.value<=thresholds['max_threshold']]
    if 'min_threshold' in thresholds:data=data[data.value>=thresholds['min_threshold']]
    data=data[(data.timestamp>=start)&(data.timestamp<=end)]
    if data.empty:return missing('阈值过滤后无数据' if thresholds else '解析后无有效数值',len(rows),len(in_range),time_text)
    data=data.dropna()
    if data.empty:return missing('解析后无有效数值',len(rows),len(in_range),time_text)
    data=data.sort_values('timestamp').reset_index(drop=True)
    if not data.empty:
        values=data.value.abs();mu=values.loc[values<=values.quantile(.95)].mean()
        if mu==0:mu=.1
        mask=(np.abs(values-mu)>100*mu)|(values<0)
        data=data.loc[values[~mask].index]
    if data.empty:return missing('异常值过滤后无数据',len(rows),len(in_range),time_text)
    result={key:[] for key in ('mean','max','min','P90','P99','HGR')}
    for state in STATES:
        values=pd.to_numeric(data.loc[data.pid==state].value,errors='ignore').dropna()
        desc=values.describe(percentiles=[.90,.99])
        calculated={key:round(desc[field],2) for key,field in (('mean','mean'),('max','max'),('min','min'),('P90','90%'),('P99','99%'))}
        calculated['HGR']=np.nan
        for key,value in calculated.items():
            rendered='-' if pd.isna(value) else str(value)
            result[key].append(rendered);result[key+'_'+state]=rendered
    for key in ('mean','max','min','P90','P99','HGR'):result[key]='/'.join(result[key])
    return result

def analyze(root,metadata,rules,deadline=None,environment='testtwo'):
    rows,compiled,sources,start,end=extract_rows(root,metadata,rules,deadline,environment)
    records=[];coverage={};samples={};field_presence={}
    for name,data in rows.items():
        present=any(m['theme']==compiled[name]['theme'] for m in sources['members'])
        records.append({'metric':name,'statistics':statistics(data,compiled[name]['thresholds'],start,end,present)})
        inside=[r for r in data if start<=r[3]<=end]
        numeric_observations=empty_observations=0
        for row in inside:
            # A tag hit alone also produces rows when a rule column is absent
            # or find_digit fails. Neither is evidence of a parsed field.
            value=str(row[4]).strip()
            if not str(row[2]).strip() or not value:continue
            if value.casefold() in EMPTY_FIELD_TOKENS:
                empty_observations+=1
            elif FIELD_NUMBER.fullmatch(value) and np.isfinite(float(value)):
                numeric_observations+=1
        observations=numeric_observations+empty_observations
        field_presence[name.casefold()]={'status':'parsed' if observations else 'unparsed',
            'observations':observations,'numeric_observations':numeric_observations,
            'empty_observations':empty_observations,'scope':'requested_window',
            'evidence':'extracted_target_value_and_state'}
        stamps=[r[3] for r in inside]
        coverage[name.casefold()]={'rows':len(inside),'state_counts':dict(Counter(r[2] for r in inside)),
            'first':datetime.fromtimestamp(min(stamps),TZ).isoformat(timespec='milliseconds') if stamps else None,
            'last':datetime.fromtimestamp(max(stamps),TZ).isoformat(timespec='milliseconds') if stamps else None,'unrecognized_rows':0}
        samples[name.casefold()]={'rows':len(data),'sorted_rows_sha256':hashlib.sha256('\n'.join(sorted(r[5] for r in data)).encode()).hexdigest()}
    return {'records':records,'field_presence':field_presence,'coverage':{'method':'target_records_before_statistical_cleaning',
        'continuous_coverage_verified':False,'targets':coverage},'samples':samples,'sources':sources,'profile':PROFILE}
