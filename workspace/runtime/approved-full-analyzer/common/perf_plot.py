"""
计算统计csv数据
1. 绘图
2. 上传到报告

提取日期: 2026-02-09
来源文件: thor_performance.py, orin_x_performance.py
"""
import requests
import glob
import re
import os
import subprocess
import zipfile
import time
import socket
import random
from datetime import datetime
import seaborn as sns
import pandas as pd
import numpy as np
import matplotlib.style as mplstyle
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib as mpl
from os.path import join
from pathlib import Path
from multiprocessing import Pool, cpu_count

from .perf_value import *
from .feishu_client import Client, LarkException, one_client
from .perf_log import logger, report_flag, dev_flag, log_filename
from . import perf_path
from .perf_utils import (
    cut_line, head_tail_index, get_unit_by_title, processing_error,
    parse_sheet_coordinate, img_to_stream, lttb_downsample, adaptive_linewidth,
    calc_figsize_by_timespan,
)
from .perf_value import get_ps_type_map, get_calculate_type_map, get_emb_type_value_name


# 飞书云文档标题长度上限为 256 字符，留出余量避免复制文件接口报错
FEISHU_TITLE_MAX_LENGTH = 250

EMPTY_DATA_REASON_LABELS = {
    'source_log_not_found': '未找到源日志文件',
    'log_tag_not_matched': '日志标签未命中',
    'outside_time_range': '统计时间范围内无数据',
    'invalid_value': '解析后无有效数值',
    'threshold_filtered': '阈值过滤后无数据',
    'outlier_filtered': '异常值过滤后无数据',
    'dependency_data_missing': '依赖变量数据不足',
    'stat_file_not_generated': '统计文件未生成，请检查配置或依赖',
    'plot_failed': '绘图失败',
    'image_upload_failed': '图片上传失败',
}


def format_empty_data_diagnostic(reason, raw_count=None, range_count=None, data_range=None):
    prefix = '图片异常' if reason in ('plot_failed', 'image_upload_failed') else '无数据'
    lines = [f"{prefix}：{EMPTY_DATA_REASON_LABELS[reason]}"]
    counts = []
    if raw_count is not None:
        counts.append(f"原始 {raw_count} 条")
    if range_count is not None:
        counts.append(f"时间范围内 {range_count} 条")
    if counts:
        lines.append('，'.join(counts))
    if data_range:
        lines.append(f"数据范围：{data_range}")
    return '\n'.join(lines)


# 代码版本 commit 信息缓存，避免每次生成报告都调用一次 git
_commit_info_cache = None


