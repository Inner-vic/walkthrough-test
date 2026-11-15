"""
性能分析工具函数集
"""

from os.path import join
import re
import base64
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytz

from .perf_log import logger

# CSV 列名约定：date time pid ts value[,ps]
_CSV_COL_NAMES = ['date', 'time', 'pid', 'ts', 'value_ps']


def get_dynamic_max_processes():
    """按 1 分钟负载动态计算进程数：至少 1，至多 cpu 核心数的一半

    Returns:
        int: 建议的进程数；获取负载失败时返回 2
    """
    cpu_count = os.cpu_count() or 4
    try:
        load_avg_1min = os.getloadavg()[0]
    except OSError:
        return 2
    return max(1, int((cpu_count - load_avg_1min) * 0.5))


# ==================== 时间处理 ====================

def timestamp_to_timestr(timestamp):
    """时间戳 → "HH:MM:SS.mmm"（供 count_obs 使用）

    Args:
        timestamp (int | float): epoch 秒级时间戳

    Returns:
        str: 形如 '14:23:45.123' 的时间字符串
    """
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f").split(" ")[-1][:-4]


def format_period(_time_str):
    """时间段 "YYYYMMDDHHMMSS/YYYYMMDDHHMMSS" → (start_ts, end_ts)

    Args:
        _time_str (str): 时间段字符串，'/' 分隔起止时间

    Returns:
        tuple | None: (start_ts, end_ts) epoch 秒；解析失败返回 None

    Raises:
        AssertionError: start 不早于 end
    """
    try:
        start = time.mktime(time.strptime(_time_str.split('/')[0], "%Y%m%d%H%M%S"))
        end = time.mktime(time.strptime(_time_str.split('/')[1], "%Y%m%d%H%M%S"))
    except Exception:
        return
    assert start < end, '[开始时间]必须早于[结束时间].'
    return start, end


def year_from_range(time_range):
    """从时间范围取起始时间戳对应的年份

    Args:
        time_range (tuple): (start_ts, end_ts) epoch 秒

    Returns:
        int: 起始时间戳对应的 4 位年份
    """
    return datetime.fromtimestamp(time_range[0]).year


# ==================== 文件处理 ====================

def make_dirs(dir_path, rm_exist=True):
    """创建目录；rm_exist=True 时先清空目录内 .png/.jpg/.csv

    Args:
        dir_path (str | Path): 目标目录路径
        rm_exist (bool): 目录已存在时是否先清空其中的 .png/.jpg/.csv 文件
    """
    dir_path = Path(dir_path)
    if rm_exist and dir_path.is_dir():
        list(map(os.remove, dir_path.glob('*.png')))
        list(map(os.remove, dir_path.glob('*.jpg')))
        list(map(os.remove, dir_path.glob('*.csv')))
    if not dir_path.is_dir():
        os.makedirs(dir_path, exist_ok=True)


def rm_duplicate_csv(file_list):
    """同名（按 '+' 后缀）CSV 去重，保留数据量更大的版本

    Args:
        file_list (list[str]): CSV 文件路径列表，文件名格式 'N+value_name.csv'；
                                同 value_name 的文件两两比较，保留数据量更大者
    """
    file_dict = {}
    for filepath in file_list[:]:
        filename = os.path.basename(filepath)
        directory = os.path.dirname(filepath)
        if not os.path.isfile(filepath):
            continue
        suffix = filename.split("+")[1]
        if suffix not in file_dict:
            file_dict[suffix] = filename
            continue
        existing_file = os.path.join(directory, file_dict[suffix])
        existing_size = os.path.getsize(existing_file)
        if (os.path.getsize(filepath) < existing_size > 0
                and pd.read_csv(existing_file, header=None, sep=' ').iloc[:, -1].sum() != 0):
            os.remove(filepath)
        else:
            os.remove(existing_file)
            file_dict[suffix] = filename


# ==================== 飞书表格坐标/图片 ====================

def parse_sheet_coordinate(sheet_coordinate):
    """坐标字符串 "sheetId!A1:B2" ↔ 元组 (sheetId, (col, row), (col, row)) 互转

    Args:
        sheet_coordinate (str | tuple): 字符串坐标，或 (sheetId, (col, row), (col, row)) 元组

    Returns:
        tuple | str: 输入为 str 返回元组；输入为元组返回字符串
    """
    if isinstance(sheet_coordinate, str):
        sheet_id, coordinate = sheet_coordinate.split('!')
        st, ed = coordinate.split(':')

        def _o(x, _t='s'):
            fx = {'s': str.isalpha, 'i': str.isdigit}
            lt = ''.join(filter(fx[_t], x))
            return ord(lt) - 65 if _t == 's' else int(lt)

        return sheet_id, (_o(st), _o(st, 'i')), (_o(ed), _o(ed, 'i'))
    a, (b, c), (d, e) = sheet_coordinate
    return a + '!' + chr(b + 65) + str(c) + ':' + chr(d + 65) + str(e)


