from .perf_log import logger

from .feishu_client import Client
from collections import OrderedDict
import csv
import os
import pandas as pd
from datetime import datetime


import re as _re

# 匹配末尾的阈值参数段: ;max_threshold=N 或 ;min_threshold=N
_THRESHOLD_RE = _re.compile(
    r';\s*(max_threshold|min_threshold)\s*=\s*([^;]+)\s*$',
    _re.IGNORECASE,
)


def parse_input_thresholds(input_str):
    """解析 input 字段，分离基础输入和阈值参数

    从末尾逐个剥离 ``;max_threshold=N`` / ``;min_threshold=N`` 段，
    剩余部分原样保留为 base_input——custom 类型 awk 表达式内部的 ';'
    及末尾的 awk 语句终止符 ';' 均不会被截断。

    示例:
      '13'                               -> ('13',   {'max_threshold': None, 'min_threshold': None})
      '13;max_threshold=1000'            -> ('13',   {'max_threshold': 1000, 'min_threshold': None})
      '13;max_threshold=1000;min_threshold=0' -> ('13', {'max_threshold': 1000, 'min_threshold': 0})
      'var = "" find_digit($11) "," find_digit($7);max_threshold=1000'
          -> ('var = "" find_digit($11) "," find_digit($7);', {'max_threshold': 1000, ...})

    Args:
        input_str (str): 原始 input 字段值

    Returns:
        tuple: (base_input, thresholds)
            base_input (str): 去除阈值参数后的原始输入（保留 awk 内部的 ';'）
            thresholds (dict): {'max_threshold': float|None, 'min_threshold': float|None}
    """
    if not input_str:
        return '', {'max_threshold': None, 'min_threshold': None}

    thresholds = {'max_threshold': None, 'min_threshold': None}
    remaining = input_str

    while True:
        m = _THRESHOLD_RE.search(remaining)
        if not m:
            break
        key = m.group(1).lower()
        try:
            thresholds[key] = float(m.group(2).strip())
        except (ValueError, TypeError):
            break
        remaining = remaining[:m.start()]

    return remaining.strip(), thresholds


def get_thresholds_for_csv(csv_path, config_date):
    """从配置中查找 CSV 文件对应变量的阈值参数

    通过 CSV 文件名中的 value_name（最后一个 '+' 之后的 stem）在配置中查找
    input 字段，解析出阈值参数。

    Args:
        csv_path (str | Path): CSV 文件路径，文件名格式为 'N+value_name.csv'
        config_date (dict): 各主题的变量统计方法配置 data[theme][vname]

    Returns:
        tuple: (max_threshold, min_threshold)，均为 float 或 None
    """
    try:
        from pathlib import Path as _Path
        p = _Path(str(csv_path))
        value_name = p.stem.split('+')[-1] if '+' in p.stem else p.stem

        for theme_data in config_date.values():
            if value_name in theme_data:
                input_str = theme_data[value_name].get('input', '')
                _, thresholds = parse_input_thresholds(input_str)
                return thresholds.get('max_threshold'), thresholds.get('min_threshold')
    except Exception:
        pass
    return None, None


def apply_value_thresholds(data, max_threshold, min_threshold, value_col=-1):
    """对 DataFrame 的数值列应用阈值过滤（就地过滤，超出范围不参与统计）

    复合值（如 ps_time 的 "5.2,1"）取逗号前的数值部分比较。
    非数值类型（ps_string 的分类字符串）无法过滤，直接返回原数据。

    Args:
        data (pd.DataFrame): 数据
        max_threshold (float | None): 最大值阈值，None 表示不限制上限
        min_threshold (float | None): 最小值阈值，None 表示不限制下限
        value_col (int | str): 值列位置或列名，默认 -1（最后一列）

    Returns:
        pd.DataFrame: 过滤后的数据
    """
    if max_threshold is None and min_threshold is None:
        return data

    try:
        # 支持整数位置和字符串列名
        if isinstance(value_col, str):
            col = data[value_col]
        else:
            col = data.iloc[:, value_col]

        # 复合值（如 ps_time 的 "5.2,1"），提取逗号前的数值部分
        if col.dtype == object:
            numeric_values = pd.to_numeric(
                col.astype(str).str.split(',').str[0], errors='coerce'
            )
        else:
            numeric_values = pd.to_numeric(col, errors='coerce')

        # 全为非数值则跳过（如 ps_string 的分类字符串）
        if numeric_values.isna().all():
            return data

        mask = pd.Series(True, index=data.index)
        if max_threshold is not None:
            mask &= numeric_values <= max_threshold
        if min_threshold is not None:
            mask &= numeric_values >= min_threshold

        return data[mask]
    except Exception as e:
        logger.warning(f'apply_value_thresholds: 阈值过滤失败: {e}')
        return data


