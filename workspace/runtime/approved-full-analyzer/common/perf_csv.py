import os
import shutil
import threading
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from pathlib import Path
from os.path import join
import numpy as np
import pandas as pd
from .perf_value import *
from .perf_log import logger
from .perf_utils import (
    year_from_range, get_dynamic_max_processes,
    make_dirs, rm_duplicate_csv, txt_process_tool,
    should_keep_log, _parse_cn_dt_from_log_path,
    get_multi_log_diff_value, get_multi_log_diff_key_value,
    _value_name_from_path,
    collect_log_tags, prefilter_logs_by_tags,
)

# 多线程数量设置
multi_log_workers = 16

# 防止重复解压
log_type_list = []
soc_locks_dict = {}
soc_locks_dict_lock = threading.Lock()  # 保护 soc_locks_dict 的并发读写

# grep 预过滤结果缓存：{log_dir_name: [过滤文件路径] | None}
# 同一日志目录被多个 theme 共用（如 FSDA_Thor 下 19 个 theme），只过滤一次供全部复用；
# 值为 None 表示该目录不适用预过滤，各 theme 退回全量扫描。
prefilter_cache = {}
prefilter_locks_dict = {}
prefilter_cache_lock = threading.Lock()  # 保护 prefilter_cache / prefilter_locks_dict

# CSV 列名约定：date time pid ts value[,ps]
_CSV_NAMES = ['date', 'time', 'pid', 'ts', 'value,ps']

# calculate_type == 'frame_rate_window' 的帧率计算参数：
# 每 FRAME_RATE_WINDOW_SIZE 个间隔为一组（不重叠），跨度 = 前一组结束时间→本组结束时间（秒）；
# 帧率 = 组内 ≤ FRAME_RATE_WINDOW_INTERVAL_MAX_MS(ms) 的有效间隔数 / 跨度秒（大间隔仍占用时间但不计数）；
# 组跨度 > FRAME_RATE_WINDOW_MAX_SPAN_SECONDS(秒) 则整组丢弃。
FRAME_RATE_WINDOW_SIZE = 5
FRAME_RATE_WINDOW_INTERVAL_MAX_MS = 120
FRAME_RATE_WINDOW_MAX_SPAN_SECONDS = 1.0


def _split_value_ps(data):
    """把 'value,ps' 列拆为 value/ps 两列，兼容无逗号或多列情况

    Args:
        data (pd.DataFrame): 含 'value,ps' 列的 DataFrame

    Returns:
        pd.DataFrame: 新增 'value' 与 'ps' 两列的 DataFrame；无 ps 时 ps 列为空串
    """
    split_cols = data['value,ps'].fillna('').astype(str).str.split(',', n=1, expand=True)
    if split_cols.shape[1] == 1:
        split_cols[1] = ''
    elif split_cols.shape[1] > 2:
        split_cols = split_cols.iloc[:, :2]
    split_cols.columns = ['value', 'ps']
    data[['value', 'ps']] = split_cols
    return data


def _join_value_ps(data):
    """按是否有 ps 拼回 'value,ps' 列

    Args:
        data (pd.DataFrame): 含 'value' 与 'ps' 列的 DataFrame

    Returns:
        pd.DataFrame: 新增/覆盖 'value,ps' 列的 DataFrame，格式 'value' 或 'value,ps'
    """
    def fmt(row):
        ps = str(row['ps']).strip()
        if pd.notna(row['ps']) and ps != '':
            return f"{row['value']},{ps}"
        return str(row['value'])
    data['value,ps'] = data.apply(fmt, axis=1)
    return data


def _read_csv_split(csv_file):
    """读取 CSV 并拆分 value/ps

    Args:
        csv_file (str): CSV 文件路径，列约定为 date time pid ts value,ps（空格分隔无表头）

    Returns:
        tuple: (data, names)；文件不存在或为空时 data 为 None，names 为列名列表
    """
    if not os.path.exists(csv_file):
        return None, _CSV_NAMES
    data = pd.read_csv(csv_file, header=None, sep=' ', names=_CSV_NAMES)
    if data.empty:
        return None, _CSV_NAMES
    return _split_value_ps(data), _CSV_NAMES


