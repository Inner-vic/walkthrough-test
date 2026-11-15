"""Synthetic-only native Sheet payload fixture; no external files or services."""
from __future__ import annotations
import copy
from datetime import datetime
from native_sheet_data import build_native_sheet_data, canonical_sha256

_SYNTHETIC_RENDERER = {'profile': 'approved-original-native-template-methods-v1', 'source_contract_fingerprint': '1912ef7912c3b9285901b22daa742a359cfbc8e8941f26c1ed89d9341aa80ebf', 'source_files_sha256': {}, 'methods': {'update_values': {'source': 'def update_values(self, rd, keymap, nsh_id):\n    """根据模板单元格占位符填充数据并拆分文本/图片更新范围\n\n        Args:\n            rd (dict): _format_report_data 输出的变量统计字典\n            keymap (dict): 飞书 get_values_batch 返回的模板单元格数据\n            nsh_id (str): 目标 sheet id\n\n        Returns:\n            tuple: (range_text, range_image)\n                range_text: {range_str: 填充后的二维列表}\n                range_image: [(img_range, img_stream, img_name), ...]\n        """\n\n    def _out(data):\n\n        def _inner2(keys):\n            if not keys:\n                return \'\'\n            key, *next_keys = keys\n            if key and \'=\' == key[0]:\n                return key[1:] + _inner2(next_keys)\n            else:\n                t = \'-\'\n                for _i, _k in enumerate(key.split(self.split_char)):\n                    if _i == 0:\n                        if _k not in data:\n                            break\n                        t = data[_k]\n                    else:\n                        if _k not in t:\n                            break\n                        t = t[_k]\n                return str(t) + _inner2(next_keys)\n        return _inner2\n    mapdata = _out(rd)\n    rg_text, rg_img = ({}, [])\n    for cell_value in keymap[\'valueRanges\']:\n        cl_img = []\n        sh_id, (x1, x2), (x3, x4) = parse_sheet_coordinate(cell_value[\'range\'])\n        for _r, row in enumerate(cell_value[\'values\']):\n            for _c, j in enumerate(row):\n                if isinstance(j, str) and j[0] == self.key_char:\n                    placeholder_keys = j.split(self.key_char)[1:]\n                    nv = mapdata(placeholder_keys)\n                    if self.split_char + \'image\' in j:\n                        value_name = placeholder_keys[0].split(self.split_char)[0]\n                        diagnostic = rd.get(value_name, {}).get(\'diagnostic\')\n                        if diagnostic:\n                            row[_c] = diagnostic\n                            continue\n                        row[_c] = None\n                        if not nv or nv in (\'-\', \'None\'):\n                            continue\n                        cor = (nsh_id, (x1 + _c, x2 + _r), (x1 + _c, x2 + _r))\n                        img_rg = parse_sheet_coordinate(cor)\n                        cl_img.append((img_rg, img_to_stream(nv), Path(nv).name))\n                    else:\n                        row[_c] = nv if nv else \'-\'\n                if str(j).startswith(\'=\'):\n                    row[_c] = {\'type\': \'formula\', \'text\': j}\n        nrg = nsh_id + \'!\' + cell_value[\'range\'].split(\'!\')[1]\n        rg_text.update({nrg: cell_value[\'values\']})\n        rg_img.extend(cl_img)\n    return (rg_text, rg_img)', 'ast_sha256': 'c6378c3e8706151fe687eaea3fa111d4a7bf24936300f5dcca98f2e632cacd53'}, 'parse_sheet_coordinate': {'source': 'def parse_sheet_coordinate(sheet_coordinate):\n    """坐标字符串 "sheetId!A1:B2" ↔ 元组 (sheetId, (col, row), (col, row)) 互转\n\n    Args:\n        sheet_coordinate (str | tuple): 字符串坐标，或 (sheetId, (col, row), (col, row)) 元组\n\n    Returns:\n        tuple | str: 输入为 str 返回元组；输入为元组返回字符串\n    """\n    if isinstance(sheet_coordinate, str):\n        sheet_id, coordinate = sheet_coordinate.split(\'!\')\n        st, ed = coordinate.split(\':\')\n\n        def _o(x, _t=\'s\'):\n            fx = {\'s\': str.isalpha, \'i\': str.isdigit}\n            lt = \'\'.join(filter(fx[_t], x))\n            return ord(lt) - 65 if _t == \'s\' else int(lt)\n        return (sheet_id, (_o(st), _o(st, \'i\')), (_o(ed), _o(ed, \'i\')))\n    a, (b, c), (d, e) = sheet_coordinate\n    return a + \'!\' + chr(b + 65) + str(c) + \':\' + chr(d + 65) + str(e)', 'ast_sha256': 'b49cfac9a8a19d2de7412192be76d60d7a0750ce7c25706e7681326b24eba381'}}, 'synthetic_fixture': True}


def synthetic_renderer_bundle():
    return copy.deepcopy(_SYNTHETIC_RENDERER)


def synthetic_native_sheet_payload(report, rules, metadata, file_rules_sha256=None):
    """Make a synthetic payload from a complete synthetic native report."""
    metadata = copy.deepcopy(metadata)
    request = metadata.get('request', metadata)
    vin = request['vin']
    if 'start' in request and 'end' in request:
        time_range = '/'.join(datetime.fromisoformat(request[k]).strftime('%Y%m%d%H%M%S') for k in ('start','end'))
        request = {k: request[k] for k in ('vin','start','end')}
    else:
        time_range = metadata['time_range']
        start,end = [datetime.strptime(x,'%Y%m%d%H%M%S').isoformat() for x in time_range.split('/')]
        request = {'vin':vin,'start':start,'end':end}
    rows = copy.deepcopy(report['statistics'])
    rd = {'title':'Synthetic native performance','version':'synthetic'}
    for row in rows:
        row.setdefault('native_publish', row.get('status') != 'conditional_skip')
        row.setdefault('rule', copy.deepcopy(rules.get(row['theme'],{}).get(row['metric'])))
        if row['native_publish'] and row.get('statistics'):
            rd[row['metric']] = {**row['statistics'],'image':None}
    context = {'vin':vin,'time_range':time_range,'request':request,
        'environment':metadata.get('environment'),
        'rules_sha256':file_rules_sha256,'rules_content_sha256':canonical_sha256(rules)}
    return build_native_sheet_data(rd,rows,rules,context,synthetic_renderer_bundle(),
        {'token':'synthetic-template-token','sheet_id':'synthetic-template-sheet'})