def img_to_stream(file_path):
    """图片文件 → base64 字符串

    PNG 图片自动转为 256 色调色板模式以减小体积（无损清晰度）。
    非图片或转换失败时回退为原始文件直读。

    Args:
        file_path (str): 图片文件路径

    Returns:
        str: base64 编码后的字符串
    """
    from PIL import Image
    import io

    try:
        if file_path.lower().endswith('.png'):
            img = Image.open(file_path)
            if img.mode != 'P':
                img = img.convert('P', palette=Image.ADAPTIVE, colors=256)
            buf = io.BytesIO()
            img.save(buf, format='PNG', optimize=True)
            return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception:
        pass
    with open(file_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def read_csv_file(csv_file, **kwargs):
    """读取 CSV（容错：空文件/解析失败均返回 None）

    Args:
        csv_file (str): CSV 文件路径
        **kwargs: 透传给 pandas.read_csv 的额外参数

    Returns:
        pandas.DataFrame | None: 读取成功且非空返回 DataFrame，否则 None
    """
    try:
        df = pd.read_csv(csv_file, header=None, sep='\\s+', **kwargs)
        return None if df.empty else df
    except FileNotFoundError as e:
        logger.warning(f'读取CSV文件失败 {csv_file}: {e}')
    except pd.errors.EmptyDataError as e:
        logger.warning(f'CSV文件为空 {csv_file}: {e}')
    except pd.errors.ParserError as e:
        logger.warning(f'CSV文件解析失败 {csv_file}: {e}')
    except Exception as e:
        logger.exception(f'read_csv_file 发生未预期错误 {csv_file}: {e}')
    return None


# ==================== 单位/其他 ====================

def get_unit_by_title(_title):
    """根据变量名后缀推断单位：_cpu/_mem/_gpu/_rate→%, _delay/_interval/_cost→ms, _fps→Hz, _usage→GB

    Args:
        _title (str): 变量名，按 '_' 分段取最后一段匹配单位

    Returns:
        str: 带单位后缀的变量名（如 'xxx_cpu(%)'）；无匹配时原样返回
    """
    t = _title.split('_')[-1]
    unit_map = {
        ('cpu', 'mem', 'gpu', 'rate'): '%',
        ('delay', 'interval', 'cost'): 'ms',
        ('fps',): 'Hz',
        ('usage',): 'GB',
    }
    for keys, unit in unit_map.items():
        if t in keys:
            return f'{_title}({unit})'
    return _title


def generator_increase():
    """从 1 开始的无限递增整数生成器

    Yields:
        int: 1, 2, 3, ...
    """
    i = 0
    while True:
        i += 1
        yield i


def time_this(func):
    """函数耗时装饰器：执行完成后 logger.warning 打印耗时

    Args:
        func (callable): 被装饰函数

    Returns:
        callable: 包装后的函数，返回值与原函数一致
    """
    def wrapper(*args, **kwargs):
        start = time.time()
        x = func(*args, **kwargs)
        logger.warning(f'{func.__name__} run time: {time.time() - start}')
        return x
    return wrapper


# ==================== 绘图辅助 ====================

def cut_line(df, keys, delta=15, g_column='g'):
    """按时间断点（>delta）与 pid 变化切分分组列 g，用于折线图分段

    Args:
        df (pandas.DataFrame): 含时间戳和 pid 的数据
        keys (list): [时间列名, pid 列名]
        delta (int): 时间差阈值，超过则视为断点
        g_column (str): 写入分组编号的列名

    Returns:
        pandas.DataFrame: 含 g 列的原始 df
    """
    df[g_column] = 0
    key, key2 = keys

    def add_gi(x):
        df.loc[x.name:, g_column] += 1

    tmp = df[key] - df[key].shift()
    df.loc[tmp > delta].apply(add_gi, axis=1)
    tmp = df[key2] != df[key2].shift()
    df.loc[tmp].apply(add_gi, axis=1)
    return df


def lttb_downsample(x, y, threshold=800):
    """LTTB (Largest Triangle Three Buckets) 降采样

    数据点超过 threshold 时，按三角面积最大化策略选点，
    保留视觉形状特征（峰、谷、趋势）的同时减少点数。
    常用于密集折线图，避免数据点堆叠导致线条过粗。

    Args:
        x: x 轴数据（pandas Series / numpy array，支持 datetime64）
        y: y 轴数据（pandas Series / numpy array）
        threshold: 目标点数上限，默认 800

    Returns:
        tuple: (降采样后的 x, 降采样后的 y)，numpy array
    """
    n = len(x)
    if n <= threshold:
        return np.asarray(x), np.asarray(y)

    x_arr = np.asarray(x)
    y_arr = np.asarray(y)

    # datetime64 → int64 用于面积计算
    if x_arr.dtype.kind == 'M':
        x_calc = x_arr.view('i8')
    else:
        x_calc = x_arr.astype('f8')
    y_calc = y_arr.astype('f8')

    bucket_size = (n - 2) / (threshold - 2)

    sampled = [0]  # 首点保留
    a = 0          # 上一个选中点

    for i in range(threshold - 2):
        # 当前桶范围
        s = int(np.floor(i * bucket_size)) + 1
        e = int(np.floor((i + 1) * bucket_size)) + 1
        s = min(max(s, 1), n - 2)
        e = min(max(e, s + 1), n - 1)

        # 下一桶均值（最后一轮用尾点）
        ns = e
        ne = min(int(np.floor((i + 2) * bucket_size)) + 1, n)
        ne = max(ne, ns + 1)
        if i == threshold - 3:
            ns, ne = n - 1, n

        avg_next_x = x_calc[ns:ne].mean()
        avg_next_y = y_calc[ns:ne].mean()

        # 在当前桶中选三角面积最大的点
        xs = x_calc[s:e]
        ys = y_calc[s:e]
        areas = np.abs(
            x_calc[a] * (ys - avg_next_y) +
            xs * (avg_next_y - y_calc[a]) +
            avg_next_x * (y_calc[a] - ys)
        )
        max_idx = s + int(np.argmax(areas))
        sampled.append(max_idx)
        a = max_idx

    sampled.append(n - 1)  # 尾点保留
    indices = np.array(sampled, dtype=np.intp)
    return x_arr[indices], y_arr[indices]


def adaptive_linewidth(n_points, fig_width=18):
    """根据数据点数和画布宽度自适应选择线宽

    以 18 英寸 @100DPI 为基准（绘图区约 1620px），阈值随画布宽度线性
    缩放：画布越宽，同密度对应的点数越多，可容忍更粗的线宽。
    线宽像素 = linewidth * 100/72 ≈ linewidth * 1.39px
    点间距 = fig_width * 90 / n_points px
    线宽占间距 <50% 时视觉清晰、无带状堆叠。

    | 点数(18in基准) | 线宽 |
    |----------------|------|
    | ≤100           | 1.5  |
    | ≤300           | 1.0  |
    | ≤600           | 0.7  |
    | >600           | 0.5  |

    Args:
        n_points (int): 折线数据点数（降采样后）
        fig_width (float): 画布宽度（英寸），默认 18

    Returns:
        float: matplotlib linewidth 值
    """
    scale = fig_width / 18
    if n_points <= 100 * scale:
        return 1.5
    elif n_points <= 300 * scale:
        return 1.0
    elif n_points <= 600 * scale:
        return 0.7
    else:
        return 0.5


def calc_figsize_by_timespan(seconds, height=9, base_width=18, max_width=40,
                           base_hours=0.75, max_hours=2.5):
    """根据时间跨度计算画布尺寸，超过基准时长后线性加宽至上限

    时间跨度在 base_hours 以内时保持固定宽度 base_width（与原始一致），
    超过后在 base_hours~max_hours 区间线性增长至 max_width，
    超过 max_hours 后不再继续加宽。

    | 时间跨度  | 画布宽度 | 说明             |
    |-----------|----------|------------------|
    | 0~45min   | 18in     | 固定（与原一致） |
    | 1h        | 21in     | 线性增长         |
    | 1.5h      | 27in     |                  |
    | 2h        | 34in     |                  |
    | ≥2.5h     | 40in     | 上限             |

    Args:
        seconds (float): 时间跨度（秒）
        height (float): 画布高度（英寸），固定不变
        base_width (float): 基准宽度（英寸），时间跨度以内保持此宽度
        max_width (float): 最大宽度（英寸）
        base_hours (float): 基准时长（小时），超过后开始线性加宽
        max_hours (float): 上限时长（小时），达到后宽度封顶

    Returns:
        tuple: (width, height)
    """
    hours = max(0, seconds) / 3600
    if hours <= base_hours:
        return (base_width, height)
    if hours >= max_hours:
        return (max_width, height)
    ratio = (hours - base_hours) / (max_hours - base_hours)
    width = base_width + (max_width - base_width) * ratio
    return (width, height)


def processing_error(data, sca=100):
    """剔除异常值：负值及偏离 95 分位均值 sca 倍以上的点

    Args:
        data (pandas.Series): 数值序列
        sca (int): 偏离 95 分位均值的倍数阈值

    Returns:
        tuple: (None, 过滤后的 Series)
    """
    data = data.abs()
    mu = data.loc[data <= data.quantile(.95)].mean()
    if mu == 0:
        mu = 0.1
    m = (np.abs(data - mu) > sca * mu) | (data < 0)
    return None, data[~m]


def _get_mktime_expr(theme):
    """根据主题返回 awk mktime 表达式（不含结尾分号）

    Args:
        theme (str): 主题名，'Tc397' 前缀走特殊日期字段顺序

    Returns:
        str: awk mktime 表达式字符串
    """
    if theme.startswith("Tc397"):
        return 'mktime(ny" "a[2]" "a[3]" "b[1]" "b[2]" "b[3])"."c[2]'
    return 'mktime(ny" "a[1]" "a[2]" "b[1]" "b[2]" "b[3])"."c[2]'


def _build_awk_cac_template(var_name, mktime_expr):
    """构造含 %s 占位符的 awk 动作块基础模板

    Args:
        var_name (str): 输出 CSV 文件路径
        mktime_expr (str): awk mktime 表达式

    Returns:
        str: 含 %s 占位符的 awk 动作块字符串
    """
    return (
        '\n    split($1,a,"-") ;\n'
        '    split($2,c,".") ;\n'
        '    split(c[1] , b , ":") ;\n'
        f'    xts = {mktime_expr} ;\n'
        '    %s\n'
        f'    print $1,$2,$3,xts,var >>  "{var_name}" ;\n'
    )


def _build_common_awk_cac(var_func, var_input, var_log_tag, var_name, mktime_expr):
    """构造常见 var_func 类型的完整 awk 动作块

    Args:
        var_func (str): 处理类型，default / brackets / key_next / custom / special_filed / timestamp
        var_input (str | list): 输入参数，类型依 var_func 而定
        var_log_tag (str): 日志匹配标签，仅 key_next 使用
        var_name (str): 输出 CSV 文件路径
        mktime_expr (str): awk mktime 表达式

    Returns:
        str | None: awk 动作块字符串；None 表示该类型由调用方自行处理

    Notes:
        special_filed 使用传入的 mktime_expr（修复了原始代码中忽略 Tc397 主题的 bug）；
        timestamp 将 $1 $2 拼接为时间字符串后直接用 xts + c[2]，避免 to_ms 每行 fork date -d。
    """
    if var_func == 'special_filed':
        return (
            '\n    split($1,a,"-") ;\n'
            '    split($2,c,".") ;\n'
            '    split(c[1] , b , ":") ;\n'
            f'    xts = {mktime_expr} ;\n'
            f'    {var_input[0]}\n'
            f'    print $1,$2,$3,xts,{var_input[1]} >>  "{var_name}" ;\n'
        )

    tmpl = _build_awk_cac_template(var_name, mktime_expr)
    actions = {
        'default': f'var = default(${var_input});',
        'brackets': f'var = brackets(${var_input});',
        'key_next': f'var = key_next("{var_log_tag}");',
        'custom': var_input,
        # xts 由模板用 mktime 已算好（格式 "1781141580.785"），c[2] 是毫秒部分，
        # 直接拼接避免 to_ms 内每行 fork date -d（每次 ~1.7ms，百万行量级下极慢）
        'timestamp': 'var = int(xts) substr(c[2] "000", 1, 3);',
    }
    action = actions.get(var_func)
    return tmpl % action if action is not None else None


def _build_awk_program(context: str, nye: int) -> str:
    """构造完整的 mawk 程序字符串（不含 mawk 命令外壳），包含所有公共函数定义

    Args:
        context (str): awk 动作块拼接字符串，由 _build_common_awk_cac 等生成
        nye (int): 年份，用于补全日志日期中的年份字段

    Returns:
        str: 可直接传给 mawk 的 awk 程序字符串
    """
    return '''
        function default(e){{
            where = match(e, /-?[0-9]+(.[0-9]+)?/);
            v = "";
            if (where){{
                v = substr(e, RSTART, RLENGTH);
            }}
            return v ;
        }}
        function find_digit(e){{
            where = match(e, /-?[0-9]+(.[0-9]+)?/);
            v = "";
            if (where){{
                v = substr(e, RSTART, RLENGTH);
            }}
            return v ;
        }}
        function brackets(e){{
            where = match(e, /\\([^()]*\\)/);
            v = "";
            if (where) {{
                var = substr(e, RSTART + 1, RLENGTH - 2);
                where = match(var, /-?[0-9]+(.[0-9]+)?/);
                if (where) {{
                    v = substr(var, RSTART, RLENGTH);
                }} else {{
                    v = "";
                }}
            }}
            return v ;
        }}
        function key_next(e){{
            split($0,kdn,e);
            gsub(/^[ \\t]+|[ \\t]+$/, "", kdn[2]);
            where = match(kdn[2], /^-?[0-9]+(.[0-9]+)?/);
            v = "";
            if (where){{
                v = substr(kdn[2], RSTART, RLENGTH);
            }}
            return v
        }}
        function find_perception_state(){{
            split($0,d1,"perception_state:");
            v = substr(d1[2], 1, 10);
            v = find_digit(v)
            return v
        }}
        function classify_brake(b7, b10,   n1, n2, b1, b2, bit3_1, bit2_0){{
            n1 = "0x" b7; n2 = "0x" b10
            b1 = n1+0; b2 = n2+0
            bit3_1 = int(b1/2) % 8
            bit2_0 = b2 % 8
            if (bit3_1 == 1 && bit2_0 == 0) return "自研EMB"
            if (bit3_1 == 1 && bit2_0 == 1) return "博世EMB"
            return "其他制动"
        }}
        function to_ms(date_str,   cmd, out){{
            ms = "000"
            base = date_str

            if (match(date_str, /\\.[0-9]+$/)) {{
                ms = substr(date_str, RSTART + 1, RLENGTH - 1)
                base = substr(date_str, 1, RSTART - 1)

                if (length(ms) > 3)
                    ms = substr(ms, 1, 3)
                while (length(ms) < 3)
                    ms = ms "0"
            }}

            if (base !~ /^[0-9]{{4}}-/)
                base = ny "-" base

            cmd = "date -d \\"" base " +0800\\" +%s"
            cmd | getline sec
            close(cmd)

            if (sec !~ /^[0-9]+$/)
                return ""

            return sec "" ms
        }}
        function ts_to_ms(s,   p, sec, frac, v){{
            # 将时间戳字符串归一化为整数毫秒（纯字符串拼接，规避 mawk 32 位整数溢出）：
            #   含小数点        → 秒.小数，小数截断/补零到 3 位，拼成 sec+3位毫秒
            #   无小数点 < 1e10 → 视为秒，末尾补 "000"
            #   无小数点 >=1e10 → 视为毫秒，原样返回
            #   科学计数法(含 e/E)→ 只能数值化（精度已丢失，仅兜底）
            # 供 custom func 嵌套使用，如：var = ts_to_ms(key_next("xxx"));
            if (s == "") return "";
            if (s ~ /[eE]/) {{
                v = s + 0;
                return (v < 10000000000) ? sprintf("%.0f", v * 1000) : sprintf("%.0f", v);
            }}
            p = index(s, ".");
            if (p == 0) {{
                return (s + 0 < 10000000000) ? s "000" : s;
            }} else {{
                sec  = substr(s, 1, p - 1);
                frac = substr(s, p + 1);
                if (length(frac) > 3) frac = substr(frac, 1, 3);
                while (length(frac) < 3) frac = frac "0";
                return sec frac;
            }}
        }}
        {{
            {1}
            {0}
        }}'''.format(context, "ny = %s;" % nye)


def _build_awk_cmd(context: str, nye: int) -> str:
    """已废弃：原返回含 'mawk' 外壳的命令字符串，供 os.system 调用。

    新代码应使用 _build_awk_program 获取 awk 程序，再用 subprocess.run 调用 mawk。
    保留此函数仅为向后兼容，内部委托给 _build_awk_program。
    """
    return 'mawk ' + _build_awk_program(context, nye)



def _parse_kv_pairs(text):
    """解析 key="value" 与 key=number 形式参数；后者不覆盖前者

    Args:
        text (str): 原始参数字符串，如 'ps="ps_time" max_interval=500'

    Returns:
        dict: 参数键值对，字符串值去引号，数字保留字符串形式
    """
    params = {}
    for match in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', text):
        params[match.group(1)] = match.group(2)
    for match in re.finditer(r'(\w+)\s*=\s*(-?\d+(?:\.\d+)?)', text):
        params.setdefault(match.group(1), match.group(2))
    return params


def _parse_diff_value_input(input_str):
    """解析 multi_log_diff_value 的 input 字符串为参数字典

    格式：参数段 ; var = (roleA - roleB),ps
      - 分号前为 key="value" 或 key=数字 形式的参数
      - 分号后为可选的差值公式，省略则默认 value_target - value_base
      - passthrough 格式 var = role[,ps]：直接输出该角色原始 value，不做差值

    方向由参数字段自动判断（互斥）：
      - value_after  → backward：以 base 为锚向后找第一条 after_ts >= base_ts，再向后找 target
      - value_before → forward ：以 base 为锚向前找最近一条 before_ts <= base_ts，再向前找 target

    通用字段：ps（ps 标志位变量名，可空）、max_interval（ms，-1 不限）

    Args:
        input_str (str | list | tuple): 原始 input 字符串，如
            'value_base="A" value_after="B" value_target="C" ps="P" max_interval=500 ; var=(value_target - value_base)'
            列表/元组会被逗号拼接成字符串

    Returns:
        dict: 包含字段 direction / value_base / value_after / value_before / value_target /
              ps / max_interval / diff_role_a / diff_role_b；diff_role_b 为 None 表示 passthrough 模式

    Raises:
        ValueError: 缺少必填字段 value_base/value_target；或同时含/同时缺 value_after 与 value_before
    """
    if isinstance(input_str, (list, tuple)):
        input_str = ','.join(str(x) for x in input_str)

    parts = input_str.split(';', 1)
    param_str = parts[0].strip()
    formula_str = parts[1].strip() if len(parts) > 1 else ''

    params = _parse_kv_pairs(param_str)

    for required_key in ('value_base', 'value_target'):
        if required_key not in params:
            raise ValueError(
                f'multi_log_diff_value input 缺少必填字段 {required_key!r}，'
                f'原始 input: {input_str!r}'
            )

    has_after = 'value_after' in params
    has_before = 'value_before' in params
    if has_after and has_before:
        raise ValueError(
            f'multi_log_diff_value input 不能同时含 value_after 和 value_before，'
            f'原始 input: {input_str!r}'
        )
    if not has_after and not has_before:
        raise ValueError(
            f'multi_log_diff_value input 必须包含 value_after（向后）或 value_before（向前）之一，'
            f'原始 input: {input_str!r}'
        )

    direction    = 'backward' if has_after else 'forward'
    value_after  = params.get('value_after')
    value_before = params.get('value_before')

    # 解析公式段：var = (roleA - roleB)
    # 可用角色名：value_base / value_after（或 value_before） / value_target
    # value_after / value_before 在函数内统一映射为 value_mid，此处一并规范化
    diff_role_a = 'value_target'
    diff_role_b = 'value_base'
    if formula_str:
        # 格式1：差值 var = (roleA - roleB)
        formula_match = re.search(r'var\s*=\s*\(\s*(\w+)\s*-\s*(\w+)\s*\)', formula_str)
        # 格式2：passthrough  var = role 或 var = role,ps
        passthrough_match = re.search(r'var\s*=\s*(value_\w+)\s*(?:,|$)', formula_str) if not formula_match else None
        if formula_match:
            diff_role_a = formula_match.group(1).strip()
            diff_role_b = formula_match.group(2).strip()
        elif passthrough_match:
            diff_role_a = passthrough_match.group(1).strip()
            diff_role_b = None   # None 标记 passthrough 模式：直接输出 roleA 的原始值
        else:
            logger.warning(
                f'multi_log_diff_value: 无法解析公式段 {formula_str!r}，'
                f'使用默认 value_target - value_base'
            )

    # 将公式中的 value_after / value_before 统一规范为 value_mid，
    # 与 get_multi_log_diff_value 内 role_values 的键保持一致
    # diff_role_b 为 None 时为 passthrough 模式，不处理
    _alias_to_mid = {'value_after', 'value_before'}
    if diff_role_a in _alias_to_mid:
        diff_role_a = 'value_mid'
    if diff_role_b is not None and diff_role_b in _alias_to_mid:
        diff_role_b = 'value_mid'

    return {
        'direction':    direction,
        'value_base':   params['value_base'],
        'value_after':  value_after,
        'value_before': value_before,
        'value_target': params['value_target'],
        'ps':           params.get('ps', None),
        'max_interval': int(float(params.get('max_interval', -1))),
        'diff_role_a':  diff_role_a,
        'diff_role_b':  diff_role_b,
    }


def _parse_diff_key_value_input(input_str):
    """解析 multi_log_diff_key_value 的 input 字符串为参数字典

    与 _parse_diff_value_input 不同：本函数按 key 值匹配两个角色，
    key 与 value 均为已由 awk 生成的独立变量 CSV。

    格式：
        key_1="...",value_1="...",key_value_1="key_before" ;
        key_2="...",value_2="...",key_value_2="value_before" ;
        ps="...",max_interval=-1 ;
        var=(value_1 - value_2),ps

    key_value_N 仅允许 'key_before'（向前找 key_ts <= value_ts）或 'value_before'（向后找 key_ts >= value_ts）。
    公式 var=(roleA - roleB)，role 仅可为 value_1/value_2，省略默认 value_1 - value_2。

    Args:
        input_str (str | list | tuple): 原始 input 字符串；列表/元组会被逗号拼接成字符串

    Returns:
        dict: 包含字段 role1 / role2（各含 key/value/key_value 三个子字段）、
              ps / max_interval / diff_role_a / diff_role_b

    Raises:
        ValueError: 缺少必填字段 key_N/value_N/key_value_N；或 key_value_N 非 'key_before'/'value_before'
    """
    if isinstance(input_str, (list, tuple)):
        input_str = ','.join(str(x) for x in input_str)

    formula_match = re.search(r'var\s*=\s*\(\s*(\w+)\s*-\s*(\w+)\s*\)', input_str)
    formula_str = formula_match.group(0) if formula_match else ''
    param_str = input_str.replace(formula_str, ' ') if formula_str else input_str
    params = _parse_kv_pairs(param_str)

    roles = {}
    for idx in (1, 2):
        role = {}
        for field in ('key', 'value', 'key_value'):
            full_key = f'{field}_{idx}'
            if full_key not in params:
                raise ValueError(
                    f'multi_log_diff_key_value input 缺少必填字段 {full_key!r}，'
                    f'原始 input: {input_str!r}'
                )
            role[field] = params[full_key]
        if role['key_value'] not in ('key_before', 'value_before'):
            raise ValueError(
                f'multi_log_diff_key_value input 字段 key_value_{idx}={role["key_value"]!r} 非法，'
                f'仅允许 "key_before" 或 "value_before"，原始 input: {input_str!r}'
            )
        roles[idx] = role

    diff_role_a, diff_role_b = 'value_1', 'value_2'
    if formula_match:
        diff_role_a = formula_match.group(1).strip()
        diff_role_b = formula_match.group(2).strip()
        if diff_role_a not in ('value_1', 'value_2') or diff_role_b not in ('value_1', 'value_2'):
            logger.warning(
                f'multi_log_diff_key_value: 公式角色 {diff_role_a!r}/{diff_role_b!r} 非法，'
                f'仅允许 value_1/value_2，使用默认 value_1 - value_2'
            )
            diff_role_a, diff_role_b = 'value_1', 'value_2'
    elif formula_str:
        logger.warning(
            f'multi_log_diff_key_value: 无法解析公式段 {formula_str!r}，'
            f'使用默认 value_1 - value_2'
        )

    return {
        'role1':        roles[1],
        'role2':        roles[2],
        'ps':           params.get('ps', None),
        'max_interval': int(float(params.get('max_interval', -1))),
        'diff_role_a':  diff_role_a,
        'diff_role_b':  diff_role_b,
    }


def get_multi_log_diff_value(
    output_csv, base_csv, mid_csv, target_csv, ps_csv,
    max_interval_ms, direction, diff_role_a, diff_role_b
):
    """以 value_base 为基准，双指针配对 mid（after/before）和 target，计算差值

    向后（backward）：mid_ts >= base_ts 取第一条，target_ts >= mid_ts 取第一条
    向前（forward） ：mid_ts <= base_ts 取最近一条，target_ts <= mid_ts 取最近一条

    diff_role_b 为 None 表示 passthrough 模式：直接输出 roleA 的原始 value。
    时间差 abs(target_ts - base_ts)*1000 仅用于 max_interval 过滤。
    输出行取自 base 的 date/time/pid/ts。双指针 O(n)，ps 用 searchsorted 二分查找。

    Args:
        output_csv (str): 输出 CSV 路径，空格分隔无表头，列顺序 date time pid ts value_ps
        base_csv (str): value_base 变量 CSV 路径，作为配对锚点
        mid_csv (str): value_after/value_before 变量 CSV 路径，中间桥接角色
        target_csv (str): value_target 变量 CSV 路径，最终差值参与方；与 mid_csv 相同时复用
        ps_csv (str | None): ps 标志位变量 CSV 路径，None 表示不解析 ps
        max_interval_ms (int): base 与 target 时间差上限（毫秒），-1 表示不限
        direction (str): 'backward' 或 'forward'
        diff_role_a (str): 差值被减数角色，'value_base'/'value_mid'/'value_target'
        diff_role_b (str | None): 差值减数角色；None 表示 passthrough 模式

    Returns:
        DataFrame | None: 写入成功返回结果 DataFrame；任一输入缺失/无匹配返回 None
    """
    output_name = _value_name_from_path(output_csv)
    base_name = _value_name_from_path(base_csv)
    mid_name = _value_name_from_path(mid_csv)
    target_name = _value_name_from_path(target_csv)

    base_df = _load_diff_csv(base_csv, base_name, output_name)
    mid_df = _load_diff_csv(mid_csv, mid_name, output_name)
    target_df = mid_df if mid_csv == target_csv else _load_diff_csv(target_csv, target_name, output_name)
    if base_df is None or mid_df is None or target_df is None:
        return None
    ps_df = _load_diff_csv(ps_csv, _value_name_from_path(ps_csv), output_name) if ps_csv else None

    base_df = base_df.sort_values('ts').reset_index(drop=True)
    mid_df = mid_df.sort_values('ts').reset_index(drop=True)
    target_df = target_df.sort_values('ts').reset_index(drop=True)
    if ps_df is not None:
        ps_df = ps_df.sort_values('ts').reset_index(drop=True)

    base_ts_arr, base_val_arr = base_df['ts'].values, base_df['value'].values
    base_date_arr, base_time_arr, base_pid_arr = base_df['date'].values, base_df['time'].values, base_df['pid'].values
    mid_ts_arr, mid_val_arr = mid_df['ts'].values, mid_df['value'].values
    target_ts_arr, target_val_arr = target_df['ts'].values, target_df['value'].values
    target_ps_arr = target_df['ps'].values.astype(str)
    ps_ts_arr = ps_df['ts'].values if ps_df is not None else None
    ps_val_arr = ps_df['value'].values if ps_df is not None else None

    role_ts_arrays = [base_ts_arr, mid_ts_arr, target_ts_arr]
    _role_map = {'value_base': 0, 'value_mid': 1, 'value_target': 2}

    out_dates, out_times, out_pids, out_ts = [], [], [], []
    out_diff_vals, out_ps = [], []
    matched_count = skipped_no_mid = skipped_no_target = skipped_interval = 0

    def _resolve_pair(base_idx, mid_idx, target_idx):
        """对一组 (base, mid, target) 计算差值与 ps"""
        role_values = (base_val_arr[base_idx], mid_val_arr[mid_idx], target_val_arr[target_idx])
        if diff_role_b is None:
            # passthrough：直接输出 roleA 原始值，ps 参考 roleA 自身 ts
            idx = _role_map.get(diff_role_a, 2)
            ref_ts = role_ts_arrays[idx][base_idx if idx == 0 else (mid_idx if idx == 1 else target_idx)]
            return role_values[idx], _resolve_ps_fast(target_ps_arr[target_idx], ps_ts_arr, ps_val_arr, ref_ts)
        a_idx = _role_map.get(diff_role_a, 2)
        b_idx = _role_map.get(diff_role_b, 0)
        diff_value = role_values[a_idx] - role_values[b_idx]
        return diff_value, _resolve_ps_fast(target_ps_arr[target_idx], ps_ts_arr, ps_val_arr, target_ts_arr[target_idx])

    if direction == 'backward':
        j = k = 0
        mid_len, target_len = len(mid_ts_arr), len(target_ts_arr)
        for i in range(len(base_ts_arr)):
            base_ts = base_ts_arr[i]
            while j < mid_len and mid_ts_arr[j] < base_ts:
                j += 1
            if j >= mid_len:
                skipped_no_mid += len(base_ts_arr) - i
                break
            mid_ts = mid_ts_arr[j]
            while k < target_len and target_ts_arr[k] < mid_ts:
                k += 1
            if k >= target_len:
                skipped_no_target += 1
                continue
            if max_interval_ms != -1 and abs(target_ts_arr[k] - base_ts) * 1000 > max_interval_ms:
                skipped_interval += 1
                continue
            diff_value, ps_final = _resolve_pair(i, j, k)
            out_dates.append(base_date_arr[i]); out_times.append(base_time_arr[i])
            out_pids.append(base_pid_arr[i]); out_ts.append(base_ts)
            out_diff_vals.append(diff_value); out_ps.append(ps_final)
            matched_count += 1
    else:
        j = len(mid_ts_arr) - 1
        k = len(target_ts_arr) - 1
        for i in range(len(base_ts_arr) - 1, -1, -1):
            base_ts = base_ts_arr[i]
            while j >= 0 and mid_ts_arr[j] > base_ts:
                j -= 1
            if j < 0:
                skipped_no_mid += i + 1
                break
            mid_ts = mid_ts_arr[j]
            while k >= 0 and target_ts_arr[k] > mid_ts:
                k -= 1
            if k < 0:
                skipped_no_target += 1
                continue
            if max_interval_ms != -1 and abs(target_ts_arr[k] - base_ts) * 1000 > max_interval_ms:
                skipped_interval += 1
                continue
            diff_value, ps_final = _resolve_pair(i, j, k)
            out_dates.append(base_date_arr[i]); out_times.append(base_time_arr[i])
            out_pids.append(base_pid_arr[i]); out_ts.append(base_ts)
            out_diff_vals.append(diff_value); out_ps.append(ps_final)
            matched_count += 1
        out_dates.reverse(); out_times.reverse(); out_pids.reverse()
        out_ts.reverse(); out_diff_vals.reverse(); out_ps.reverse()

    logger.warning(
        f'❗{output_name}[{direction}]: 匹配 {matched_count} 条，'
        f'{mid_name} 无匹配 {skipped_no_mid} 条，'
        f'{target_name} 无匹配 {skipped_no_target} 条，'
        f'超时间窗口 {skipped_interval} 条'
    )
    if not out_dates:
        return None
    return _write_diff_result(output_csv, output_name,
                              out_dates, out_times, out_pids, out_ts, out_diff_vals, out_ps)


def _resolve_ps_fast(raw_ps, ps_ts_arr, ps_val_arr, ref_ts):
    """ps 解析：行内优先，否则用 searchsorted 二分查找最近一条 ps_ts <= ref_ts

    Args:
        raw_ps (str): target 行内已携带的 ps 值，空/'nan' 触发外部查找
        ps_ts_arr (np.ndarray | None): ps 变量的 ts 升序数组；None 表示无外部 ps 源
        ps_val_arr (np.ndarray | None): ps 变量的 value 数组，与 ps_ts_arr 等长
        ref_ts (float): 参考时间戳，用于在 ps_ts_arr 中定位最近一条

    Returns:
        str: 解析得到的 ps 字符串；无来源时返回空串
    """
    ps_str = str(raw_ps).strip()
    if ps_str and ps_str.lower() != 'nan':
        return ps_str
    if ps_ts_arr is not None:
        idx = np.searchsorted(ps_ts_arr, ref_ts, side='right') - 1
        if idx >= 0:
            ps_val_raw = ps_val_arr[idx]
            try:
                return str(int(ps_val_raw))
            except (ValueError, TypeError):
                return str(ps_val_raw).strip()
    return ''


def _value_name_from_path(path):
    """从 CSV 路径取变量名

    Args:
        path (str | None): CSV 文件路径，文件名形如 'N+value_name.csv'

    Returns:
        str: 文件名最后一个 '+' 之后的 stem；无 '+' 时返回完整 stem；path 为空返回 '(unknown)'
    """
    if not path:
        return '(unknown)'
    stem = Path(path).stem
    return stem.split('+', 1)[-1] if '+' in stem else stem


def _load_diff_csv(path, var_name, output_name, numeric_value=True):
    """读取差值计算用的 CSV，拆分 value_ps → value/ps，按需数值化

    CSV 列约定：date time pid ts value_ps（空格分隔无表头），
    value_ps 形如 '123' 或 '123,ps_xxx'。

    Args:
        path (str): CSV 文件路径
        var_name (str): 变量名，仅用于日志标识
        output_name (str): 输出变量名，仅用于日志标识
        numeric_value (bool): True 时 value 列转 float 并丢弃 NaN；False 时保留为字符串

    Returns:
        DataFrame | None: 含 date/time/pid/ts/value/ps 列的 DataFrame；
                          文件不存在/为空/读取异常时返回 None
    """
    if not path or not os.path.exists(path):
        logger.warning(f'❗{output_name}: {var_name} 文件不存在: {path}')
        return None
    if os.path.getsize(path) == 0:
        logger.warning(f'❗{output_name}: {var_name} 文件为空: {path}')
        return None
    try:
        df = pd.read_csv(path, header=None, sep=r'\s+', names=_CSV_COL_NAMES)
        if df.empty:
            logger.warning(f'❗{output_name}: {var_name} 读取后为空: {path}')
            return None
        split_cols = df['value_ps'].fillna('').astype(str).str.split(',', n=1, expand=True)
        if split_cols.shape[1] == 1:
            split_cols[1] = ''
        split_cols.columns = ['value', 'ps']
        df[['value', 'ps']] = split_cols
        df['ts'] = pd.to_numeric(df['ts'], errors='coerce')
        if numeric_value:
            df['value'] = pd.to_numeric(df['value'], errors='coerce')
            df = df.dropna(subset=['ts', 'value']).reset_index(drop=True)
        else:
            df['value'] = df['value'].astype(str)
            df = df.dropna(subset=['ts']).reset_index(drop=True)
        return df
    except Exception as e:
        logger.exception(f'❗{output_name}: 读取 {var_name} 失败: {e}')
        return None


def _format_value_ps(diff_vals, ps_vals):
    """向量化格式化 value_ps：整数去 .0，按需拼接 ',ps'

    Args:
        diff_vals (list | np.ndarray): 差值数值列表
        ps_vals (list | np.ndarray): ps 字符串列表，与 diff_vals 等长

    Returns:
        np.ndarray: 形如 '123' 或 '123,ps_xxx' 的字符串数组
    """
    diff_arr = np.array(diff_vals)
    is_int_mask = (diff_arr == np.floor(diff_arr))
    diff_strs = np.where(is_int_mask,
                         diff_arr.astype(np.int64).astype(str),
                         diff_arr.astype(str))
    ps_arr = np.array(ps_vals).astype(str)
    has_ps = (ps_arr != '') & (np.char.lower(ps_arr) != 'nan')
    return np.where(has_ps,
                    np.char.add(np.char.add(diff_strs, ','), ps_arr),
                    diff_strs)


def _write_diff_result(output_csv, output_name, dates, times, pids, ts_list, diff_vals, ps_vals):
    """把差值结果写回 CSV（空格分隔，无表头）

    Args:
        output_csv (str): 输出 CSV 路径
        output_name (str): 变量名，仅用于日志标识
        dates (list): 输出行 date 列
        times (list): 输出行 time 列
        pids (list): 输出行 pid 列
        ts_list (list): 输出行 ts 列
        diff_vals (list): 差值数值列表
        ps_vals (list): ps 字符串列表

    Returns:
        DataFrame | None: 写入成功返回结果 DataFrame；失败返回 None
    """
    result_df = pd.DataFrame({
        'date':     dates,
        'time':     times,
        'pid':      pids,
        'ts':       ts_list,
        'value_ps': _format_value_ps(diff_vals, ps_vals),
    })
    try:
        result_df.to_csv(output_csv, sep=' ', header=False, index=False)
    except Exception as e:
        logger.exception(f'❗{output_name}: 写入输出文件失败: {e}')
        return None
    return result_df



def _pair_role_key_value(key_df, value_df, key_value, output_name, role_tag):
    """角色 key/value 配对：以 value 为锚，按 key_value 方向给每个 value 配最近的 key

    key_before   → 向前找最近 key_ts <= value_ts
    value_before → 向后找最近 key_ts >= value_ts

    Args:
        key_df (DataFrame): 该角色 key 变量 DataFrame，含 ts/value 列
        value_df (DataFrame): 该角色 value 变量 DataFrame，含 ts/value/date/time/pid 列
        key_value (str): 'key_before' 或 'value_before'
        output_name (str): 输出变量名，仅用于日志标识
        role_tag (str): 角色标签，仅用于日志标识

    Returns:
        list[tuple]: 每项形如 (key_value_str, value_num, value_ts, date, time, pid)，
                     保持 value 时间序；无配对时返回空列表
    """
    if key_df is None or value_df is None:
        return []
    key_df = key_df.sort_values('ts').reset_index(drop=True)
    value_df = value_df.sort_values('ts').reset_index(drop=True)

    key_ts_arr = key_df['ts'].values
    key_val_arr = key_df['value'].values.astype(str)
    value_ts_arr = value_df['ts'].values
    value_val_arr = value_df['value'].values
    value_date_arr = value_df['date'].values
    value_time_arr = value_df['time'].values
    value_pid_arr = value_df['pid'].values
    key_len = len(key_ts_arr)

    pairs = []
    j = 0
    if key_value == 'key_before':
        for i in range(len(value_ts_arr)):
            value_ts = value_ts_arr[i]
            while j < key_len and key_ts_arr[j] <= value_ts:
                j += 1
            if j == 0:
                continue
            pairs.append((key_val_arr[j - 1], value_val_arr[i], value_ts,
                          value_date_arr[i], value_time_arr[i], value_pid_arr[i]))
    else:
        for i in range(len(value_ts_arr)):
            value_ts = value_ts_arr[i]
            while j < key_len and key_ts_arr[j] < value_ts:
                j += 1
            if j >= key_len:
                break
            pairs.append((key_val_arr[j], value_val_arr[i], value_ts,
                          value_date_arr[i], value_time_arr[i], value_pid_arr[i]))

    if not pairs:
        logger.warning(f'❗{output_name}[{role_tag}/{key_value}]: key/value 无配对')
    return pairs


def get_multi_log_diff_key_value(
    output_csv,
    role1_key_csv, role1_value_csv, role1_key_value,
    role2_key_csv, role2_value_csv, role2_key_value,
    ps_csv, max_interval_ms, diff_role_a, diff_role_b
):
    """按 key 值匹配两个角色，计算 diff_role_a.value - diff_role_b.value 差值

    流程：
      1. 每个角色按 key_value 方向给 value 配最近 key
      2. 角色内去重：相同 key 值只保留 value_ts 最早的一条
      3. 角色间按 key 值相等匹配，计算差值
      4. max_interval 过滤：|value_1_ts - value_2_ts|*1000 > max_interval_ms 时丢弃
      5. ps：以 value_1_ts 为参考，searchsorted 取最近 ps_ts <= value_1_ts

    输出行取自 role1 value 的 date/time/pid/ts。

    Args:
        output_csv (str): 输出 CSV 路径，空格分隔无表头
        role1_key_csv (str): 角色1 key 变量 CSV 路径
        role1_value_csv (str): 角色1 value 变量 CSV 路径
        role1_key_value (str): 角色1 配对方向，'key_before' 或 'value_before'
        role2_key_csv (str): 角色2 key 变量 CSV 路径
        role2_value_csv (str): 角色2 value 变量 CSV 路径
        role2_key_value (str): 角色2 配对方向，'key_before' 或 'value_before'
        ps_csv (str | None): ps 标志位变量 CSV 路径，None 表示不解析 ps
        max_interval_ms (int): 两角色 value_ts 差上限（毫秒），-1 表示不限
        diff_role_a (str): 差值被减数角色，'value_1' 或 'value_2'
        diff_role_b (str): 差值减数角色，'value_1' 或 'value_2'

    Returns:
        DataFrame | None: 写入成功返回结果 DataFrame；任一输入缺失/无匹配返回 None
    """
    output_name = _value_name_from_path(output_csv)

    role1_key_df = _load_diff_csv(role1_key_csv, _value_name_from_path(role1_key_csv), output_name, numeric_value=False)
    role1_value_df = _load_diff_csv(role1_value_csv, _value_name_from_path(role1_value_csv), output_name)
    role2_key_df = _load_diff_csv(role2_key_csv, _value_name_from_path(role2_key_csv), output_name, numeric_value=False)
    role2_value_df = _load_diff_csv(role2_value_csv, _value_name_from_path(role2_value_csv), output_name)
    if any(df is None for df in (role1_key_df, role1_value_df, role2_key_df, role2_value_df)):
        return None
    ps_df = _load_diff_csv(ps_csv, _value_name_from_path(ps_csv), output_name) if ps_csv else None

    pairs1 = _pair_role_key_value(role1_key_df, role1_value_df, role1_key_value, output_name, 'role1')
    pairs2 = _pair_role_key_value(role2_key_df, role2_value_df, role2_key_value, output_name, 'role2')

    def _dedup_earliest(pairs):
        dedup = {}
        for item in pairs:
            key_str = item[0]
            if key_str not in dedup or item[2] < dedup[key_str][2]:
                dedup[key_str] = item
        return dedup

    map1 = _dedup_earliest(pairs1)
    map2 = _dedup_earliest(pairs2)

    if ps_df is not None:
        ps_df = ps_df.sort_values('ts').reset_index(drop=True)
        ps_ts_arr = ps_df['ts'].values
        ps_val_arr = ps_df['value'].values
    else:
        ps_ts_arr = ps_val_arr = None

    out_dates, out_times, out_pids, out_ts = [], [], [], []
    out_diff_vals, out_ps = [], []
    matched_count = skipped_no_match = skipped_interval = 0
    _role_map = {'value_1': 0, 'value_2': 1}

    for key_str, item1 in map1.items():
        item2 = map2.get(key_str)
        if item2 is None:
            skipped_no_match += 1
            continue
        v1, ts1 = item1[1], item1[2]
        v2, ts2 = item2[1], item2[2]
        if max_interval_ms != -1 and abs(ts1 - ts2) * 1000 > max_interval_ms:
            skipped_interval += 1
            continue
        role_values = (v1, v2)
        diff_value = role_values[_role_map[diff_role_a]] - role_values[_role_map[diff_role_b]]
        ps_final = _resolve_ps_fast('', ps_ts_arr, ps_val_arr, ts1)
        out_dates.append(item1[3]); out_times.append(item1[4])
        out_pids.append(item1[5]); out_ts.append(ts1)
        out_diff_vals.append(diff_value); out_ps.append(ps_final)
        matched_count += 1

    logger.warning(
        f'❗{output_name}[key_value]: 匹配 {matched_count} 条，'
        f'role2 无相同 key {skipped_no_match} 条，'
        f'超时间窗口 {skipped_interval} 条'
    )
    if not out_dates:
        return None
    result_df = _write_diff_result(output_csv, output_name,
                                   out_dates, out_times, out_pids, out_ts, out_diff_vals, out_ps)

    # 额外生成带表头的调试明细表 {统计变量名}_tmp.csv（_tmp 子目录，不参与绘图统计）
    try:
        tmp_dir = os.path.join(os.path.dirname(output_csv), '_tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, output_name + '_tmp.csv')
        tmp_rows = []
        for key_str, item1 in map1.items():
            item2 = map2.get(key_str)
            if item2 is None:
                continue
            v1, ts1 = item1[1], item1[2]
            v2, ts2 = item2[1], item2[2]
            if max_interval_ms != -1 and abs(ts1 - ts2) * 1000 > max_interval_ms:
                continue
            role_values = (v1, v2)
            diff_value = role_values[_role_map[diff_role_a]] - role_values[_role_map[diff_role_b]]
            ps_final = _resolve_ps_fast('', ps_ts_arr, ps_val_arr, ts1)
            tmp_rows.append({
                'key': key_str,
                'value1_date': item1[3], 'value1_time': item1[4], 'value1_pid': item1[5],
                'value1_ts': ts1, 'value1': v1,
                'value2_ts': ts2, 'value2': v2,
                'diff': diff_value, 'ps': ps_final,
            })
        if tmp_rows:
            pd.DataFrame(tmp_rows).to_csv(tmp_path, sep=',', header=True, index=False, encoding='utf-8-sig')
            logger.warning(f'❗{output_name}: 明细表已写入 {tmp_path}')
    except Exception as e:
        logger.exception(f'❗{output_name}: 写入明细表失败: {e}')

    return result_df


# ==================== mawk 前置 grep 预过滤 ====================

# 预过滤命中率兜底阈值：过滤后体积占原始比例高于此值时退回全量扫描。
# 收益条件推导（N=复用该目录的 theme 数，M=单 theme 全量 mawk 耗时，G=grep 耗时）：
#   不预过滤 N×M  >  预过滤 G + N×M×命中率   =>   命中率 < 1 - G/(N×M)
# 实测 grep 比带程序的 mawk 快数倍（5.5GB/119模式 grep 6.9s vs 单 theme mawk 13-33s），
# 故即使 N=1 阈值也在 0.83 以上（lidar_container 实测 0.83、vla_arch 0.85、
# FSDA_Thor N=19 时 0.98）。取 0.8 为统一下界：真实目录命中率均低于 30%，
# 该阈值只在 log_tag 几乎匹配所有行（预过滤退化为多写一遍原文件）时才触发。
_PREFILTER_MAX_HIT_RATIO = 0.8


def collect_log_tags(var_lists):
    """从 var_lists 中收集本次统计需要的全部 log_tag（去重）

    log_tag 直接作为 awk 的 /.../ 正则字面量使用，也是 grep -E 的模式来源。
    任一 log_tag 为空串时，该空模式会匹配所有行导致预过滤失效，
    此时返回 None 通知调用方放弃预过滤、退回全量扫描。

    Args:
        var_lists (list): get_theme 返回的 [[log_tag, [(func, input, ...), ...]], ...]

    Returns:
        list[str] | None: 去重后的 log_tag 列表；含空 log_tag 或无有效 tag 时返回 None
    """
    try:
        tags = []
        seen = set()
        for var in var_lists:
            tag = var[0]
            if not isinstance(tag, str) or not tag.strip():
                # 空 log_tag 会让 grep 匹配全部行，预过滤失去意义
                return None
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags or None
    except Exception as e:
        logger.exception(f'collect_log_tags: 收集 log_tag 失败: {e}')
        return None


def prefilter_logs_by_tags(log_path_list, log_tags, filtered_file_path):
    """用一次 grep -E 把命中任意 log_tag 的行抽取到单个中间文件

    各 theme 的 mawk 随后只扫这个小文件，避免同一批日志被 N 个 theme 反复全量扫描。
    grep 按传入文件顺序输出且不改写行内容，因此原始时序与 awk 语义完全保持。

    失败、无命中、命中率过高时均返回 None，由调用方退回全量扫描，保证不丢数据。

    Args:
        log_path_list (list[str]): 原始日志文件路径列表（顺序即 mawk 的读取顺序）
        log_tags (list[str]): 需要匹配的 log_tag 列表，作为 grep -E 的模式
        filtered_file_path (str): 过滤结果输出路径，须位于可写目录（不能在 perf/ 只读硬链接区）

    Returns:
        str | None: 成功时返回 filtered_file_path；应退回全量扫描时返回 None
    """
    if not log_path_list or not log_tags:
        return None

    pattern_file_path = None
    try:
        os.makedirs(os.path.dirname(filtered_file_path), exist_ok=True)

        # 模式写入临时文件，避免超长命令行与引号转义问题
        with tempfile.NamedTemporaryFile(
            'w', suffix='.pattern', delete=False, encoding='utf-8'
        ) as pattern_file:
            pattern_file.write('\n'.join(log_tags))
            pattern_file_path = pattern_file.name

        original_total_size = 0
        for single_log_path in log_path_list:
            try:
                original_total_size += os.path.getsize(single_log_path)
            except OSError:
                pass

        with open(filtered_file_path, 'wb') as filtered_file:
            completed = subprocess.run(
                ['grep', '-E', '-h', '-f', pattern_file_path] + list(log_path_list),
                stdout=filtered_file,
                stderr=subprocess.PIPE,
            )

        # grep 退出码：0=有命中，1=无命中（合法），>=2=真正出错
        if completed.returncode >= 2:
            logger.warning(
                f'预过滤 grep 执行异常 returncode={completed.returncode}，退回全量扫描'
                f'\nstderr: {completed.stderr.decode("utf-8", "ignore")[:500]}'
            )
            return None

        filtered_size = os.path.getsize(filtered_file_path)
        if filtered_size == 0:
            # 无任何命中：该 log_dir 下确实没有目标日志行，仍可让 mawk 扫空文件，
            # 结果与全量扫描一致（都产出空 CSV），且更快。
            logger.warning('预过滤无命中，mawk 将扫描空文件')
            return filtered_file_path

        if original_total_size > 0:
            hit_ratio = filtered_size / original_total_size
            if hit_ratio > _PREFILTER_MAX_HIT_RATIO:
                logger.warning(
                    f'预过滤命中率过高 {hit_ratio:.1%}（>{_PREFILTER_MAX_HIT_RATIO:.0%}），'
                    f'收益不足，退回全量扫描'
                )
                return None
            logger.warning(
                f'🔍 预过滤完成 {original_total_size / 1048576:.0f}MB → '
                f'{filtered_size / 1048576:.1f}MB（{hit_ratio:.2%}），'
                f'文件数 {len(log_path_list)} → 1'
            )
        return filtered_file_path
    except FileNotFoundError as e:
        # 环境缺少 grep：退回全量扫描，不影响功能
        logger.warning(f'预过滤不可用（缺少 grep），退回全量扫描: {e}')
        return None
    except Exception as e:
        logger.exception(f'预过滤失败，退回全量扫描: {e}')
        return None
    finally:
        if pattern_file_path:
            try:
                os.remove(pattern_file_path)
            except OSError:
                pass


def txt_process_tool(var_lists, save_root, log_path, theme, time_range):
    """
    使用 mawk 处理日志文本，提取性能数据

    这是一个复杂的日志处理函数，使用 mawk (更快的 awk 实现) 来解析日志文件
    并提取各种性能指标数据到 CSV 文件。

    Args:
        var_lists: 变量列表，格式为 [[匹配模式, [[类型, 数据, 保存名称], ...]], ...]
        save_root: CSV文件保存根目录
        log_path: 日志文件路径列表
        theme: 主题名称（用于特殊处理）
        time_range: 时间范围元组 (start_timestamp, end_timestamp)

    Returns:
        tuple: (csv_path, csv_path_count_sec, csv_path_count_min, csv_path_diff,
                csv_path_multi_log_diff_value, csv_path_multi_log_diff_key_value)
            csv_path_multi_log_diff_value: list of dict，每项包含：
                output_csv, value_base, value_after, value_target, ps, max_interval
            csv_path_multi_log_diff_key_value: list of dict，每项包含：
                output_csv, role1, role2, ps, max_interval, diff_role_a, diff_role_b
    """

    csv_path = []
    csv_path_count_sec = []
    csv_path_count_min = []
    csv_path_diff = []
    # multi_log_diff_value 不走 awk，记录待后处理的配置项
    csv_path_multi_log_diff_value = []
    # multi_log_diff_key_value 不走 awk，记录待后处理的配置项
    csv_path_multi_log_diff_key_value = []

    if not log_path:
        return csv_path, csv_path_count_sec, csv_path_count_min, csv_path_diff, csv_path_multi_log_diff_value, csv_path_multi_log_diff_key_value
    _context = ''
    gi = generator_increase()
    _mktime = _get_mktime_expr(theme)

    def ge_save(x):
        _n = x if '+' in x else f'{next(gi)}+{x}'
        return join(save_root, _n + '.csv')

    for var in var_lists:
        var_log_tag, var_fuc_input_name = var[0], var[1]
        tr1 = """
            liky = "{0}"
            if($0 ~ /{0}/){{
                %s
            }} ;
        """.format(var_log_tag)
        cac = []
        for var_func, var_input, var_name, calculate_type, _ps_type in var_fuc_input_name:

            # multi_log_diff_value：不生成 awk 代码，仅记录后处理配置
            if var_func == 'multi_log_diff_value':
                try:
                    diff_params = _parse_diff_value_input(var_input)
                except ValueError as e:
                    logger.warning(f'txt_process_tool: 解析 multi_log_diff_value 参数失败: {e}')
                    continue
                output_csv = ge_save(var_name)
                Path(output_csv).touch()
                csv_path.append(output_csv)
                # 将解析结果原样存入，供后处理时调用；同时保留 calculate_type 以便二次处理
                entry = {'output_csv': output_csv, 'calculate_type': calculate_type}
                entry.update(diff_params)
                csv_path_multi_log_diff_value.append(entry)
                continue  # 跳过 awk 代码生成

            # multi_log_diff_key_value：不生成 awk 代码，仅记录后处理配置
            if var_func == 'multi_log_diff_key_value':
                try:
                    kv_params = _parse_diff_key_value_input(var_input)
                except ValueError as e:
                    logger.warning(f'txt_process_tool: 解析 multi_log_diff_key_value 参数失败: {e}')
                    continue
                output_csv = ge_save(var_name)
                Path(output_csv).touch()
                csv_path.append(output_csv)
                entry = {'output_csv': output_csv, 'calculate_type': calculate_type}
                entry.update(kv_params)
                csv_path_multi_log_diff_key_value.append(entry)
                continue  # 跳过 awk 代码生成

            var_name = ge_save(var_name)
            Path(var_name).touch()
            csv_path.append(var_name)
            if calculate_type == "count_sec":
                csv_path_count_sec.append(var_name)
            elif calculate_type == "count_min":
                csv_path_count_min.append(var_name)
            elif calculate_type == "value_diff":
                csv_path_diff.append(var_name)

            _cac = _build_common_awk_cac(var_func, var_input, var_log_tag, var_name, _mktime)
            if _cac is None:
                tmpl = _build_awk_cac_template(var_name, _mktime)
                if var_func == 'bevparking':
                    _cac = tmpl % 'var = "" key_next(liky) "," find_perception_state();'
                elif var_func == 'emb_type':
                    _cac = tmpl % 'split($9,car_config,",")\n    var = classify_brake(car_config[8], car_config[11]);'
                else:
                    logger.warning(f'txt_process_tool: 未知 var_func 类型: {var_func!r}')
                    continue
            cac.append(_cac)
        if cac:
            _context += tr1 % ''.join(cac)
    nye = year_from_range(time_range)
    awk_program = _build_awk_program(_context, nye)
    # 用 subprocess 替代 os.system：列表参数自动处理路径转义，
    # 能拿到 returncode/stderr。
    # mawk 通过 -f 从临时文件读取程序，避免引号嵌套；数据文件作为位置参数传入。
    # 实测 mawk 对 `mawk - <files>`（程序从 stdin 读）的处理有问题：会把后续参数
    # 当成程序而非文件名，导致静默无输出。改用 -f programfile 是最稳妥的方式。
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.awk', delete=False, encoding='utf-8'
        ) as prog_file:
            prog_file.write(awk_program)
            prog_path = prog_file.name
        proc = subprocess.run(
            ['mawk', '-f', prog_path] + list(log_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            logger.error(
                f'txt_process_tool: mawk 执行失败 returncode={proc.returncode}'
                f'\nstderr: {proc.stderr}'
            )
    except FileNotFoundError:
        logger.error('txt_process_tool: 未找到 mawk 可执行文件，请确认已安装 mawk')
    except Exception as e:
        logger.exception(f'txt_process_tool: 调用 mawk 发生未预期错误: {e}')
    finally:
        try:
            os.unlink(prog_path)
        except (OSError, NameError):
            pass
    return (
        list(filter(lambda x: Path(x).exists(), csv_path)),
        list(filter(lambda x: Path(x).exists(), csv_path_count_sec)),
        list(filter(lambda x: Path(x).exists(), csv_path_count_min)),
        list(filter(lambda x: Path(x).exists(), csv_path_diff)),
        csv_path_multi_log_diff_value,
        csv_path_multi_log_diff_key_value,
    )



def head_tail_index(_x, head=2, tail=2):
    """获取数据变化点的头尾索引

    用于绘图时在数值变化边界处保留前后若干行，便于观察跳变。

    Args:
        _x (pd.Series): 数据 Series，可能含 NaN
        head (int): 每个变化点头部保留行数
        tail (int): 每个变化点尾部保留行数

    Returns:
        list: 变化点附近 head+tail 行的索引列表
    """
    _x2 = _x.fillna('!')
    xi = _x2.loc[_x2 != _x2.shift(-1)].index
    xi = xi - tail + 1
    xl = []
    for i in range(head + tail):
        xl.extend((xi + i).to_list())
    return xl


CN_TZ = pytz.timezone("Asia/Shanghai")
_TS_RE = re.compile(r"(\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})(?=\D|$)")


def _parse_cn_dt_from_log_path(log_path) -> datetime:
    """从文件名提取 YYYY_MM_DD_HH_MM_SS 时间戳并解析为中国时区时间

    Args:
        log_path (Path | str): 日志文件路径，取其 name 部分匹配时间戳

    Returns:
        datetime | None: 解析成功返回中国时区（Asia/Shanghai）的 datetime；
                         文件名无时间戳返回 None
    """
    name = getattr(log_path, "name", None) or str(log_path)
    matches = list(_TS_RE.finditer(name))
    if not matches:
        return None
    dt = datetime.strptime(matches[-1].group(1), "%Y_%m_%d_%H_%M_%S")
    return CN_TZ.localize(dt)


def should_keep_log(log_path, time_range, state: dict) -> bool:
    """判断 log_path 是否需要保留（把所有判断过程封装在这里）

    规则：
    - 文件名无时间：保留
    - 文件名时间 < start：丢弃
    - 文件名时间 > end：第一个超过 end 的保留；第二个及之后丢弃

    Args:
        log_path (Path | str): 日志文件路径
        time_range (tuple | None): (start_ts, end_ts) 的 epoch seconds（中国时区解释），None 表示不过滤
        state (dict): 跨文件复用的状态字典，需在循环外创建并传入，
                      用于记录 'kept_first_after_end' 标志位

    Returns:
        bool: True 保留该文件，False 跳过该文件
    """
    if not time_range:
        return True

    start_ts, end_ts = time_range
    start_dt = datetime.fromtimestamp(start_ts, tz=CN_TZ)
    end_dt = datetime.fromtimestamp(end_ts, tz=CN_TZ)

    file_dt = _parse_cn_dt_from_log_path(log_path)
    if file_dt is None:
        return True

    if file_dt < start_dt:
        return False

    if file_dt > end_dt:
        if state is None:
            state = {}
        if not state.get("kept_first_after_end", False):
            state["kept_first_after_end"] = True
            return True
        return False

    return True

