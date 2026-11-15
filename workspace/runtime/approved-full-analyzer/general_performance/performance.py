import os
from os.path import dirname, abspath,join
import sys
from datetime import datetime
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import configparser
from config import CONFIG_PATH, CONFIG_MAPS
# 引入通用函数
sys.path.insert(0, dirname(dirname(abspath(__file__))))
from common.common_wrapper import *


def log_process(_log_path, _theme, _time, _config_map, _platform=None, _vin=None, _package_version=None):
    try:
        # theme_lists = _config_map[_theme].get('theme_lists')
        path_config = CONFIG_PATH
        # 读取在线配置
        value_config_url = _config_map[_theme].get('value_config_url')
        config_data = load_config_from_feishu(value_config_url[0], None) # 统计方式
        theme_lists = get_theme_lists(config_data) # 统计主题列表

        def _fmt_time(_x):
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_x[0])) + ' -> ' + time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(_x[1]))

        if _time is None or _time == [None]:
            _time_str = 'all'
        else:
            _time_str = _fmt_time(_time)
        logger.warning(f"""
logPath: {_log_path}
theme_lists:{theme_lists}
time: {_time_str}
path_config: {path_config}
value_config_url: {value_config_url}
""")
        # 处理日志 => csv
        log_start_time = datetime.now()
        rop = RecordPerformance(_log_path, theme_lists,
                                _time, path_config, config_data)
        rop.grep_log()

        # csv => jpg 并上传
        config_file = join(os.path.dirname(__file__), 'o.ini')
        config = configparser.ConfigParser()
        config.read(config_file)
        plot_start_time = datetime.now()

        plot_theme = Plot_Singal(
            rop.result_path, _time, _theme, config, _config_map, config_data, None,
            platform=_platform, vin=_vin, package_version=_package_version)
        result = plot_theme.summary_report()
        logger.warning(f"🖥🚐 性能报告{_theme}主题，统计时间：{_time_str}")
        logger.warning(f"📜💾 {_theme}处理日志{plot_start_time.strftime('%Y-%m-%d %H:%M:%S')}结束，耗时:{(plot_start_time - log_start_time).total_seconds():.2f} 秒")
        logger.warning(f"📊📤 {_theme}绘制、上传{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}结束，耗时:{(datetime.now() - plot_start_time).total_seconds():.2f} 秒")
        # 返回: (报告结果, 本次有效变量集合, 配置表 token)
        # 用于 main 末尾回写"最新统计时间"
        return result, getattr(rop, 'active_value_names', {}), value_config_url[0]
    except AssertionError as e:
        raise e
    except Exception as e:
        logger.error(e, exc_info=True)
        return


# 多线程数量设置
multi_theme_workers = 16


def main(platform: str):
    CONFIG_MAP = CONFIG_MAPS[platform]

    config_file = os.path.join(os.path.dirname(__file__), 'o.ini')
    assert os.path.isfile(config_file), '请检查配置文件保存路径是否正确'
    config = configparser.ConfigParser()
    config.read(config_file)

    try:
        log_path = config['PARAMETER']['logPath']
        _time = config['PARAMETER']['time']
        _theme = config['PARAMETER']['theme']
    except KeyError:
        logger.warning('请检查配置文件是否完整.')
        return

    assert os.path.isdir(log_path), '请检查输入的[日志路径].'

    if _time:
        _time = list(map(format_period, _time.split(',')))
        assert _time, '请检查输入的[时间段].'
    else:
        _time = [None]

    try:
        _theme = list(filter(lambda x: x in CONFIG_MAP.keys() or x.replace('_comp', '') in CONFIG_MAP.keys(),
                             re.findall(r'[A-Za-z0-9_]+', _theme)))
    except (TypeError, AttributeError) as e:
        logger.error(f'解析theme失败: {e}')
        _theme = None
    except Exception as e:
        logger.exception(f'解析theme发生未预期错误: {e}')
        _theme = None
    assert _theme, '请检查输入的[theme].'

    start_time = datetime.now()
    theme_tasks = []

    # 从 o.ini 读取 VIN（由 start.py 写入），用于报告命名；复用上面已读的 config
    vin = None
    try:
        if config.has_option('PARAMETER', 'vin'):
            vin = config.get('PARAMETER', 'vin').strip() or None
    except Exception as e:
        logger.exception(f'读取 o.ini 中的 vin 失败: {e}')
    if not vin:
        vin = 'VIN'
        logger.warning(f'未从 o.ini 读取到 VIN，报告命名兜底为 "{vin}"')

    # 从 o.ini 读取大包版本（由 start.py 写入），用于报告汇总与命名
    package_version = None
    try:
        if config.has_option('PARAMETER', 'package_version'):
            package_version = config.get('PARAMETER', 'package_version').strip() or None
    except Exception as e:
        logger.exception(f'读取 o.ini 中的 package_version 失败: {e}')
    if not package_version:
        package_version = '未知'
        logger.warning(f'未从 o.ini 读取到大包版本，兜底为 "{package_version}"')

    with ThreadPoolExecutor(max_workers=multi_theme_workers) as executor:  # 根据需要调整最大线程数
        for i in _theme:
            for t in _time:
                theme_tasks.append(executor.submit(
                    log_process, log_path, i, t, CONFIG_MAP, platform, vin, package_version))
        for future in as_completed(theme_tasks):
            future.result()

    logger.warning(f"📎 主题处理{multi_theme_workers}线程{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}结束，总耗时: {(datetime.now() - start_time).total_seconds():.2f} 秒")

    # ================ 回写"最新统计时间"到配置表 ================
    # 同一平台共用同一张配置表，合并本次所有 theme 的有效变量后一次性回写
    try:
        from common.perf_value import (
            update_last_valid_time_to_feishu, _parse_date_from_time_range
        )
        merged_active = set()
        config_token = None
        for future in theme_tasks:
            try:
                res = future.result()
                # log_process 正常返回: (result, active_value_names, token)
                if isinstance(res, tuple) and len(res) == 3:
                    _, active_map, token = res
                    if token:
                        config_token = token
                    if active_map:
                        for names in active_map.values():
                            merged_active |= set(names)
            except Exception as e:
                logger.exception(f'收集 active_value_names 失败: {e}')

        if config_token and merged_active:
            stat_date = _parse_date_from_time_range(_time)
            logger.warning(f"📝 回写最新统计时间: 变量数={len(merged_active)}, stat_date={stat_date}")
            update_last_valid_time_to_feishu(config_token, merged_active, stat_date)
        else:
            logger.warning("📝 无需回写最新统计时间（无配置表 token 或无有效变量）")
    except Exception as e:
        logger.exception(f'回写最新统计时间失败: {e}')

    # ================ 上传perf过程日志 ================
    # if not dev_flag:
    #     config_file = join(os.path.dirname(__file__), 'o.ini')
    #     config = configparser.ConfigParser()
    #     config.read(config_file)
    #     FeishuTemplateReport(config, None).upload_run_log_files()


