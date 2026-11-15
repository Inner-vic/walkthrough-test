"""
Common Module Wrapper (Parameterized Platform Version)

此文件整合了所有从 common 模块引入的功能，包括：
- perf_log: 代码日志
- perf_path: 性能日志路径处理
- perf_utils: 日志处理工具
- perf_plot: 绘图上传
- perf_csv: CSV 生成与后处理
- perf_value: 飞书在线配置读取与变量最新统计时间回写
- feishu_client: 飞书文档交互

使用方式：
    from common_wrapper import format_period, make_dirs, txt_process_tool
    from common_wrapper import Plot_Singal, FeishuTemplateReport, RecordPerformance
"""
import os
import sys
from os.path import dirname

# 入口模块仍可能以 `from common_wrapper import *` 形式使用，
# 保证项目根目录在 sys.path 中（与历史行为一致）。
_project_root = dirname(dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from .perf_value import load_config, get_theme_lists, load_config_from_feishu, get_calculate_type_map, get_emb_type_value_name
from .perf_plot import Plot_Singal, FeishuTemplateReport
from .perf_csv import RecordPerformance
from .perf_utils import (
    format_period, year_from_range,
    make_dirs, rm_duplicate_csv, read_csv_file, txt_process_tool,
    head_tail_index
)

from . import perf_path
from .perf_path import (
    set_source_log_path, get_source_log_path,
    check_time_range, is_valid_time_string,
    clean_dir, find_time_strings_in_directory,
    modify_ini_preserve_comments_and_case,
    set_dir_and_time_from_path, set_dir_and_time_from_args,
    extract_vin_from_log_dir, find_path_by_keyword,
    extract_and_move_logs, extract_file, is_file_time_in_range,
    resolve_clp_binary_path, build_clp_missing_message,
)
from .perf_log import logger, dev_flag
from .feishu_client import Client, LarkException
