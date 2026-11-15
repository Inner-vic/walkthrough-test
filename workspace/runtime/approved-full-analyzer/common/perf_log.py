import logging
import logging.handlers
import multiprocessing
import os
import socket
import time
from os.path import join
from pathlib import Path
import warnings


warnings.filterwarnings("ignore")
dev_flag = socket.gethostname() != 'john-ThinkPad-L14-Gen-1'
formatted_time = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())

_log_dir = Path(os.path.dirname(__file__)).parent / 'log'
_log_dir.mkdir(exist_ok=True)
log_filename = str(_log_dir / f"log-{formatted_time}.log")

# 日志保留份数：仅保留最新 5 份
_MAX_LOG_FILES = 5


def _rotate_logs(log_dir, max_logs=_MAX_LOG_FILES):
    """保留最新的 max_logs 个日志文件，超出时删除最旧的

    在创建当次新日志前调用，保留 max_logs - 1 个历史文件，加上即将新建的 1 个，合计 max_logs 个。
    """
    existing = sorted(Path(log_dir).glob("log-*.log"), key=lambda p: p.stat().st_mtime)
    to_delete = existing[:max(0, len(existing) - (max_logs - 1))]
    for f in to_delete:
        try:
            f.unlink()
        except OSError:
            pass

report_flag = []

def set_log():
    _logger = logging.getLogger('branch_logger')
    _logger.propagate = False
    _logger.setLevel(logging.INFO)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter()
    stream_handler.setFormatter(formatter)
    _logger.addHandler(stream_handler)
    # 只在主进程写日志文件，避免 spawn 子进程重入时创建空文件
    if multiprocessing.current_process().name == 'MainProcess':
        _rotate_logs(str(_log_dir))
        # 用 TimedRotatingFileHandler 按天切分，保留 14 天，避免长期运行日志被快速覆盖。
        # atTime 设为凌晨 3 点切分，减少白天运行时的切分干扰。
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_filename,
            when='midnight',
            interval=1,
            backupCount=_MAX_LOG_FILES,
            encoding='utf-8',
        )
        file_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)
        try:
            version_file = join(os.path.dirname(__file__), 'version')
            if os.path.exists(version_file):
                with open(version_file, 'r', encoding='utf-8') as f:
                    _logger.info(f.read())

            config_file = join(os.path.dirname(__file__), 'o.ini')
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    _logger.info(f.read())
        except (IOError, OSError) as e:
            _logger.warning(f'无法读取版本或配置文件: {e}')
        except Exception as e:
            _logger.exception(f'读取文件时发生未预期错误: {e}')
    return _logger


logger = set_log()