def load_config(csv_file):
    """
    读取CSV配置文件，返回三层嵌套字典结构：
    data[theme][value_name][field]
    """
    data = OrderedDict()

    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            vname = row['value_name'].strip()
            theme = row['theme'].strip()

            if theme not in data:
                data[theme] = OrderedDict()

            data[theme][vname] = {
                'soc_type':       row['soc_type'].strip(),
                'log_tag':        row['log_tag'].strip(),
                'func':           row['func'].strip(),
                'input':          row['input'].strip(),
                'calculate_type': row['calculate_type'].strip(),
                'ps_type':        row['ps_type'].strip(),
            }

    return data


def get_theme_lists(data):
    """
    获取 data 中所有的 theme 列表。

    配置表空行会产生 theme='' 的条目，这类条目无法定位日志目录也产不出数据，
    在此直接过滤掉，避免下游为它白跑一轮统计（详见 get_theme 内的说明）。

    参数:
      data: dict, 源数据字典

    返回:
      list: 包含所有有效 theme 名称的列表, 例如 ["val_lidar", "theme2", ...]
    """
    if not data:
        return []

    theme_lists = [theme for theme in data.keys() if str(theme).strip()]
    skipped_count = len(data) - len(theme_lists)
    if skipped_count:
        logger.warning(f'配置表存在 {skipped_count} 个空 theme（疑似空行），已跳过')
    return theme_lists


def get_theme(data, theme):
    """
    判断主题是否存在:
      - 存在:   返回 (True, soc_type, values_list)
      - 不存在: 返回 (False, None, None)

    返回值说明:
      exists     : bool, 主题是否存在
      soc_type   : str 或 list, 该主题下的 soc_type；
                   单个前缀时为 str，多个前缀时为 list。
                   soc_type 字段支持逗号分隔多个前缀，如 "FSDA_M100,FSDB_M100"。
      values     : list, 按 log_tag 分组的二层列表:
                   [
                     [ log_tag, [ (func, input, value_name, calculate_type, ps_type), ... ] ],
                     ...
                   ]

    使用方式:
      exists, soc_type, values = get_theme(data, 'val_lidar')
      if exists:
          for var in values:
              var_log_tag, var_fuc_input_name = var[0], var[1]
              for var_func, var_input, var_name, calculate_type, ps_type in var_fuc_input_name:
                  logger.debug(f"theme var: {var_log_tag} {var_func} {var_name}")
    """
    if theme not in data:
        return False, None, None

    # 配置表中的空行会产生 theme='' 且各字段全空的条目。
    # 此时 soc_type 解析为空 list，extract_logs 会把 log_dir_name 取成 ''，
    # 使提取目录退化为 perf/log/ 本身并递归 glob 全部子目录（实测白扫 20GB），
    # 且空 log_tag 匹配每一行却产不出任何有效数据。视为主题不存在，直接跳过。
    # 正常路径下 get_theme_lists 已过滤掉空 theme，此处作为纵深防御。
    if not str(theme).strip():
        logger.debug('get_theme: 空 theme（疑似配置表空行），已跳过')
        return False, None, None

    values = data[theme]

    # ---- 提取该主题的 soc_type（支持单个字段内以逗号分隔多个前缀）----
    soc_types = list(OrderedDict.fromkeys(
        s.strip()
        for v in values.values()
        for s in v['soc_type'].split(',')
        if s.strip()
    ))
    if not soc_types:
        # 无任何有效 soc_type：无法定位日志目录，跳过以免退化成扫描 perf/log/ 全目录
        logger.warning(f'get_theme: 主题 {theme!r} 无有效 soc_type，已跳过')
        return False, None, None
    soc_type = soc_types[0] if len(soc_types) == 1 else soc_types

    # ---- 按 log_tag 分组，构建目标结构 ----
    grouped = OrderedDict()
    for vname, fields in values.items():
        log_tag = fields['log_tag']
        entry = (
            fields['func'],
            fields['input'],
            vname,                    # value_name 作为 var_name
            fields['calculate_type'],
            fields['ps_type'],
        )
        if log_tag not in grouped:
            grouped[log_tag] = []
        grouped[log_tag].append(entry)

    # 转换为 [ [log_tag, [(func, input, name, calc_type, ps_type), ...]], ... ]
    var_lists = [[log_tag, entries] for log_tag, entries in grouped.items()]

    return True, soc_type, var_lists


