import os
import sys
import re
import configparser
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from performance import main, set_dir_and_time_from_path, set_dir_and_time_from_args
from common.perf_log import logger
from common import perf_path
from common.perf_path import extract_and_move_logs, extract_vin_from_log_dir


VALID_PLATFORMS = {"m100-ultra", "m100-max", "thor", "orin"}
INTEREST_DIR = {"app", "soc", "OrinX", "log_fsdA", "log_fsdB", "log_xcu_mcu", "log_bzcu_eth"}

# clp 工具缺失时的退出提示：与参数校验同属启动阶段的前置条件检查。
# 缺少 clp 会导致 .clp 日志包全部解压失败，继续运行只会产出缺失大量数据的错误报告，
# 因此在主进程终止（子进程内绝不能退出，会破坏 ProcessPoolExecutor）。
CLP_MISSING_EXIT_MESSAGE = "缺少 clp 解压工具，提前终止程序！！！"


def preprocess_logs_or_exit(root_dir, target_dir, time_range):
    """解压归整日志，若因缺少 clp 工具无法继续则终止程序

    Args:
        root_dir (str): 原始日志根目录
        target_dir (str): 解压归整后的输出目录
        time_range (str | None): 'start/end' 时间范围，None 表示不过滤
    """
    if not extract_and_move_logs(root_dir, INTEREST_DIR, target_dir, time_range):
        sys.exit(CLP_MISSING_EXIT_MESSAGE)


def detect_arg_type(arg: str) -> str:
    """检测单个参数的类型"""
    if re.match(r"^\d{14}/\d{14}$", arg):
        return "time_range"
    if arg.lower() in VALID_PLATFORMS:
        return "platform"
    if arg.startswith(("/", "~", ".")) or os.sep in arg:
        return "path"
    return "unknown"


def parse_args():
    """
    从 sys.argv 解析参数，返回 (path, time_range, platform)
    未传入的参数默认为 None
    """
    path = None
    time_range = None
    platform = None
    for arg in sys.argv[1:]:
        arg = arg.strip()
        arg_type = detect_arg_type(arg)
        if arg_type == "path":
            path = arg
        elif arg_type == "time_range":
            time_range = arg
        elif arg_type == "platform":
            platform = arg.lower()
        else:
            raise ValueError(f"无法识别参数: '{arg}'")
    return path, time_range, platform


if __name__ == '__main__':
    start_time = datetime.now()
    ROOT_DIR, TIME_RANGE, PLATFORM = parse_args()

    preprocessed = False  # path/args 分支内已自行解压归整，末尾不再重复执行
    if PLATFORM is None:
        logger.warning(f"📌 PLATFORM: {PLATFORM} 未提供平台参数")
        logger.warning(f"平台参数可选: {VALID_PLATFORMS}")
        sys.exit("参数无效，提前终止程序！！！")
    elif ROOT_DIR is None:
        logger.warning(f"📌 未提供参数须自行保证o.ini文件和以下参数正确")
        logger.warning(f"原始日志路径root_folder={ROOT_DIR} 路径")
        logger.warning(f"o.ini中logPath={ROOT_DIR}/perf 路径")
        logger.warning(f"o.ini中time=20250728161500 /20250728165500 格式")
        input("######确认o.ini文件已修改，按回车继续######")
        perf_path.set_source_log_path(ROOT_DIR)
        config_file = os.path.join(os.path.dirname(__file__), 'o.ini')
        assert os.path.isfile(config_file), '请检查配置文件保存路径是否正确'
        config = configparser.ConfigParser()
        config.read(config_file)
        TIME_RANGE = config['PARAMETER']['time']
    elif TIME_RANGE is None:
        logger.warning(f"☁️ 处理{PLATFORM}云端log格式")
        logger.warning(f"☁️ 从日志路径{ROOT_DIR}获取时间范围")
        perf_path.set_source_log_path(ROOT_DIR)
        ROOT_DIR = perf_path.get_source_log_path()
        # 先解压归整到 perf/，soc 日志落到此处后再提取 VIN，避免原始目录尚未解压完整导致 VIN 提取落空
        target_dir = '{}/perf'.format(ROOT_DIR)
        preprocess_logs_or_exit(ROOT_DIR, target_dir, TIME_RANGE)
        VIN, PACKAGE_VERSION = extract_vin_from_log_dir(target_dir)
        logger.warning(f"🚗 提取 VIN: {VIN}, 大包版本: {PACKAGE_VERSION}")
        set_dir_and_time_from_path(ROOT_DIR, VIN, PACKAGE_VERSION)
        preprocessed = True
        TIME_RANGE = None
    else:
        logger.warning(f"🚙 处理{PLATFORM}车端log格式")
        perf_path.set_source_log_path(ROOT_DIR)
        if not perf_path.check_time_range(TIME_RANGE):
            logger.warning(f"提供输入有效参数：并重新运行脚本，例如：")
            logger.warning(f"python start.py /home/lixiang/Desktop/test_log/car-m100 20250728161500/20250728165500")
            sys.exit("参数无效，提前终止程序！！！")
        ROOT_DIR = perf_path.get_source_log_path()
        # 先解压归整到 perf/，soc 日志落到此处后再提取 VIN，避免原始目录尚未解压完整导致 VIN 提取落空
        target_dir = '{}/perf'.format(ROOT_DIR)
        preprocess_logs_or_exit(ROOT_DIR, target_dir, TIME_RANGE)
        VIN, PACKAGE_VERSION = extract_vin_from_log_dir(target_dir)
        logger.warning(f"🚗 提取 VIN: {VIN}, 大包版本: {PACKAGE_VERSION}")
        set_dir_and_time_from_args(ROOT_DIR, TIME_RANGE, VIN, PACKAGE_VERSION)
        preprocessed = True

    if not preprocessed:
        preprocess_logs_or_exit(ROOT_DIR, '{}/perf'.format(ROOT_DIR), TIME_RANGE)
    logger.warning(f"🆗 日志解压预处理{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}结束，耗时:{(datetime.now() - start_time).total_seconds():.2f} 秒")

    try:
        main(PLATFORM)
    except KeyboardInterrupt:
        logger.error('程序被强制终止!!!')