def get_commit_info():
    """获取当前代码版本的 git 短 commit id 与提交时间，用于报告标题追踪版本

    结果会缓存，进程内仅调用一次 git；获取失败时返回 'unknown'，绝不抛异常。

    Returns:
        str: 形如 '2026-07-31 6753225' 的版本信息；失败返回 'unknown'
    """
    global _commit_info_cache
    if _commit_info_cache is not None:
        return _commit_info_cache
    try:
        result = subprocess.run(
            ['git', 'show', '-s', '--format=%cd %h',
             '--date=format:%Y-%m-%d', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            _commit_info_cache = result.stdout.strip()
        else:
            logger.warning(f"❗获取 commit 信息失败: {result.stderr.strip()}")
            _commit_info_cache = 'unknown'
    except Exception as e:
        logger.warning(f"❗获取 commit 信息异常: {e}")
        _commit_info_cache = 'unknown'
    return _commit_info_cache


class FeishuTemplateReport:
    def __init__(self, config, token=None, calculate_type_map=None,
                 emb_type_value_name=None, emb_type_value=None,
                 package_version=None):
        """初始化飞书报告生成器

        Args:
            config (dict): 配置字典，需含 PARAMETER 段
            token (tuple | None): 模板 (sheet_token, sheet_id)，None 时按需生成
            calculate_type_map (dict | None): 变量名→calculate_type 映射
            emb_type_value_name (str | None): emb_type 变量名，用于过滤上报
            emb_type_value (str | None): 预提取的 emb_type 值，None 时从统计结果读取
            package_version (str | None): 大包版本，填充模板占位符 &version；
                None 时兜底为 "未知"
        """
        self.client = one_client
        self.access_token = None
        self.template_token = token
        self.key_char = '&'
        self.split_char = '@'
        self.config = config
        self.calculate_type_map = calculate_type_map or {}
        self.emb_type_value_name = emb_type_value_name
        self.emb_type_value = emb_type_value
        self.package_version = package_version or '未知'

    def _should_skip_emb_type_variable(self, key1, data):
        """判断 calculate_type=emb_type 的变量是否应跳过上报

        规则：仅当 emb_type 统计值为"自研EMB"时上报，否则跳过。
        emb_type 变量本身不受此规则影响，始终上报。

        Args:
            key1 (str): 变量名
            data (dict): _format_report_data 的输出，含各变量统计字段

        Returns:
            bool: True 跳过该变量，False 保留
        """
        if self.calculate_type_map.get(key1) != 'emb_type':
            return False
        if key1 == self.emb_type_value_name:
            return False

        # 获取 emb_type 分类值：优先用预提取值，回退到格式化数据
        emb_type_str = self._get_emb_type_str(data)
        if emb_type_str != '自研EMB':
            logger.warning(f'emb_type={emb_type_str}，非自研EMB，跳过变量: {key1}')
            return True
        return False

    def _get_emb_type_str(self, data):
        """获取 emb_type 的分类值

        Args:
            data (dict): _format_report_data 的输出

        Returns:
            str: emb_type 分类值；找不到时返回空字符串
        """
        if self.emb_type_value is not None:
            return str(self.emb_type_value)
        if not self.emb_type_value_name:
            return ''
        emb_type_field = data.get(self.emb_type_value_name)
        if emb_type_field is None:
            return ''
        return str(emb_type_field.get('mean', ''))

    def _format_report_data(self, _d):
        """从统计结果中提取每个变量的统计字段和图片路径

        Args:
            _d (dict): 原始统计结果，键为 Path 或 str（title），值为含 sta_df/img_path 的字典

        Returns:
            dict: {变量名: {统计字段..., 'image': img_path}}；
                  str 键原样保留；全空 sta_df 的变量跳过
        """
        _rd = {}
        for i, j in _d.items():
            if i and isinstance(i, str):
                _rd.update({i: j})
                continue
            key1 = i.stem.split('+')[1]
            diagnostic = j.get('diagnostic')
            if key1 in _rd.keys() or (j['sta_df'].isnull().all().all() and not diagnostic):
                continue
            field = {}
            for column in j['sta_df'].columns:
                value = j['sta_df'][column].iloc[0]
                field[column] = '-' if pd.isna(value) else value
            field['image'] = j.get('img_path')
            if diagnostic:
                field['diagnostic'] = diagnostic
            _rd.update({key1: field})
        return _rd

    def theme_report(self, data, title):
        """生成单主题飞书报告

        Args:
            data (dict): 单主题统计结果
            title (str): 报告标题

        Returns:
            str | None: 飞书云文档链接；失败返回 None
        """
        self.access_token = self.client.get_tenant_access_token()
        if self.template_token is None:
            self.template_token = self.generate_template(data)
            logger.warning('new template ', self.template_token)

        rd = self._format_report_data(data)
        rd = {k: v for k, v in rd.items(
        ) if not self._should_skip_emb_type_variable(k, rd)}
        rd['title'] = title
        rd['version'] = self.package_version
        tb, st = self.template_token
        return self.generate_report(tb, st, rd, title)

    def summary_report(self, data, title):
        """汇总多 theme 的统计结果后生成报告

        先把各 theme 数据展平再复用 theme 的格式化逻辑。

        Args:
            data (dict): {theme_name: 单主题统计结果}
            title (str): 报告标题

        Returns:
            str | None: 飞书云文档链接；失败返回 None
        """
        flattened = {}
        for k, v in data.items():
            flattened.update(v)
        rd = self._format_report_data(flattened)
        rd = {k: v for k, v in rd.items(
        ) if not self._should_skip_emb_type_variable(k, rd)}
        rd['title'] = title
        rd['version'] = self.package_version
        self.access_token = self.client.get_tenant_access_token()
        tb, st = self.template_token
        return self.generate_report(tb, st, rd, title)

    def generate_report(self, sheet_token, sheet_id, data, title, read_range='!B:O'):
        """基于模板生成飞书云文档报告

        Args:
            sheet_token (str): 模板表格 token
            sheet_id (str): 模板 sheet id
            data (dict): _format_report_data 输出的变量统计字典
            title (str): 报告标题
            read_range (str): 读取模板数据的范围，默认 '!B:O'。
                必须包含 B 列：模板中 &title 占位符位于 B1(合并 B1:C1)，
                范围不含 B 列时 &title 不会被替换。

        Returns:
            str | None: 新文档链接；失败返回 None
        """
        try:
            newfile = self.client.copy_file(
                self.access_token, self.template_token[0], title)
            self.client.update_permission(self.access_token, newfile['token'])
            sheet_info = self.client.get_sheets_info(
                self.access_token, newfile['token'])
            for sheet_x in sheet_info:
                sheet_id = sheet_x['sheetId']
                keymap = self.client.get_values_batch(
                    self.access_token, sheet_token, sheet_id + read_range)
                range_text, range_image = self.update_values(
                    data, keymap, sheet_id)
                # 多个区间合并为一次批量写入，减少 HTTP 往返
                if range_text:
                    value_ranges = [{"range": rg, "values": vals}
                                    for rg, vals in range_text.items()]
                    self.client.update_values_batch(
                        self.access_token, newfile['token'], value_ranges)
                self.client.batch_set_cell_style(
                    self.access_token, newfile['token'], sheet_id + read_range)

                def insert_image(x):
                    from requests.exceptions import RequestException

                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            self.client.insert_image(
                                self.access_token, newfile['token'], x)
                            logger.info(f"图片上传成功: {x[2]}")
                            return True
                        except (RequestException, IOError, LarkException) as e:
                            if attempt < max_retries - 1:
                                logger.warning(
                                    f"图片上传失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                            else:
                                logger.error(
                                    f"图片上传失败，已重试 {max_retries} 次: {e}")
                        except Exception as e:
                            logger.exception(f"图片上传发生未预期错误: {e}")
                            break
                    return False

                failed_images = [image for image in range_image if not insert_image(image)]
                if failed_images:
                    try:
                        diagnostic = format_empty_data_diagnostic('image_upload_failed')
                        failed_ranges = [
                            {"range": image[0], "values": [[diagnostic]]}
                            for image in failed_images
                        ]
                        self.client.update_values_batch(
                            self.access_token, newfile['token'], failed_ranges)
                    except Exception as e:
                        logger.exception(f"回写图片上传失败诊断发生异常: {e}")
            logger.warning('🚩 云文档链接地址 %s' % newfile['url'])
            self.upload_fsd_log_files(newfile['token'])
            if dev_flag:
                # try:
                #     subprocess.Popen(['xdg-open', newfile['url']], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                # except:
                #     logger.info(
                #         '浏览器打开报告失败。',
                #         exc_info=True)
                pass

            # 在 orin_report_path.txt 中保存飞书云文档地址
            save_directory = perf_path.get_source_log_path()
            file_name = 'general_report_path.txt'
            # 确保目录存在
            os.makedirs(save_directory, exist_ok=True)
            # 完整文件路径
            file_path = os.path.join(save_directory, file_name)
            with open(file_path, 'w') as file:
                file.write(newfile['url'])
                logger.warning(f"保存飞书云文档链接{file_path}")

            return newfile['url']
        except Exception as e:
            logger.exception(
                "飞书云文档生成失败，网络异常，建议重新运行脚本。")

    def update_values(self, rd, keymap, nsh_id):
        """根据模板单元格占位符填充数据并拆分文本/图片更新范围

        Args:
            rd (dict): _format_report_data 输出的变量统计字典
            keymap (dict): 飞书 get_values_batch 返回的模板单元格数据
            nsh_id (str): 目标 sheet id

        Returns:
            tuple: (range_text, range_image)
                range_text: {range_str: 填充后的二维列表}
                range_image: [(img_range, img_stream, img_name), ...]
        """
        def _out(data):
            def _inner2(keys):
                if not keys:
                    return ''
                key, *next_keys = keys
                if key and ('=' == key[0]):
                    return key[1:] + _inner2(next_keys)
                else:
                    t = '-'
                    for _i, _k in enumerate(key.split(self.split_char)):
                        if _i == 0:
                            if _k not in data:
                                break
                            t = data[_k]
                        else:
                            if _k not in t:
                                break
                            t = t[_k]
                    return str(t) + _inner2(next_keys)

            return _inner2

        mapdata = _out(rd)
        rg_text, rg_img = {}, []
        for cell_value in keymap['valueRanges']:
            cl_img = []
            sh_id, (x1, x2), (x3, x4) = parse_sheet_coordinate(
                cell_value['range'])
            for _r, row in enumerate(cell_value['values']):
                for _c, j in enumerate(row):
                    if isinstance(j, str) and j[0] == self.key_char:
                        placeholder_keys = j.split(self.key_char)[1:]
                        nv = mapdata(placeholder_keys)
                        if self.split_char + 'image' in j:
                            value_name = placeholder_keys[0].split(self.split_char)[0]
                            diagnostic = rd.get(value_name, {}).get('diagnostic')
                            if diagnostic:
                                row[_c] = diagnostic
                                continue
                            row[_c] = None
                            if (not nv) or (nv in ('-', 'None')):
                                continue
                            cor = (nsh_id, (x1 + _c, x2 + _r),
                                   (x1 + _c, x2 + _r))
                            img_rg = parse_sheet_coordinate(cor)
                            cl_img.append(
                                (img_rg, img_to_stream(nv), Path(nv).name))
                        else:
                            row[_c] = nv if nv else '-'
                    if str(j).startswith('='):
                        row[_c] = {"type": "formula", "text": j}
            nrg = nsh_id + "!" + cell_value['range'].split('!')[1]
            rg_text.update({nrg: cell_value['values']})
            rg_img.extend(cl_img)
        return rg_text, rg_img

    def generate_template(self, data):
        """按统计字段生成新的飞书模板表格

        Args:
            data (dict): _format_report_data 输出的变量统计字典，用于生成字段卡片

        Returns:
            tuple: (token, sheet_id) 新模板的表格 token 与首个 sheet id
        """
        def merge_write(crange, txt):
            a, (b, c), (d, e) = crange
            ri = [(a, (b, c), (b, c)), crange]
            rgs = list(map(parse_sheet_coordinate, ri))
            logger.warning(rgs)
            self.client.update_values(
                self.access_token, token, rgs[0], [[txt]])
            self.client.merge_cells(self.access_token, token, rgs[1])

        def create_filed_card(filed):
            filed_name = filed[0].stem.split('+')[-1]
            km = filed[1]['sta_df']
            columns = km.columns.to_list()
            vals = np.empty([len(columns), 2], dtype='<U200')
            vals[:, 0] = columns
            vals[:, 1] = [f'&{filed_name}@{i}' for i in columns]
            nonlocal start_cell
            c, r = start_cell
            er, ec = r + len(columns) - 1, c + 2
            rg = parse_sheet_coordinate((sheetid, (c + 1, r), (ec, er)))
            self.client.update_values(
                self.access_token, token, rg, vals.tolist())
            merge_write((sheetid, (c, r), (c, er)), km.iloc[0].name)
            merge_write((sheetid, (ec + 1, r), (ec + 1, er)),
                        f'&{filed_name}@image')
            start_cell = (c, er + 1)

        folder_token = ''
        token, url = self.client.create_spreadsheet(
            self.access_token, folder_token, 'title')
        sheetid = self.client.get_first_sheetid(self.access_token, token)
        oa, (ob, oc), (od, oe) = parse_sheet_coordinate(sheetid + '!A1:A1')
        self.client.update_permission(self.access_token, token)
        merge_write((oa, (ob, oc), (od + 1, oc)), "对比软件版本")
        merge_write((oa, (ob + 2, oc), (od + 4, oc)), "&title")
        merge_write((oa, (ob + 5, oc), (od + 7, oc)), "&version")
        self.client.sheets_batch_update(self.access_token, token, sheetid)
        start_cell = (ob + 1, oc + 1)
        list(map(create_filed_card, data.items()))
        _c, _r = start_cell
        merge_write((sheetid, (ob, oc + 1), (ob, _r - 1)), "perf性能")
        return token, sheetid

    def upload_fsd_log_files(self, param):
        """上传 FSD 日志文件到飞书云文档

        仅当 config['PARAMETER']['upload'] == '1' 时执行，把 FSD*.zip 与
        x01-orin*_GPU.log 打包后上传，并在文档中新增"日志文件" sheet 记录链接。

        Args:
            param (str): 飞书云文档 token
        """
        try:
            upload_flag = self.config['PARAMETER']['upload'] == '1'
            if upload_flag:
                logger.warning('正在上传日志文件...')
                log_path = self.config['PARAMETER']['logPath']
                logger.warning(f"上传日志路径: {log_path}")
                file_list = (list(glob.glob(join(log_path, 'FSD*.zip'))) + list(
                    glob.glob(join(log_path, 'x01-orin*_GPU.log'))))
                zip_file_name = '/tmp/log.zip'
                with zipfile.ZipFile(zip_file_name, 'w') as zipf:
                    for file in file_list:
                        zipf.write(file, os.path.basename(file))
                dx = self.client.upload_file_to_drive(
                    self.access_token, zip_file_name)
                _i = len(self.client.get_sheets_info(self.access_token, param))
                data = self.client.add_work_sheet(
                    self.access_token, param, '日志文件', _i)
                crange = data['replies'][0]['addSheet']['properties']['sheetId'] + '!A1:B1'
                f_url = 'https://li.feishu.cn/file/' + dx['file_token']
                self.client.update_values(
                    self.access_token, param, crange, [["log", f_url]])
                self.client.update_permission(
                    self.access_token, dx['file_token'], 'file')
                logger.warning('日志文件上传成功')
        except (IOError, OSError, LarkException) as e:
            logger.error(f"日志文件上传失败: {e}")
        except Exception as e:
            logger.exception(f"日志文件上传发生未预期错误: {e}")

    def upload_run_log_files(self):
        """dev 模式下上传运行日志到飞书云盘

        文件名格式：[theme]-[report_flag]-[hostname].log
        仅在 dev_flag 为真时执行。
        """
        try:
            fp = os.path.abspath(log_filename)
            nod = ''
            _theme = self.config['PARAMETER']['theme']
            t3 = re.sub(r'[^a-zA-Z0-9]', '_', _theme)
            t2 = socket.gethostname()
            t4 = ','.join(report_flag)
            fnx = f'[{t3}]-[{t4}]-[{t2}].log'
            if dev_flag:
                if self.access_token is None:
                    self.access_token = self.client.get_tenant_access_token()
                self.client.upload_file_to_drive(
                    self.access_token, fp, nod, fnx)
        except (IOError, OSError, LarkException) as e:
            logger.warning(f"运行日志上传失败: {e}")
        except Exception as e:
            logger.exception(f"运行日志上传发生未预期错误: {e}")


# ===================== 统计绘图 ======================
mpl.use('Agg')  # 满足多线程,绘制不显示
sns.set(style="darkgrid")
plt.rcParams['agg.path.chunksize'] = 10000
mplstyle.use('fast')
mpl.rcParams['path.simplify'] = True
mpl.rcParams['path.simplify_threshold'] = 1.0


def plot_worker(args):
    """多进程绘图任务工作函数

    在子进程中重建 Plot_Singal 并执行 get_data，避免主进程 matplotlib 状态污染。

    Args:
        args (tuple): (theme_name, data_path, not_plot_flag, date_of_data,
                       time_range, config, config_map, config_date, comparison)

    Returns:
        tuple: (theme_name, statistics_data_comp[0], core_process, emb_type_value)
    """
    theme_name, data_path, not_plot_flag, date_of_data, time_range, config, config_map, config_date, comparison = args
    # 这里重新 import Plot_Singal 类（不能在 __main__ 里定义）
    ps = Plot_Singal(data_path, time_range, theme_name,
                     config, config_map, config_date, comparison)
    ps.not_plot = not_plot_flag
    ps.date_of_data = date_of_data

    # 执行原本的绘图统计逻辑
    ps.get_data()

    # 返回 (主题名, 统计结果, 核心进程标记, emb_type分类值)
    return theme_name, ps.statistics_data_comp[0], ps.core_process, ps.emb_type_value


class Plot_Singal:
    def __init__(self, data_path, time_range, theme, config, config_map, config_date, comparison,
                 platform=None, vin=None, package_version=None):
        """初始化单主题绘图器

        Args:
            data_path (str | dict): 单主题时为 CSV 目录路径；汇总时为 {theme: path} 字典
            time_range (tuple | None): (start_ts, end_ts) 时间范围，None 表示不过滤
            theme (str): 主题名
            config (dict): 全局配置
            config_map (dict): 主题级配置映射，含 perception_state / theme_style 等
            config_date (dict): 变量统计方法配置
            comparison: 对比相关参数
            platform (str | None): 平台类型，用于报告命名（如 m100-ultra）
            vin (str | None): 车辆 VIN，用于报告命名；None 时兜底为 "VIN"
            package_version (str | None): 大包版本，用于报告汇总；None 时兜底为 "未知"
        """
        self.config = config
        self.config_map = config_map
        self.config_date = config_date
        self.ps_map = get_ps_type_map(self.config_date, theme)
        # emb_type 过滤：calculate_type=emb_type 的变量仅在 emb_type="自研EMB" 时上报
        self.calculate_type_map = {}
        for t in config_date:
            self.calculate_type_map.update(
                get_calculate_type_map(config_date, t))
        self.emb_type_value_name = get_emb_type_value_name(config_date)
        self.emb_type_value = None

        self.data_path = data_path
        self.time_range = time_range
        self.theme = theme
        self.platform = platform or theme
        self.vin = vin or 'VIN'
        self.package_version = package_version or '未知'
        self.one_theme_sta_img = None
        self.time_range_comp = time_range
        self.statistics_data_comp = []
        self.not_plot = False
        self.comparison = comparison
        self.image_suffix = '.png'
        plt.rcParams['axes.labelpad'] = 2
        self.date_of_data = time.strftime("%Y%m%d", time.localtime())
        self.core_process = []

    def special_filed(self, value_name):
        """按 ps 类型返回特殊字段标记

        Args:
            value_name (str): 变量名

        Returns:
            int: 1=ps_time（按状态分组）, 2=ps_seq（按序号绘图）,
                 3=ps_xvm（柱状图）, 4=ps_string（分类众数）, 0=普通
        """
        if self.ps_map[value_name] == "ps_time":
            return 1
        if self.ps_map[value_name] == "ps_seq":
            return 2
        if self.ps_map[value_name] == "ps_xvm":
            return 3
        if self.ps_map[value_name] == "ps_string":
            return 4
        return 0

    def plot(self, path, data, csv_file_name):
        """绘制单个变量的统计图并保存为 jpg

        Args:
            path (str): 图片输出目录
            data (pd.DataFrame): 待绘图数据，含 timestamp/pid/value 等列
            csv_file_name (str): CSV 文件名（含序号前缀），用于决定变量名与图片名

        Returns:
            str: 保存后的 jpg 绝对路径
        """
        data_dd = data.copy()
        plt.close('all')
        k1 = data_dd.columns[-1]
        filed_name = csv_file_name.split('+')[-1]

        special = self.special_filed(filed_name)
        if special == 2 and not self.config_map['unicitynoa']['axis_seq_num']:
            special = 0
        if special == 3:
            data_dd['g'] = 0
            # 必须按时间排序，否则不分组直接连线会导致折线来回穿插
            data_dd = data_dd.sort_values('timestamp')
        else:
            if k1.endswith('_log_lines'):
                data_dd = cut_line(data_dd, ['timestamp', 'pid'], delta=120)
            else:
                data_dd = cut_line(data_dd, ['timestamp', 'pid'])

        # 根据时间跨度动态计算画布宽度，保证单位时间对应的横轴长度一致
        if special == 2:
            _fig_w, _fig_h = 18, 9
        else:
            _ts = data_dd['timestamp']
            _span = float(_ts.max() - _ts.min()) if len(_ts) > 1 else 0
            _fig_w, _fig_h = calc_figsize_by_timespan(_span)
        plt.figure(figsize=(_fig_w, _fig_h))
        plt.ticklabel_format(style='plain', axis='y', useOffset=False)
        data_dd['timestamp'] = pd.to_datetime(
            data_dd['timestamp'], unit='s') + pd.Timedelta(hours=8)

        _legend = {}
        try:
            color = plt.rcParams['axes.prop_cycle'].by_key()['color']
            aps = data_dd.pid.unique()
            if special == 1:
                aps = pd.Series(
                    list(self.config_map['perception_state'].values()) + ['unknown'])
            _flag = aps.shape[0] <= len(color)
            col_map = {k: v for k, v in zip(aps, color)}

            # 按全图最大分组点数统一线宽，同一张图内所有线段粗细一致
            _chart_lw = adaptive_linewidth(
                data_dd.groupby('g').size().max() if not data_dd.empty else 0,
                fig_width=_fig_w)

            for i, d in data_dd.groupby('g'):
                d = d.dropna(subset=[k1])
                if d.empty:
                    continue
                now_pid = d.pid.mode()[0]
                col = color[0]
                if _flag:
                    col = col_map.get(now_pid, color[0])
                    _legend[now_pid] = col
                y = d['timestamp']
                if special == 2:
                    y = d['seq']
                if special == 3:
                    # 绘制柱状图
                    # 方法1：使用 plt.bar (width=0.00005 大约是 4 秒的宽度，可根据实际数据密度微调)
                    # plt.bar(y, d[k1], color=col, width=0.00005)

                    # 方法2：如果数据点很密集，强烈建议改用下面这行 vlines，效果更好且不会互相遮挡
                    plt.vlines(y, ymin=0, ymax=d[k1], color=col, linewidth=2)
                else:
                    # 绘制折线图（密集数据自动降采样避免线条过粗）
                    _x, _vy = lttb_downsample(y, d[k1])
                    plt.plot(_x, _vy, color=col, linewidth=_chart_lw)
        except Exception as e:
            logger.error(e, exc_info=True)
        ax = plt.gca()
        if special == 2:
            pass
        else:
            xfmt = mpl.dates.DateFormatter('%H:%M')
            ax.xaxis.set_major_formatter(xfmt)
        if len(_legend.keys()) > 1 or special == 1:
            kl = 'status' if special == 1 else 'pid'
            patches = [mpatches.Patch(color=co, label="{}:{}".format(
                kl, lb)) for lb, co in _legend.items()]
            ax.legend(handles=patches, loc=1)
        plt.ylabel(k1)
        if special == 2:
            plt.xlabel('prediction seq num')
        else:
            plt.xlabel('time(hh:mm)')
        plt.xticks(rotation=0)
        plt.title(filed_name)
        _s = str(join(path, csv_file_name + self.image_suffix))
        plt.savefig(_s, bbox_inches='tight', format=self.image_suffix[1:],
                    pad_inches=0.02, pil_kwargs={"optimize": True})
        return _s

    def plot_two_columns(self, xdata, theme):
        """双 Y 轴绘图：delay 与障碍物数量同图对比

        Args:
            xdata (dict): 两个变量的数据，键形如 (path, (csv1, df1), (csv2, df2))
            theme (str): 主题名（未直接使用，保留接口）

        Returns:
            str: 保存后的 jpg 绝对路径
        """
        (_, (a, b1, c1)), (_, (_, b2, c2)) = list(xdata.items())
        col1, col2 = b1.columns[-1], b2.columns[-1]
        b1[col2] = b2.iloc[:, -1]
        data = cut_line(b1, ['timestamp', 'pid'])
        # 根据时间跨度动态计算画布宽度，保证单位时间对应的横轴长度一致
        _ts = data['timestamp']
        _span = float(_ts.max() - _ts.min()) if len(_ts) > 1 else 0
        _fig_w, _fig_h = calc_figsize_by_timespan(_span)
        data['timestamp'] = pd.to_datetime(
            data['timestamp'], unit='s') + pd.Timedelta(hours=8)
        plt.close('all')
        fig = plt.figure(figsize=(_fig_w, _fig_h))
        ax = fig.add_subplot()
        ax2 = ax.twinx()
        ax.ticklabel_format(style='plain', axis='y', useOffset=False)
        xfmt = mpl.dates.DateFormatter('%H:%M')
        ax.xaxis.set_major_formatter(xfmt)
        ax2.xaxis.set_major_formatter(xfmt)
        xlegend = []

        def _plot(_ax, _data, _col, _legend, _color, s=20, alpha=1, t=1):
            try:
                last_pid = -1
                _color_index = -1
                _gx = data.groupby('g')
                # 按全图最大分组点数统一线宽，同一张图内所有线段粗细一致
                _lw = adaptive_linewidth(
                    _gx.size().max() if len(_gx) > 0 else 0,
                    fig_width=_fig_w)
                for i, d in _gx:
                    now_pid = d.pid.mode()[0]
                    if last_pid != now_pid:
                        _color_index += 1
                        last_pid = now_pid
                    if t == 1:
                        _dx, _dy = lttb_downsample(d['timestamp'], d[_col])
                        _l = _ax.plot(_dx, _dy, '-', color=_color[_color_index], alpha=alpha,
                                      linewidth=_lw, label="{} pid:{}".format(_legend, now_pid))
                    else:
                        _dx, _dy = lttb_downsample(d['timestamp'], d[_col])
                        _l = _ax.plot(_dx, _dy, '-', c=_color[_color_index], alpha=alpha, linewidth=_lw,
                                      label="{} pid:{}".format(_legend, now_pid))
                    _l = _l[0]
                    xlegend.append(_l)
            except Exception as e:
                logger.error(e, exc_info=True)
            return _color_index

        color1 = plt.rcParams['axes.prop_cycle'].by_key()['color'][::2]
        color2 = plt.rcParams['axes.prop_cycle'].by_key()['color'][1::2]
        txt1, txt2 = c1.split('+')[-1], c2.split('+')[-1]
        _plot(ax, data, col1, txt1, color1)
        _plot(ax2, data, col2, txt2, color2, alpha=0.3, t=2, s=1)
        fig.legend(handles=xlegend, loc=1, bbox_to_anchor=(
            1, 1), bbox_transform=ax.transAxes)
        ax.set_xlabel('time(hh:mm)')
        ax.set_ylabel(r"delay(ms)")
        ax2.set_ylabel(r"number of obstacles")
        plt.title(c1.split('+')[-1] + ' & ' + c2.split('+')[-1])
        plt.savefig(join(a, c1 + self.image_suffix), bbox_inches='tight', format=self.image_suffix[1:],
                    pad_inches=0.02, pil_kwargs={"optimize": True})
        return str(join(a, c1 + self.image_suffix))

    def _empty_statistic(self, value_name):
        col_name = get_unit_by_title(value_name)
        return pd.DataFrame(
            {"mean": None, "max": None, "min": None, "P90": None, "P99": None, "HGR": None},
            index=[col_name],
        )

    @staticmethod
    def _data_range_text(data):
        timestamps = pd.to_numeric(data['timestamp'], errors='coerce').dropna()
        if timestamps.empty:
            return None
        start = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamps.min()))
        end = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamps.max()))
        return f'{start} 至 {end}'

    def csv_file_to_statistic(self, csv_file):
        """从单个 CSV 计算统计量并准备绘图数据

        依次完成：ps 拆分、单位/进制转换、时间范围过滤、异常剔除、
        数值统计（mean/max/min/P90/P99/HGR）；ps_string 取众数；
        ps_time 按状态分组拼接。

        Args:
            csv_file (Path): CSV 文件路径

        Returns:
            tuple: (sta_df, data_dd, diagnostic)
                sta_df: 统计结果 DataFrame，索引为带单位的变量名
                data_dd: 过滤清洗后的绘图数据 DataFrame；数据不足时为 None
                diagnostic: 无数据原因文字；正常时为 None
        """
        filed_name = csv_file.stem.split('+')[-1]
        col_name = get_unit_by_title(filed_name)
        special = self.special_filed(filed_name)
        sta_df, data_dd = self._empty_statistic(filed_name), None
        config_info = self.config_date.get(self.theme, {}).get(filed_name, {})
        var_func = config_info.get('func', '')
        empty_reason = 'dependency_data_missing' if var_func.startswith('multi_log_') else 'log_tag_not_matched'

        try:
            if csv_file.stat().st_size == 0:
                return sta_df, None, format_empty_data_diagnostic(empty_reason, raw_count=0)
            columns_names = ['date', 'time', 'pid', 'timestamp']
            if special == 2:
                columns_names = ['date', 'time', 'pid', 'timestamp', 'seq']
            columns_names.append(col_name)
            data = pd.read_csv(csv_file, header=None, sep=' ', names=columns_names)
        except Exception as e:
            logger.exception(f'读取统计文件失败[{filed_name}]: {e}')
            return sta_df, None, format_empty_data_diagnostic('invalid_value')

        raw_count = len(data)
        if data.empty:
            return sta_df, None, format_empty_data_diagnostic(empty_reason, raw_count=0)

        if special == 1 and data.shape[1] > 0:
            last_col = data.columns[-1]
            col_as_str = data[last_col].fillna('').astype(str)
            split_cols = col_as_str.str.split(',', n=1, expand=True)
            if split_cols.shape[1] == 1:
                split_cols[1] = ''
                logger.error(f"❗ {filed_name}异常 请检查log变化")
            data.iloc[:, [-1, 2]] = split_cols.values
            data = data.loc[~data.index.isin(head_tail_index(data['pid']))]

        conversion = {
            'datarec_fps': lambda x: x / 60,
        }
        ms_names = ['prefill_total_time_delay', 'vision_encode_time_delay', 'decode_total_time_delay',
                    'vla_planning_delay', 'e2e-plan_runtime_cost', 'lidar_delay', 'perc_lidar_app_delay']
        if special != 4:
            data.iloc[:, -1] = pd.to_numeric(data.iloc[:, -1], errors='coerce')
            if filed_name in ms_names:
                data.iloc[:, -1] = data.iloc[:, -1] * 1000
            elif filed_name in conversion:
                data.iloc[:, -1] = conversion[filed_name](data.iloc[:, -1])

        data['timestamp'] = pd.to_numeric(data['timestamp'], errors='coerce')
        timestamp_data = data.dropna(subset=['timestamp'])
        data_range = self._data_range_text(timestamp_data)
        if self.time_range:
            range_data = timestamp_data[
                (timestamp_data['timestamp'] >= self.time_range[0])
                & (timestamp_data['timestamp'] <= self.time_range[1])
            ]
        else:
            range_data = timestamp_data
        range_count = len(range_data)
        if self.time_range and range_data.empty:
            diagnostic = format_empty_data_diagnostic(
                'outside_time_range', raw_count=raw_count, range_count=0, data_range=data_range)
            logger.warning(f'[{filed_name}] {diagnostic}')
            return sta_df, None, diagnostic

        if special == 4:
            valid_value_count = range_data[col_name].replace('', np.nan).notna().sum()
        else:
            valid_value_count = pd.to_numeric(range_data.iloc[:, -1], errors='coerce').notna().sum()
        if valid_value_count == 0:
            diagnostic = format_empty_data_diagnostic(
                'invalid_value', raw_count=raw_count, range_count=range_count, data_range=data_range)
            logger.warning(f'[{filed_name}] {diagnostic}')
            return sta_df, None, diagnostic

        max_threshold = min_threshold = None
        if special != 4:
            calculate_type = self.calculate_type_map.get(filed_name, '')
            if calculate_type not in ('count_sec', 'count_min'):
                try:
                    max_threshold, min_threshold = get_thresholds_for_csv(csv_file, self.config_date)
                    if max_threshold is not None or min_threshold is not None:
                        before_threshold = len(data)
                        data = apply_value_thresholds(data, max_threshold, min_threshold)
                        filtered_count = before_threshold - len(data)
                        if filtered_count > 0:
                            logger.warning(
                                f'阈值过滤[{filed_name}]: 过滤 {filtered_count} 条数据'
                                f'（max={max_threshold}, min={min_threshold}）'
                            )
                except Exception as e:
                    logger.warning(f'csv_file_to_statistic: 阈值过滤异常: {e}')

        if self.time_range:
            data = data[
                (data['timestamp'] >= self.time_range[0])
                & (data['timestamp'] <= self.time_range[1])
            ]
        if data.empty:
            reason = 'threshold_filtered' if max_threshold is not None or min_threshold is not None else 'invalid_value'
            diagnostic = format_empty_data_diagnostic(
                reason, raw_count=raw_count, range_count=range_count, data_range=data_range)
            logger.warning(f'[{filed_name}] {diagnostic}')
            return sta_df, None, diagnostic

        if special == 4:
            data = data.dropna(subset=[col_name])
            data.sort_values('timestamp', inplace=True)
            data_dd = data.reset_index(drop=True)
        else:
            data.dropna(inplace=True)
            if data.empty:
                diagnostic = format_empty_data_diagnostic(
                    'invalid_value', raw_count=raw_count, range_count=range_count, data_range=data_range)
                logger.warning(f'[{filed_name}] {diagnostic}')
                return sta_df, None, diagnostic
            data.sort_values('timestamp', inplace=True)
            data_dd = data.reset_index(drop=True)
            data_dd = data_dd.loc[processing_error(data_dd.iloc[:, -1])[1].index]
        if data_dd.empty:
            diagnostic = format_empty_data_diagnostic(
                'outlier_filtered', raw_count=raw_count, range_count=range_count, data_range=data_range)
            logger.warning(f'[{filed_name}] {diagnostic}')
            return sta_df, None, diagnostic

        if special == 4:
            mode_val = data_dd.iloc[:, -1].mode(
            ).iloc[0] if not data_dd.iloc[:, -1].mode().empty else '-'
            sta_df = pd.DataFrame({"mean": mode_val, "max": mode_val, "min": mode_val,
                                   "P90": mode_val, "P99": mode_val, "HGR": '-'},
                                  index=[col_name])
            logger.info(f"ps_string:{sta_df}")
            return sta_df, data_dd, None

        # 只分析最后一列数据，给出均值、最大、最小、90分位、99分位及特有的HGR
        def calculate_statistical(_x):
            last_col = pd.to_numeric(_x.iloc[:, -1], errors="ignore").dropna()
            des = last_col.describe(percentiles=[.90, .99])
            _hgr = np.nan
            if not last_col.empty and '_mem' in filed_name:
                lx1, lx2 = _x.loc[last_col.index[0]
                                  ], _x.loc[last_col.index[-1]]
                t1, t2, v1, v2 = lx1.iloc[3], lx2.iloc[3], lx1.iloc[-1], lx2.iloc[-1]
                _hgr = (v2 - v1) / ((t2 - t1) / 3600)
            if col_name == 'fps':
                des['90%'] = 0.1
                des['99%'] = 0.01
            res = pd.DataFrame({"mean": round(des['mean'], 2), "max": round(des['max'], 2), "min": round(des['min'], 2),
                                "P90": round(des['90%'], 2), "P99": round(des['99%'], 2), "HGR": round(_hgr, 2)},
                               index=[col_name])
            return res
        # special==1时，需要按特定pid分组（对应业务状态），计算每个状态下的统计量，然后拼接为斜杠分隔的合并结果。
        if special == 1:
            mx = self.config_map['perception_state'].copy()
            pst = self.config_map['perception_state_text'][:]
            if self.theme == 'fapa':
                if '0' in mx and '2' in mx:
                    mx['0'], mx['2'] = mx['2'], mx['0']
            flipped_mx = {v: k for k, v in mx.items()}
            kx = list(flipped_mx[i] for i in pst.split('/'))
            mst = list(map(lambda _k: calculate_statistical(
                data_dd.loc[data_dd.pid == _k]), kx))
            mst = [i.fillna('-').astype('str') for i in mst]
            # 拼接多状态结果
            mvl = [i.iloc[0].to_list() for i in mst]
            cn_mvl = ['/'.join(ix) for ix in zip(*mvl)]

            sta_df = mst[0].copy()
            sta_df.iloc[0] = cn_mvl
            ixn = sta_df.iloc[0].name
            sta_df.rename(index={ixn: ixn + '(%s)' % pst}, inplace=True)

            # 分状态的结果
            for _k, df in zip(kx, mst):
                for c in df.columns:
                    # 取该统计项这一行对应列的值
                    sta_df[f"{c}_{_k}"] = df.loc[df.index[0], c]

            data_dd['pid'] = data_dd['pid'].map(lambda x: mx.get(x, 'unknown'))
        else:
            sta_df = calculate_statistical(data_dd)
        return sta_df, data_dd, None

    def get_data_one_theme(self, not_plot=False):
        """处理单主题：遍历所有 CSV → 统计 → 绘图，结果存入 self.one_theme_sta_img

        Args:
            not_plot (bool): True 时只统计不绘图
        """
        one_theme_sta_img = {}
        configured_values = self.config_date.get(self.theme, {})
        csv_f = list(Path(self.data_path).glob('*.csv'))
        csv_f.sort(key=lambda x: float(str(x.name).split('+')[0]))
        csv_by_name = {csv_file.stem.split('+')[-1]: csv_file for csv_file in csv_f}
        all_plot_data = {}
        for filed_name in configured_values:
            csv_file = csv_by_name.get(filed_name)
            if csv_file is None:
                reason = 'source_log_not_found' if not csv_f else 'stat_file_not_generated'
                diagnostic = format_empty_data_diagnostic(reason)
                synthetic_path = Path(self.data_path) / f'{len(csv_f) + len(one_theme_sta_img) + 1}+{filed_name}.csv'
                one_theme_sta_img[synthetic_path] = {
                    'sta_df': self._empty_statistic(filed_name),
                    'diagnostic': diagnostic,
                    'img_path': None,
                }
                logger.warning(f'[{filed_name}] {diagnostic}')
                continue

            perf_data, plot_data, diagnostic = self.csv_file_to_statistic(csv_file)
            one_theme_sta_img[csv_file] = {
                'sta_df': perf_data,
                'diagnostic': diagnostic,
            }
            all_plot_data[csv_file] = plot_data
            if self.emb_type_value_name and self.emb_type_value is None:
                if filed_name == self.emb_type_value_name:
                    try:
                        mean_val = perf_data['mean'].iloc[0]
                        if pd.notna(mean_val):
                            self.emb_type_value = str(mean_val)
                            logger.warning(
                                f'emb_type 统计值: {self.emb_type_value}')
                    except Exception as e:
                        logger.warning(f'提取 emb_type 统计值失败: {e}')
        try:
            for _p, _d in list(all_plot_data.items()):
                if _d is not None and len(_d.timestamp) > 1:
                    self.date_of_data = time.strftime(
                        "%Y%m%d", time.localtime(_d.timestamp.iloc[0]))
                    break
        except (KeyError, AttributeError, ValueError) as e:
            logger.warning(f'提取数据日期失败: {e}')
        except Exception as e:
            logger.exception(f'提取数据日期发生未预期错误: {e}')
        if not not_plot:
            prediction_data = {}
            for csv_file, plot_data in all_plot_data.items():
                if plot_data is None:
                    one_theme_sta_img[csv_file]['img_path'] = None
                    continue
                csv_file_name = csv_file.stem
                if csv_file_name.split('+')[1] in ['prediction_delay', 'prediction_obs']:
                    prediction_data[csv_file] = (
                        self.data_path, plot_data, csv_file_name)
                    continue
                try:
                    one_theme_sta_img[csv_file]['img_path'] = self.plot(
                        self.data_path, plot_data, csv_file_name)
                except Exception as e:
                    logger.exception(f'绘图失败[{csv_file_name}]: {e}')
                    one_theme_sta_img[csv_file]['img_path'] = None
                    one_theme_sta_img[csv_file]['diagnostic'] = format_empty_data_diagnostic('plot_failed')
            prediction_paths = list(prediction_data.keys())
            try:
                if len(prediction_data) == 2:
                    img_path = self.plot_two_columns(prediction_data, self.theme)
                    one_theme_sta_img[prediction_paths[0]]['img_path'] = img_path
                    one_theme_sta_img[prediction_paths[1]]['img_path'] = img_path
                elif len(prediction_data) == 1:
                    img_path = self.plot(*(list(prediction_data.values())[0]))
                    one_theme_sta_img[prediction_paths[0]]['img_path'] = img_path
            except Exception as e:
                logger.exception(f'绘图失败[{self.theme}/prediction]: {e}')
                for prediction_path in prediction_paths:
                    one_theme_sta_img[prediction_path]['img_path'] = None
                    one_theme_sta_img[prediction_path]['diagnostic'] = format_empty_data_diagnostic('plot_failed')
        self.one_theme_sta_img = one_theme_sta_img.copy()

        all_sta = [i['sta_df'] for i in one_theme_sta_img.values()]
        if list(i for i in all_sta if not i.isnull().all(None)):
            all_sta_df = pd.concat(all_sta)
        else:
            all_sta_df = None
        if (all_sta_df is None) or all_sta_df.empty:
            logger.warning(f'日志中没有[{self.theme}]的数据')
            return
        all_sta_df.fillna('-', inplace=True)
        logger.warning(all_sta_df)
        ft = all_sta_df.reset_index()
        m1 = ft['index'].map(lambda x: ('_cpu(%)' in x) or ('_mem(%)' in x))
        m2 = pd.to_numeric(ft['min'], errors='coerce') < 0.3
        m3 = ft['index'].map(lambda x: not (
            x.startswith('orina_') or x.startswith('orinb_')))
        gt = ft.loc[m1 & m2 & m3]
        if not gt.empty:
            cl = gt['index'].to_list()
            mfl = [i.split('(')[0] for i in cl]
            ggt = []
            for fj, d in all_plot_data.items():
                _mf = fj.stem.split('+')[1]
                if (_mf in mfl) and (len(d.iloc[:, 2].unique()) > 1):
                    ggt.append(_mf[:-4])
            self.core_process.extend(ggt)

    def get_data(self):
        """对外入口：统计并绘图，结果存入 self.statistics_data_comp"""
        self.statistics_data_comp = []
        self.get_data_one_theme(self.not_plot)
        self.statistics_data_comp.append(self.one_theme_sta_img)

    def core_process_title(self, title):
        """把 core_process 列表拼接到标题尾部

        Args:
            title (str): 原始标题

        Returns:
            str: 形如 '原标题_core[a, b]' 的标题；无 core_process 时原样返回
        """
        if self.core_process:
            _cp = list(set(self.core_process))
            title = title + '_core[%s]' % ', '.join(_cp)
        # 飞书云文档标题有长度限制，超长则截断，避免 copy_file 接口报错
        if len(title) > FEISHU_TITLE_MAX_LENGTH:
            logger.warning(
                f"❗报告标题超长({len(title)})，截断至 {FEISHU_TITLE_MAX_LENGTH} 字符")
            title = title[:FEISHU_TITLE_MAX_LENGTH]
        return title

    def _format_time_range_for_title(self):
        """把 self.time_range 格式化为标题用的 (开始时间, 结束时间) 字符串

        无时间范围时两个返回值均为 'all'。

        Returns:
            tuple: (start_str, end_str)，格式 'YYYY-MM-DD HH:MM:SS'
        """
        try:
            tr = self.time_range
            if tr is None or tr == [None]:
                return 'all', 'all'
            ranges = tr if isinstance(tr, list) else [tr]
            first = ranges[0]
            last = ranges[-1]
            start_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(first[0]))) \
                if first and first[0] is not None else 'all'
            end_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(last[1]))) \
                if last and last[1] is not None else 'all'
            return start_str, end_str
        except Exception as e:
            logger.exception(f'_format_time_range_for_title: 格式化失败: {e}')
            return 'all', 'all'

    def save_doc(self):
        """生成单主题飞书报告

        Returns:
            str | None: 飞书云文档链接；statistics_data_comp 为空时返回 None
        """
        if self.statistics_data_comp[0] is None:
            return
        theme_style = self.config_map[self.theme].get('theme_style', {})
        theme_title = theme_style.get('title', [])
        idx = theme_title[0] if theme_title else self.theme
        rx = random.randint(100, 999)
        title = '实车测试性能统计_%s_%s_%d' % (
            idx, self.date_of_data, rx)
        title = self.core_process_title(title)
        tpl = self.config_map[self.theme].get('template', None)
        reu = FeishuTemplateReport(
            self.config, tpl,
            calculate_type_map=self.calculate_type_map,
            emb_type_value_name=self.emb_type_value_name,
            emb_type_value=self.emb_type_value,
            package_version=self.package_version
        ).theme_report(
            self.statistics_data_comp[0], title)
        report_flag.append('%s(%d)' % (idx, rx))
        return reu

    def summary_report(self):
        """汇总多主题并生成飞书报告

        绘图部分多进程加速；图片上传部分串行（飞书无批量接口，并行易失败）。

        Returns:
            str | None: 飞书云文档链接
        """
        theme_path = self.data_path.copy()  # 数据路径字典: {theme_name: path}
        theme_data = {}
        not_plot = self.not_plot = self.config_map[self.theme]['not_plot']

        # # ***************** 并行绘图部分 *****************
        logger.warning(f"🚩 plot worker ...")
        tasks = [
            (k, v, not_plot, self.date_of_data,
             self.time_range, self.config, self.config_map, self.config_date, self.comparison)
            for k, v in theme_path.items()]

        max_process = min(len(tasks), cpu_count())
        with Pool(processes=max_process) as pool:
            results = pool.map(plot_worker, tasks)

        for theme, data, cp, emb_val in results:
            theme_data[theme] = data
            self.core_process.extend(cp)
            if emb_val is not None and self.emb_type_value is None:
                self.emb_type_value = emb_val
        # ***************** 上传及生成报告部分 *****************
        logger.warning(f"🚩 upload worker ...")
        token = self.config_map[self.theme].get('template', None)
        assert token, f'There is no template for {self.theme} report.'
        rx = random.randint(100, 999)
        start_str, end_str = self._format_time_range_for_title()
        title = '负载报告(Version %s):fsdlog %s,%s,%s,perf-%s,%d' % (
            get_commit_info(), self.vin, start_str, end_str, self.platform, rx)
        title = self.core_process_title(title)
        reu = FeishuTemplateReport(
            self.config, token,
            calculate_type_map=self.calculate_type_map,
            emb_type_value_name=self.emb_type_value_name,
            emb_type_value=self.emb_type_value,
            package_version=self.package_version
        ).summary_report(theme_data, title)
        report_flag.append('%s(%d)' % (self.theme, rx))

        # 生成报告成功后，把本次参数(VIN/时间/类型/大包版本/链接)追加到月份汇总表
        if reu:
            self.append_report_summary_to_feishu(reu, title)

        return reu

    def append_report_summary_to_feishu(self, report_url, report_title):
        """把本次生成报告的关键参数追加到飞书月份文件夹的汇总在线表格

        汇总表放在与报告归档同源的月份目录(YYYYMM)下，表名
        "负载报告汇总_YYYYMM"。表不存在则首次创建并写表头；已存在则追加一行。

        记录列：生成时间 / VIN / 时间范围 / 类型(平台) / 大包版本 / 在线文档链接。
        其中"在线文档链接"以富文本写入，显示文字为该报告的独立命名(标题)，点击跳转。

        全程异常捕获，失败仅记录日志，绝不影响主报告流程。

        Args:
            report_url (str): 本次生成的飞书云文档链接
            report_title (str): 本次报告的标题，作为链接单元格显示文字
        """
        try:
            from common.secrets_manager import get_secret
            from .feishu_client import one_client

            client = one_client
            access_token = client.get_tenant_access_token()

            # 与报告归档同源：取/建月份目录 YYYYMM
            root_folder_token = get_secret(
                'feishu_folders', 'root_folder_token',
                fallback=''
            )
            month = datetime.now().strftime("%Y%m")
            month_token = client.get_or_create_folder(
                access_token, root_folder_token, month
            )

            # 在月份目录下查找/创建汇总表
            summary_name = f"负载报告汇总_{month}"
            spreadsheet_token = None
            try:
                files = client.list_folder_files(
                    access_token, month_token, filter_type='sheet'
                )
            except Exception as e:
                logger.warning(f"汇总表列举失败 month={month}: {e}")
                files = []

            for f in files:
                if f.get('name') == summary_name:
                    spreadsheet_token = f.get('token')
                    break

            if not spreadsheet_token:
                spreadsheet_token, spreadsheet_url = client.create_spreadsheet(
                    access_token, month_token, summary_name
                )
                logger.warning(f"汇总表不存在，已创建: {summary_name}")
            else:
                spreadsheet_url = f"https://li.feishu.cn/sheets/{spreadsheet_token}"

            # 取首个 sheet
            sheets_info = client.get_sheets_info(access_token, spreadsheet_token)
            sheet_id = sheets_info[0].get('sheetId')

            # 读现有数据，确定首个空行；首行空则先写表头
            read_range = f"{sheet_id}!A:F"
            result = client.get_values_batch(
                access_token, spreadsheet_token, read_range,
                value_render="UnformattedValue"
            )
            rows = []
            if result and 'valueRanges' in result and result['valueRanges']:
                rows = result['valueRanges'][0].get('values', []) or []

            headers = ['生成时间', 'VIN', '时间范围', '类型', '大包版本', '在线文档链接']
            if not rows:
                # 空表：第 1 行写表头，第 2 行写记录
                client.update_values(
                    access_token, spreadsheet_token,
                    f"{sheet_id}!A1:F1", [headers]
                )
                next_row = 2
            else:
                next_row = len(rows) + 1

            start_str, end_str = self._format_time_range_for_title()
            if start_str == 'all' and end_str == 'all':
                time_range_str = 'all'
            else:
                time_range_str = f"{start_str} -> {end_str}"

            # 在线文档链接单元格：富文本，显示文字为该报告独立命名(标题)，点击跳转
            link_cell = {
                "text": report_title,
                "link": report_url,
                "type": "url",
            }
            row_values = [[
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.vin,
                time_range_str,
                self.platform,
                self.package_version,
                link_cell,
            ]]
            client.update_values(
                access_token, spreadsheet_token,
                f"{sheet_id}!A{next_row}:F{next_row}", row_values
            )
            logger.warning(
                f"📑 报告汇总已追加: {summary_name} 第 {next_row} 行 "
                f"[{self.vin} {self.platform} {time_range_str}] {spreadsheet_url}"
            )
        except Exception as e:
            logger.exception(f"追加报告汇总到飞书失败: {e}")