def get_ps_type_map(data, theme):
    """
    获取指定 theme 下 value_name → ps_type 的映射字典

    参数:
      data  : load_config 返回的三层嵌套字典
      theme : str, 主题名

    返回:
      dict, {value_name: ps_type, ...}
      若 theme 不存在则返回空字典 {}

    使用方式:
      ps_map = get_ps_type_map(data, 'val_lidar')
      ps_type = ps_map['some_value_name']  # 快速获取对应 ps_type
    """
    if theme not in data:
        return {}

    return {vname: info['ps_type'] for vname, info in data[theme].items()}


def get_calculate_type_map(data, theme):
    """获取指定 theme 下 value_name → calculate_type 的映射"""
    if theme not in data:
        return {}
    return {vname: info['calculate_type'] for vname, info in data[theme].items()}


def get_emb_type_value_name(data):
    """在所有 theme 中查找 value_name 为 'emb_type' 的变量，未找到返回 None"""
    for values in data.values():
        if 'emb_type' in values:
            return 'emb_type'
    return None


def load_config_from_feishu(spreadsheet_token, sheet_name=None, keep_temp_file=False, temp_file_name='value_config.csv'):
    """
    直接从飞书表格读取配置，返回与 load_config 相同格式的数据

    参数:
      spreadsheet_token: str, 飞书表格的 token
      sheet_name       : str, 可选，指定要读取的 sheet 名称。默认读取第一个 sheet
      keep_temp_file   : bool, 是否保留临时文件。默认 False，读取完成后删除
      temp_file_name   : str, 临时文件名称。默认 'value_config.csv'，保存在当前运行文件夹

    返回:
      data: 三层嵌套字典结构 data[theme][value_name][field]

    字段说明:
      每行数据应包含: value_name, theme, soc_type, log_tag, func, input, calculate_type, ps_type

    使用方式:
      data = load_config_from_feishu(spreadsheet_token='abcdef123456', sheet_name='配置表')
      exists, soc_type, values = get_theme(data, 'val_lidar')
    """
    client = Client()
    access_token = client.get_tenant_access_token()

    # 获取表格信息，确定 sheet_id
    sheets_info = client.get_sheets_info(access_token, spreadsheet_token)

    if sheet_name:
        # 查找指定名称的 sheet
        sheet_id = None
        for sheet in sheets_info:
            if sheet.get('title') == sheet_name:
                sheet_id = sheet.get('sheetId')
                break
        if sheet_id is None:
            raise ValueError(f"Sheet '{sheet_name}' not found in spreadsheet")
    else:
        # 使用第一个 sheet
        sheet_id = sheets_info[0].get('sheetId')

    # 使用 get_values_batch 读取数据，参考 perf_plot.py 中的用法
    # 只读取前8列有效数据（A:H）避免读取空白列
    range_str = sheet_id + "!A1:H10000"
    result = client.get_values_batch(
        access_token, spreadsheet_token, range_str, value_render="UnformattedValue")

    # 解析返回的数据
    values = []
    if result and 'valueRanges' in result and result['valueRanges']:
        values = result['valueRanges'][0].get('values', [])

    if not values:
        raise Exception("No data found in spreadsheet")

    # 保存到当前运行文件夹
    temp_path = os.path.join(os.getcwd(), temp_file_name)

    # 写入文件
    with open(temp_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(values)

    # 复用 load_config 读取数据
    data = load_config(temp_path)

    # 根据 keep_temp_file 参数决定是否删除临时文件
    if not keep_temp_file:
        try:
            os.remove(temp_path)
        except OSError as e:
            logger.warning(f'load_config_from_feishu: 删除临时文件失败 {temp_path}: {e}')

    return data


# ==================== 变量最新统计时间回写 ====================
LAST_VALID_TIME_HEADER = 'last valid time'


def _parse_date_from_time_range(time_range):
    """从本次运行的时间范围解析出统计日期（YYYY-MM-DD）

    time_range 可能为：
      - None: 未指定时间范围（理论不会出现，兜底用当天日期）
      - (start_ts, end_ts): 单个时间段，取 start_ts 对应日期
      - [(start_ts, end_ts), ...]: 多个时间段，取所有 start_ts 中最大日期

    Args:
        time_range: None / tuple / list[tuple]

    Returns:
        str: YYYY-MM-DD 格式的统计日期；解析失败时兜底返回当天日期
    """
    try:
        if not time_range:
            return datetime.now().strftime("%Y-%m-%d")

        ranges = time_range if isinstance(time_range, list) else [time_range]
        max_ts = None
        for r in ranges:
            if not r or r[0] is None:
                continue
            ts = r[0]
            if max_ts is None or ts > max_ts:
                max_ts = ts
        if max_ts is None:
            return datetime.now().strftime("%Y-%m-%d")
        return datetime.fromtimestamp(int(max_ts)).strftime("%Y-%m-%d")
    except Exception as e:
        logger.exception(f'_parse_date_from_time_range: 解析 {time_range!r} 失败: {e}')
        return datetime.now().strftime("%Y-%m-%d")


def _is_date_newer(new_date, old_date):
    """判断 new_date 是否比 old_date 更新（字符串 YYYY-MM-DD 字典序即日期序）

    old_date 为空/None 时，new_date 视为更新。
    """
    if not old_date or not str(old_date).strip():
        return True
    return str(new_date).strip() > str(old_date).strip()


def update_last_valid_time_to_feishu(spreadsheet_token, active_value_names, stat_date):
    """把本次"有效统计"的变量最新统计时间回写到飞书配置表

    在配置表新增/更新一列「last valid time」：
      - 表头若不存在该列，首次运行自动在 I 列写入表头
      - 仅更新 active_value_names 中包含的 value_name 对应行
      - 已有时间 < stat_date 才更新，否则保留（保留最新时间）
      - 找不到 value_name 列时直接返回，不进行任何写入

    全程异常捕获，失败仅记录日志，不抛出、不退出进程。

    Args:
        spreadsheet_token (str): 飞书配置表的 token
        active_value_names (set | list): 本次"有效统计"的 value_name 集合
        stat_date (str): 本次统计日期 YYYY-MM-DD
    """
    try:
        if not active_value_names:
            logger.warning('update_last_valid_time_to_feishu: active_value_names 为空，跳过回写')
            return
        active_set = set(active_value_names)
        if not stat_date:
            logger.warning('update_last_valid_time_to_feishu: stat_date 为空，跳过回写')
            return

        client = Client()
        access_token = client.get_tenant_access_token()
        sheets_info = client.get_sheets_info(access_token, spreadsheet_token)
        sheet_id = sheets_info[0].get('sheetId')

        # 读取表头与全部配置行（A:I 列）
        range_str = f"{sheet_id}!A1:I10000"
        result = client.get_values_batch(
            access_token, spreadsheet_token, range_str, value_render="UnformattedValue")
        rows = []
        if result and 'valueRanges' in result and result['valueRanges']:
            rows = result['valueRanges'][0].get('values', []) or []
        if not rows:
            logger.warning('update_last_valid_time_to_feishu: 配置表为空，跳过回写')
            return

        header = rows[0]
        # 找 value_name 列
        value_name_col = None
        for idx, h in enumerate(header):
            if str(h).strip().lower() == 'value_name':
                value_name_col = idx
                break
        if value_name_col is None:
            logger.warning('update_last_valid_time_to_feishu: 配置表未找到 value_name 列，跳过回写')
            return

        # 找 last valid time 列；不存在则追加到 I 列（第 9 列，索引 8）
        lvt_col = None
        for idx, h in enumerate(header):
            if str(h).strip().lower() == LAST_VALID_TIME_HEADER:
                lvt_col = idx
                break
        need_write_header = False
        if lvt_col is None:
            lvt_col = 8  # I 列
            need_write_header = True
            # 先写入表头
            try:
                header_cell = chr(ord('A') + lvt_col) + '1'
                client.update_values(
                    access_token, spreadsheet_token,
                    f"{sheet_id}!{header_cell}:{header_cell}",
                    [[LAST_VALID_TIME_HEADER]])
                logger.warning(f'update_last_valid_time_to_feishu: 首次运行，已在 {header_cell} 写入表头 "{LAST_VALID_TIME_HEADER}"')
            except Exception as e:
                logger.exception(f'update_last_valid_time_to_feishu: 写入表头失败: {e}')
                return

        lvt_letter = chr(ord('A') + lvt_col)

        # 构造 {value_name: [行号(1-based), 旧时间]}；行号对应 rows 索引（rows[0] 是表头）
        updates = []  # 元素: (行号1-based, 新时间)
        matched = 0
        for i in range(1, len(rows)):
            row = rows[i]
            # 行数据可能比表头短，做长度保护
            if value_name_col >= len(row):
                continue
            vname = str(row[value_name_col]).strip()
            if not vname or vname not in active_set:
                continue
            old_val = row[lvt_col] if lvt_col < len(row) else ''
            if _is_date_newer(stat_date, old_val):
                updates.append((i + 1, stat_date))  # 行号: rows 索引 i → 飞书行号 i+1
            matched += 1

        if not updates:
            logger.warning(
                f'update_last_valid_time_to_feishu: 共匹配 {matched} 个变量，'
                f'无需更新（已均为最新或无 active 变量），stat_date={stat_date}')
            return

        # 批量写入：按行号排序，构造连续区段尽量合并；这里直接逐行写入过于啰嗦，
        # 飞书 update_values 支持一次性写入矩形区域。为避免错位，采用"逐行单格"方式更稳妥。
        # 但为减少请求数，按连续行号分组写为多段矩形。
        updates.sort(key=lambda x: x[0])
        # 拆成连续行段
        segments = []
        seg_start = updates[0][0]
        seg_vals = [updates[0][1]]
        for row_num, val in updates[1:]:
            if row_num == seg_start + len(seg_vals):
                seg_vals.append(val)
            else:
                segments.append((seg_start, seg_vals))
                seg_start = row_num
                seg_vals = [val]
        segments.append((seg_start, seg_vals))

        for seg_start, seg_vals in segments:
            seg_end = seg_start + len(seg_vals) - 1
            crange = f"{sheet_id}!{lvt_letter}{seg_start}:{lvt_letter}{seg_end}"
            # update_values 期望 values 为二维列表
            values_2d = [[v] for v in seg_vals]
            client.update_values(access_token, spreadsheet_token, crange, values_2d)

        logger.warning(
            f'update_last_valid_time_to_feishu: 回写完成，共更新 {len(updates)} 个变量，'
            f'stat_date={stat_date}，分 {len(segments)} 段写入')
    except Exception as e:
        logger.exception(f'update_last_valid_time_to_feishu: 回写失败: {e}')