def _write_csv_joined(csv_file, data, names):
    """拼接 value,ps 后写回 CSV（空格分隔，无表头）

    Args:
        csv_file (str): 目标 CSV 路径，写入前会被删除重建
        data (pd.DataFrame): 含 value/ps 列的 DataFrame
        names (list): 输出列名顺序，如 ['date','time','pid','ts','value,ps']
    """
    os.remove(csv_file)
    _join_value_ps(data)
    data[names].fillna('').to_csv(csv_file, sep=' ', index=False, header=False)


def _index_csvs_by_name(theme_result_path):
    """把目录下所有 CSV 按变量名建立索引

    变量名取文件名最后一个 '+' 之后的 stem，便于按名查找依赖 CSV。

    Args:
        theme_result_path (str): 主题结果目录

    Returns:
        dict: {变量名: CSV 绝对路径}；遍历异常时返回空字典
    """
    csv_by_name = {}
    try:
        for csv_file in Path(theme_result_path).glob('*.csv'):
            csv_by_name[_value_name_from_path(csv_file)] = str(csv_file)
    except Exception as e:
        logger.exception(f'遍历目录失败: {e}')
    return csv_by_name


class RecordPerformance:
    def __init__(self, log_path, theme_lists, time_range, path_config, config_date):
        """初始化性能记录器

        Args:
            log_path (str): 日志根目录
            theme_lists (list): 需要统计的主题列表
            time_range (tuple | None): (start_ts, end_ts) 时间范围，None 表示不过滤
            path_config (dict): 各 soc_type 对应的日志文件名列表映射
            config_date (dict): 各主题的变量统计方法配置
        """
        # 当前日志路径、统计主题和时间范围
        self.log_path = log_path
        self.theme_lists = theme_lists
        self.time_range = time_range
        # 日志文件分布
        self.path_config = path_config
        # 每个变量的统计方法
        self.config_date = config_date

        self.extract_log_path = None  # 待处理的log的路径列表
        self.result_path = None  # log处理结果目录，内有各种csv
        # 各 theme 下本次"有效统计"（CSV 有数据）的 value_name 集合
        # 结构: {theme: set(value_name)}；由 grep_log_worker 写入，用于回写"最新统计时间"
        self.active_value_names = {}
        self._active_value_names_lock = threading.Lock()

    def _collect_active_value_names(self, theme, csv_paths):
        """从已知的 CSV 路径列表中收集"有效统计"（文件非空）的变量名

        变量名取 CSV 文件名最后一个 '+' 之后的 stem（与 _value_name_from_path 一致）。
        空文件、读取异常的 CSV 视为该变量本次未活跃。

        Args:
            theme (str): 主题名
            csv_paths (list[str]): grep_log_worker 已产出的 CSV 路径列表
        """
        active = set()
        for csv_file in csv_paths:
            try:
                if os.path.getsize(csv_file) <= 0:
                    continue
                # CSV 无表头、空格分隔；存在任意非空行即视为该变量本次有效统计
                has_data = False
                with open(csv_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if line.strip():
                            has_data = True
                            break
                if has_data:
                    active.add(_value_name_from_path(str(csv_file)))
            except Exception as e:
                logger.exception(f'_collect_active_value_names: 读取 {csv_file} 失败: {e}')

        if active:
            with self._active_value_names_lock:
                self.active_value_names[theme] = self.active_value_names.get(theme, set()) | active

    def calculate_count(self, csv_path_count, calculate_type="sec", config_date=None):
        """按行数统计帧率

        合并所有 pid（帧率统计不依赖进程），按 ts 分组计数作为 value。
        若配置了阈值阈值（max_threshold/min_threshold），先过滤原始事件行再计数。

        Args:
            csv_path_count (list): 待统计的 CSV 路径列表
            calculate_type (str): 'sec' 按秒统计；'min' 按分钟统计（ts 向下取整到分钟）
            config_date (dict | None): 变量配置，用于查找阈值参数
        """
        for csv_file in csv_path_count:
            data, names = _read_csv_split(csv_file)
            if data is None:
                continue

            # 阈值过滤：在计数前过滤原始事件行
            if config_date is not None:
                try:
                    max_th, min_th = get_thresholds_for_csv(csv_file, config_date)
                    if max_th is not None or min_th is not None:
                        before_len = len(data)
                        data = apply_value_thresholds(data, max_th, min_th, value_col='value')
                        filtered = before_len - len(data)
                        if filtered > 0:
                            vname = _value_name_from_path(csv_file)
                            logger.warning(
                                f'阈值过滤[{vname}]: 计数前过滤 {filtered} 条事件'
                                f'（max={max_th}, min={min_th}）'
                            )
                except Exception as e:
                    logger.warning(f'calculate_count: 阈值过滤异常: {e}')

            # 按分钟统计时把 ts 向下取整到分钟
            if calculate_type == "min":
                data['ts'] = data['ts'] // 60 * 60
            # 帧率统计不依赖 pid，合并所有进程（含进程重启导致的 pid 变化）
            data['pid'] = 1
            data['ts'] = data['ts'].astype(int)

            # 分组统计行数代表帧率
            data = data.groupby(['pid', 'ts', 'ps']).agg(
                date=('date', 'first'),
                time=('time', 'first'),
                value=('value', 'count')
            ).reset_index()
            _write_csv_joined(csv_file, data, names)

    def calculate_diff(self, csv_path_diff):
        """对 value 求相邻差分，diff > -50 时用差分，否则保持原值

        Args:
            csv_path_diff (list): 待差分处理的 CSV 路径列表
        """
        for csv_file in csv_path_diff:
            data, names = _read_csv_split(csv_file)
            if data is None:
                continue

            data["value"] = data["value"].astype(float)
            data["diff"] = data["value"].diff()
            # 向量化：diff 非空且 > -50 时用 diff，否则保持原值（首行 diff 为 NaN 保持原值）
            use_diff = data["diff"].notna() & (data["diff"] > -50)
            data["value"] = data["value"].where(~use_diff, data["diff"])
            data.drop(columns=["diff"], inplace=True)
            _write_csv_joined(csv_file, data, names)

    def calculate_multi_log_diff_value(self, diff_value_entries, theme_result_path):
        """执行 multi_log_diff_value 后处理

        依赖 awk 已生成的各 value CSV，按双指针配对计算
        diff_role_a.value - diff_role_b.value 差值，写入 output_csv。
        支持向后（value_after）和向前（value_before）两种搜索方向。
        必须在 awk 提取及 count/diff 后处理全部完成后调用。

        Args:
            diff_value_entries (list[dict]): 每项含 output_csv / direction / value_base /
                value_target / ps / max_interval / diff_role_a / diff_role_b，
                以及方向相关的 value_after 或 value_before
            theme_result_path (str): 主题结果目录，用于按变量名查找依赖 CSV
        """
        if not diff_value_entries:
            return

        csv_by_name = _index_csvs_by_name(theme_result_path)
        if not csv_by_name:
            return

        for entry in diff_value_entries:
            try:
                output_csv   = entry['output_csv']
                direction    = entry.get('direction', 'backward')
                value_base   = entry['value_base']
                value_target = entry['value_target']
                ps_name      = entry.get('ps')
                max_interval = entry.get('max_interval', -1)
                diff_role_a  = entry.get('diff_role_a', 'value_target')
                diff_role_b  = entry.get('diff_role_b', 'value_base')

                # 根据方向取中间变量
                mid_value_name = entry.get('value_after') if direction == 'backward' else entry.get('value_before')

                base_csv   = csv_by_name.get(value_base)
                mid_csv    = csv_by_name.get(mid_value_name) if mid_value_name else None
                # mid 与 target 相同时复用同一文件
                target_csv = csv_by_name.get(value_target) if value_target != mid_value_name else mid_csv
                ps_csv     = csv_by_name.get(ps_name) if ps_name else None

                # 找不到任一依赖 CSV 则跳过
                missing = []
                if base_csv is None:
                    missing.append(f'value_base={value_base!r}')
                if mid_csv is None:
                    mid_key = 'value_after' if direction == 'backward' else 'value_before'
                    missing.append(f'{mid_key}={mid_value_name!r}')
                if target_csv is None:
                    missing.append(f'value_target={value_target!r}')
                if missing:
                    logger.warning(
                        f'calculate_multi_log_diff_value: 找不到 {", ".join(missing)} 的 CSV，'
                        f'跳过 {output_csv}'
                    )
                    continue

                get_multi_log_diff_value(
                    output_csv, base_csv, mid_csv, target_csv, ps_csv,
                    max_interval, direction, diff_role_a, diff_role_b
                )
                calc_type = entry.get('calculate_type')
                # calculate_type == "count_sec"：对配对成功的事件按秒计数，得到每秒帧率（原行为，保留兼容）
                if calc_type == 'count_sec':
                    self.calculate_count([output_csv], "sec", config_date=self.config_date)
                # calculate_type == "frame_rate_window"：先算相邻间隔（与 _dely 同源，不剔除大间隔），
                # 再每 N 个间隔为一组，用前一组结束时间→本组结束时间的跨度求帧率
                # （组内 ≤上限的有效间隔数 / 跨度秒），跨度 > 上限秒的组整组丢弃
                elif calc_type == 'frame_rate_window':
                    self.calculate_interval_time_diff([output_csv])
                    self._calculate_windowed_fps(output_csv)
            except Exception as e:
                logger.exception(f'calculate_multi_log_diff_value: 处理条目 {entry} 时发生错误: {e}')

        # calculate_type == "interval_time" 的输出 CSV 需二次处理：
        # 对 value 数值做相邻差值（后一条 - 前一条），保留 ps，剔除差值为 0 的行
        interval_time_csvs = [
            e['output_csv'] for e in diff_value_entries
            if e.get('calculate_type') == 'interval_time'
        ]
        self.calculate_interval_time_diff(interval_time_csvs)

    def _calculate_windowed_fps(self, csv_file, window=FRAME_RATE_WINDOW_SIZE,
                                valid_max_ms=FRAME_RATE_WINDOW_INTERVAL_MAX_MS,
                                max_span_seconds=FRAME_RATE_WINDOW_MAX_SPAN_SECONDS):
        """每 window 个间隔为一组（不重叠），用前一组结束时间→本组结束时间的跨度求帧率

        组为 rows [start, start+window-1]，跨度 = ts[组尾] - ts[start-1]（前一组结束时间），
        因此首行只作起始锚点、分组从 index 1 起。帧率 = 组内有效间隔数 / 跨度秒（保留 float 不取整），
        有效间隔 = value ≤ valid_max_ms（丢帧造成的大间隔仍占用时间但不计数）。
        跨度 > max_span_seconds 的组整组丢弃；跨度 ≤0 或无有效间隔也丢弃。ts 列为带毫秒的浮点秒。
        每组输出一行，取组尾打印的 date/time/pid/ts/ps。

        Args:
            csv_file (str): 间隔 CSV（未剔除大间隔，已按 ts 升序，value 为间隔 ms）
            window (int): 每组间隔数
            valid_max_ms (float): 有效间隔上限（ms），≤ 此值才计入帧率次数
            max_span_seconds (float): 组时间跨度上限（秒），超过则整组丢弃
        """
        try:
            data, names = _read_csv_split(csv_file)
            if data is None:
                return
            data = data.reset_index(drop=True)
            ts = pd.to_numeric(data['ts'], errors='coerce')
            value = pd.to_numeric(data['value'], errors='coerce')
            rows = []
            total = len(data)
            for start in range(1, total, window):
                end = min(start + window - 1, total - 1)
                # 跨度取前一组结束时间到本组结束时间
                span_seconds = ts.iloc[end] - ts.iloc[start - 1]
                if not (0 < span_seconds <= max_span_seconds):
                    continue
                valid_count = int((value.iloc[start:end + 1] <= valid_max_ms).sum())
                if valid_count < 1:
                    continue
                member = data.iloc[end]
                rows.append({
                    'date': member['date'], 'time': member['time'], 'pid': member['pid'],
                    'ts': member['ts'], 'value': valid_count / span_seconds, 'ps': member['ps'],
                })
            result = pd.DataFrame(rows, columns=['date', 'time', 'pid', 'ts', 'value', 'ps'])
            _write_csv_joined(csv_file, result, names)
        except Exception as e:
            logger.exception(f'_calculate_windowed_fps: 处理 {csv_file} 时发生错误: {e}')

    def calculate_multi_log_diff_key_value(self, kv_entries, theme_result_path):
        """执行 multi_log_diff_key_value 后处理

        依赖 awk 已生成的各 key/value CSV，按 key 值匹配两个角色，
        计算 diff_role_a.value - diff_role_b.value 差值，写入 output_csv。
        必须在 awk 提取及 count/diff 后处理全部完成后调用。

        Args:
            kv_entries (list[dict]): 每项含 output_csv / role1 / role2 /
                ps / max_interval / diff_role_a / diff_role_b，
                role1/role2 各含 key/value/key_value 三个子字段
            theme_result_path (str): 主题结果目录，用于按变量名查找依赖 CSV
        """
        if not kv_entries:
            return

        csv_by_name = _index_csvs_by_name(theme_result_path)
        if not csv_by_name:
            return

        for entry in kv_entries:
            try:
                output_csv   = entry['output_csv']
                role1        = entry['role1']
                role2        = entry['role2']
                ps_name      = entry.get('ps')
                max_interval = entry.get('max_interval', -1)
                diff_role_a  = entry.get('diff_role_a', 'value_1')
                diff_role_b  = entry.get('diff_role_b', 'value_2')

                role1_key_csv   = csv_by_name.get(role1['key'])
                role1_value_csv = csv_by_name.get(role1['value'])
                role2_key_csv   = csv_by_name.get(role2['key'])
                role2_value_csv = csv_by_name.get(role2['value'])
                ps_csv          = csv_by_name.get(ps_name) if ps_name else None

                # 找不到任一依赖 CSV 则跳过
                missing = []
                if role1_key_csv is None:
                    missing.append(f'role1.key={role1["key"]!r}')
                if role1_value_csv is None:
                    missing.append(f'role1.value={role1["value"]!r}')
                if role2_key_csv is None:
                    missing.append(f'role2.key={role2["key"]!r}')
                if role2_value_csv is None:
                    missing.append(f'role2.value={role2["value"]!r}')
                if missing:
                    logger.warning(
                        f'calculate_multi_log_diff_key_value: 找不到 {", ".join(missing)} 的 CSV，'
                        f'跳过 {output_csv}'
                    )
                    continue

                get_multi_log_diff_key_value(
                    output_csv,
                    role1_key_csv, role1_value_csv, role1['key_value'],
                    role2_key_csv, role2_value_csv, role2['key_value'],
                    ps_csv, max_interval, diff_role_a, diff_role_b
                )
            except Exception as e:
                logger.exception(f'calculate_multi_log_diff_key_value: 处理条目 {entry} 时发生错误: {e}')

    def calculate_interval_time_diff(self, csv_files):
        """对 calculate_type == 'interval_time' 的输出做二次处理

        - 计算相邻行 value 差值（后一条 - 前一条），ps 直接保留
        - 剔除首行(NaN)、差值为 0、差值绝对值 > 1000 的行，结果回写原文件

        Args:
            csv_files (list[str]): 待二次处理的 CSV 路径列表
        """
        col_names = ['date', 'time', 'pid', 'ts', 'value_ps']
        for csv_file in csv_files:
            try:
                if not os.path.exists(csv_file) or os.path.getsize(csv_file) == 0:
                    continue

                data = pd.read_csv(csv_file, header=None, sep=r'\s+', names=col_names)
                if data.empty:
                    continue

                # 拆分 value 与 ps
                split_cols = data['value_ps'].fillna('').astype(str).str.split(',', n=1, expand=True)
                if split_cols.shape[1] == 1:
                    split_cols[1] = ''
                split_cols.columns = ['value', 'ps']
                data[['value', 'ps']] = split_cols
                data['value'] = pd.to_numeric(data['value'], errors='coerce')

                # 相邻差值：后一条 - 前一条；首行 diff 为 NaN
                data['diff'] = data['value'].diff()
                valid_mask = data['diff'].notna() & (data['diff'] != 0) & (data['diff'].abs() <= 1000)
                data = data[valid_mask].copy()

                if data.empty:
                    os.remove(csv_file)
                    open(csv_file, 'w').close()  # 保留空文件占位
                    continue

                # 向量化格式化 value_ps：差值写回，ps 保留原值
                diff_arr = data['diff'].values
                is_int_mask = (diff_arr == np.floor(diff_arr))
                diff_strs = np.where(is_int_mask,
                                     diff_arr.astype(np.int64).astype(str),
                                     diff_arr.astype(str))
                ps_arr = data['ps'].fillna('').astype(str).values.astype('U')
                has_ps = (ps_arr != '') & (np.char.lower(ps_arr) != 'nan')
                data['value_ps'] = np.where(has_ps,
                                            np.char.add(np.char.add(diff_strs, ','), ps_arr),
                                            diff_strs)

                data[col_names].to_csv(csv_file, sep=' ', header=False, index=False)
            except Exception as e:
                logger.exception(f'calculate_interval_time_diff: 处理 {csv_file} 时发生错误: {e}')

    def _resolve_log_dir_name(self, theme, soc_type):
        """按 theme/soc_type 推导日志提取目录名

        soc_type 为 list（配置里逗号分隔多个前缀）时目录名取 theme，否则取 soc_type。
        与 extract_logs 内的取名规则保持一致。

        Args:
            theme (str): 主题名
            soc_type (str | list): 单个前缀或多个前缀列表

        Returns:
            str: 日志提取目录名
        """
        return theme if isinstance(soc_type, list) else soc_type

    def _get_log_dir_tag_union(self, log_dir_name):
        """取某个日志目录下所有 theme 的 log_tag 并集，作为预过滤的 grep 模式

        同一目录被多个 theme 共用（如 FSDA_Thor 下有 19 个 theme），预过滤文件按目录
        共享，因此模式必须覆盖所有 theme 的 tag，否则后来的 theme 会漏行。
        并集是各 theme 所需行的超集，mawk 结果与全量扫描完全一致。

        任一 theme 的 log_tag 含空串时返回 None（空模式会匹配所有行，预过滤失效）。

        Args:
            log_dir_name (str): 日志提取目录名

        Returns:
            list[str] | None: 去重后的 log_tag 列表；不可预过滤时返回 None
        """
        try:
            union_tags = []
            seen = set()
            for theme in self.theme_lists:
                exists, soc_type, var_lists = get_theme(self.config_date, theme)
                if not exists:
                    continue
                if self._resolve_log_dir_name(theme, soc_type) != log_dir_name:
                    continue
                theme_tags = collect_log_tags(var_lists)
                if theme_tags is None:
                    # 该目录下存在空 log_tag 的 theme，整个目录放弃预过滤
                    return None
                for tag in theme_tags:
                    if tag not in seen:
                        seen.add(tag)
                        union_tags.append(tag)
            return union_tags or None
        except Exception as e:
            logger.exception(f'_get_log_dir_tag_union: 收集 {log_dir_name} 的 log_tag 失败: {e}')
            return None

    def get_prefiltered_log_path(self, log_dir_name, extract_log_path_list):
        """取该日志目录的 grep 预过滤结果，供 mawk 替代全量日志扫描

        同一目录只过滤一次，结果按 log_dir_name 缓存供其余 theme 复用。
        任一环节不可用（无 tag / grep 失败 / 命中率过高）时返回 None，
        调用方退回全量扫描，保证不丢数据。

        中间文件写到 logs_traned/.prefiltered/ 下——不能落在 perf/log/，
        那里的 .log 与原始日志共享 inode，只读。

        Args:
            log_dir_name (str): 日志提取目录名
            extract_log_path_list (list[str]): 该目录下的原始日志路径列表（顺序即扫描顺序）

        Returns:
            list[str] | None: 单元素列表 [过滤文件路径]；应退回全量扫描时返回 None
        """
        try:
            with prefilter_cache_lock:
                if log_dir_name in prefilter_cache:
                    return prefilter_cache[log_dir_name]
                if log_dir_name not in prefilter_locks_dict:
                    prefilter_locks_dict[log_dir_name] = threading.Lock()
                current_prefilter_lock = prefilter_locks_dict[log_dir_name]

            with current_prefilter_lock:
                # 双重检查：等锁期间可能已由其他线程完成
                with prefilter_cache_lock:
                    if log_dir_name in prefilter_cache:
                        return prefilter_cache[log_dir_name]

                log_tags = self._get_log_dir_tag_union(log_dir_name)
                if log_tags is None:
                    logger.warning(f'{log_dir_name} 无可用 log_tag，跳过预过滤，使用全量扫描')
                    result = None
                else:
                    filtered_file_path = join(
                        self.log_path, 'logs_traned', '.prefiltered',
                        f'{log_dir_name or "_root"}.log'
                    )
                    filtered = prefilter_logs_by_tags(
                        extract_log_path_list, log_tags, filtered_file_path
                    )
                    result = [filtered] if filtered else None

                with prefilter_cache_lock:
                    prefilter_cache[log_dir_name] = result
                return result
        except Exception as e:
            logger.exception(f'get_prefiltered_log_path: {log_dir_name} 预过滤异常，退回全量扫描: {e}')
            return None

    def extract_logs(self, theme, soc_type):
        """按 theme/soc_type 提取日志文件到 {log_path}/log/{log_dir_name} 目录

        同一 log_dir_name 加锁串行提取，不同目录可并发。已提取过则直接返回现有列表。
        每个文件名按时间范围过滤，第一个超过 end 的文件保留作为边界。

        Args:
            theme (str): 主题名
            soc_type (str | list): 单个前缀或多个前缀列表；列表时 log_dir_name 取 theme

        Returns:
            list[str]: 提取后的 .log 文件绝对路径列表，按文件名排序
        """
        # soc_type 可以是 str（单个前缀）或 list（多个前缀，逗号分隔而来）
        log_dir_name = self._resolve_log_dir_name(theme, soc_type)
        if isinstance(soc_type, list):
            soc_type_list = soc_type
        else:
            soc_type_list = [soc_type]

        # 仅对当前的 log_dir_name 加锁，不影响其他目录的并发执行
        with soc_locks_dict_lock:
            if log_dir_name not in soc_locks_dict:
                soc_locks_dict[log_dir_name] = threading.Lock()
            current_soc_lock = soc_locks_dict[log_dir_name]

        with current_soc_lock:
            extract_log_path = join(self.log_path, 'log', log_dir_name)

            # 已提取过则直接返回对应列表
            if log_dir_name in log_type_list:
                extract_log_path = list(f.as_posix() for f in Path(extract_log_path).glob('**/*.log'))
                return sorted(extract_log_path, key=lambda x: Path(x).name)

            logger.warning(f"🚩 extract theme:{theme} to {log_dir_name} doing")
            log_type_list.append(log_dir_name)
            make_dirs(extract_log_path)

            # 遍历所有 soc_type，复用 path_config 将日志放入目录
            for single_soc_type in soc_type_list:
                # 若 single_soc_type 是具体日志文件名则直接使用，否则从 path_config 获取文件名列表
                valid_filenames = self.path_config.get(single_soc_type, [single_soc_type])

                for file_name in valid_filenames:
                    have_file_name = False
                    state = {"kept_first_after_end": False}

                    log_paths_list = list(Path(self.log_path).glob(f"{file_name}*.log"))
                    log_paths_list.sort(key=lambda p: (lambda dt: (dt is not None, dt))(_parse_cn_dt_from_log_path(p)))

                    for log_path in log_paths_list:
                        if not should_keep_log(log_path, self.time_range, state):
                            continue
                        try:
                            target_path = os.path.join(extract_log_path, os.path.basename(log_path))
                            if not os.path.exists(target_path):
                                # 优先硬链接（快且不占空间）；跨分区退化到软链接或复制
                                try:
                                    os.link(log_path, target_path)
                                except OSError:
                                    try:
                                        os.symlink(log_path, target_path)
                                    except Exception:
                                        shutil.copy2(log_path, target_path)
                            have_file_name = True
                        except Exception as e:
                            logger.error(f"复制日志文件失败: {log_path}")
                            logger.exception(e)
                    if have_file_name:
                        logger.warning(f"🚩 extract {file_name} => {extract_log_path}")

            extract_log_path = list(f.as_posix() for f in Path(extract_log_path).glob('**/*.log'))
            return sorted(extract_log_path, key=lambda x: Path(x).name)

    def grep_log_worker(self, theme, process_executor):
        """单个主题的日志统计工作函数（线程内执行）

        提取日志 → mawk 生成 CSV → 去重 → count/diff/interval_time/multi_log_diff 后处理。

        Args:
            theme (str): 主题名
            process_executor (ProcessPoolExecutor): 用于跑 txt_process_tool 的进程池

        Returns:
            tuple: (theme, theme_result_path)；主题不存在时 theme_result_path 为 None
        """
        theme_result_path = None
        exists, soc_type, var_lists = get_theme(self.config_date, theme)

        if exists:
            extract_log_path = self.extract_logs(theme, soc_type)

            # grep 预过滤：把该目录下命中任意 log_tag 的行抽成单个小文件，
            # 各 theme 的 mawk 只扫这个文件，避免同一批日志被反复全量扫描。
            # 不可用时返回 None，退回原全量扫描路径。
            log_dir_name = self._resolve_log_dir_name(theme, soc_type)
            prefiltered_log_path = self.get_prefiltered_log_path(
                log_dir_name, extract_log_path)
            mawk_input_path = prefiltered_log_path or extract_log_path

            # 创建主题子文件夹 {root_folder}/perf/logs_traned/{theme}
            theme_result_path = join(self.log_path, 'logs_traned', theme)
            make_dirs(theme_result_path)

            logger.warning(f"🚩{theme} 统计单个日志")
            # 独立进程运行，提取生产 csv
            future = process_executor.submit(
                txt_process_tool,
                var_lists, theme_result_path, mawk_input_path, theme, self.time_range
            )
            csv_path, csv_path_count_sec, csv_path_count_min, csv_path_diff, \
                csv_path_multi_log_diff_value, csv_path_multi_log_diff_key_value = future.result()

            rm_duplicate_csv(csv_path)
            rm_duplicate_csv(csv_path_count_sec)
            rm_duplicate_csv(csv_path_count_min)
            # 部分 csv 特殊统计处理
            self.calculate_count(csv_path_count_sec, "sec", config_date=self.config_date)
            self.calculate_count(csv_path_count_min, "min", config_date=self.config_date)
            self.calculate_diff(csv_path_diff)
            # multi_log_diff_value：依赖前面所有统计结果，最后执行
            self.calculate_multi_log_diff_value(csv_path_multi_log_diff_value, theme_result_path)
            # multi_log_diff_key_value：依赖前面所有统计结果，最后执行
            self.calculate_multi_log_diff_key_value(csv_path_multi_log_diff_key_value, theme_result_path)

            # 收集本次"有效统计"的变量名（CSV 有数据），供回写"最新统计时间"使用
            # 直接复用 grep_log_worker 已持有的 csv_path 列表，省一遍目录扫描
            self._collect_active_value_names(theme, csv_path)
        else:
            # 不存在则跳过，可能是其他平台的配置
            logger.warning(f"🚩config_date中不包含{theme}主题的统计方法")

        return theme, theme_result_path

    def grep_log(self):
        """并发统计所有主题的日志

        使用 ProcessPoolExecutor 跑 mawk（CPU 密集），ThreadPoolExecutor 调度各主题。
        结果存入 self.result_path：{theme: theme_result_path}。
        """
        result_path = {}
        max_processes = get_dynamic_max_processes()
        logger.warning(f"current max_processes = {max_processes} (cpu={os.cpu_count()}, load1m={os.getloadavg()[0]:.1f})")
        _mp_ctx = multiprocessing.get_context('spawn')
        with ProcessPoolExecutor(max_workers=max_processes, mp_context=_mp_ctx) as process_executor:
            with ThreadPoolExecutor(max_workers=multi_log_workers) as thread_executor:
                # 把 process_executor 作为参数传给 grep_log_worker
                futures = [thread_executor.submit(self.grep_log_worker, theme, process_executor)
                           for theme in self.theme_lists]

                for future in as_completed(futures):
                    res_theme, res_path = future.result()
                    if res_path is not None:
                        result_path[res_theme] = res_path

        self.result_path = result_path
        self._cleanup_prefiltered_files()

    def _cleanup_prefiltered_files(self):
        """删除 grep 预过滤产生的中间文件

        这些文件是 mawk 的临时输入，统计完成后即无用（单次运行可累计数百 MB）。
        删的是 logs_traned/.prefiltered/ 下自建的普通文件，与 perf/log/ 下
        共享 inode 的原始日志无关，不会波及原始数据。

        清理失败仅告警，不影响统计结果。
        """
        prefiltered_dir = join(self.log_path, 'logs_traned', '.prefiltered')
        try:
            if os.path.isdir(prefiltered_dir):
                shutil.rmtree(prefiltered_dir)
                logger.warning(f'🧹 已清理预过滤中间文件: {prefiltered_dir}')
        except Exception as e:
            logger.exception(f'清理预过滤中间文件失败（不影响统计结果）: {e}')
        finally:
            # 缓存与文件生命周期一致，避免同进程内复用已删除的路径
            try:
                with prefilter_cache_lock:
                    prefilter_cache.clear()
            except Exception as e:
                logger.exception(f'清空预过滤缓存失败: {e}')
