from .perf_log import logger
from .perf_utils import get_dynamic_max_processes
import os
import re
import shutil
import tarfile
import zipfile
import subprocess
from typing import Tuple, Optional
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed


# ======================== 获取统一目录 ========================
source_log_folder = '/XX/LW433B120N1005374_2025-07-10_09_21_00_2025-07-10_10_23_00'  # 本地测试的log保存目录


def set_source_log_path(value):
    """设置全局日志根目录 source_log_folder

    Args:
        value (str): 日志根目录绝对路径
    """
    global source_log_folder
    source_log_folder = value


def get_source_log_path():
    """获取全局日志根目录 source_log_folder

    Returns:
        str: 当前日志根目录路径
    """
    return source_log_folder

# ======================== 检查输入时间范围是否正确 ========================


def _is_rotational_device(path) -> bool:
    """判断 path 所在设备是否为机械硬盘：读 /sys/block/<dev>/queue/rotational，1=HDD，0=SSD；失败保守返回 False

    Args:
        path (str): 任意文件或目录路径，用于定位所在块设备

    Returns:
        bool: True 表示旋转盘（HDD），False 表示固态盘（SSD/NVMe）或判断失败
    """
    try:
        major = os.major(os.stat(path).st_dev)
        for dev_name in os.listdir('/sys/block'):
            dev_file = f'/sys/block/{dev_name}/dev'
            if not os.path.exists(dev_file):
                continue
            with open(dev_file) as f:
                blk_major = int(f.read().strip().split(':')[0])
            if blk_major == major:
                rot_file = f'/sys/block/{dev_name}/queue/rotational'
                with open(rot_file) as f:
                    return f.read().strip() == '1'
    except Exception:
        pass
    return False


def is_valid_time_string(time_str):
    """校验 14 位 YYYYMMDDHHMMSS 数字字符串是否为合法时间

    Args:
        time_str (str): 待校验的时间字符串，如 '20260710123300'

    Returns:
        bool: True 表示格式合法且为有效时间
    """
    if len(time_str) != 14 or not time_str.isdigit():
        return False
    try:
        datetime.strptime(time_str, "%Y%m%d%H%M%S")
        return True
    except ValueError:
        return False


def check_time_range(input_str):
    """校验 'start/end' 格式的时间范围，start < end 才合法

    Args:
        input_str (str): 时间范围字符串，如 '20260710123300/20260710174100'

    Returns:
        bool: True 表示格式合法且 start < end
    """
    if input_str.count('/') != 1:
        return False
    start_str, end_str = input_str.split('/')
    if not (is_valid_time_string(start_str) and is_valid_time_string(end_str)):
        return False
    start = datetime.strptime(start_str, "%Y%m%d%H%M%S")
    end = datetime.strptime(end_str, "%Y%m%d%H%M%S")
    return start < end

# ======================== 处理目录结构，提取log到临时目录 ========================


def clean_dir(target_dir):
    """清理并重建目录；无写权限或文件系统错误时抛出对应异常

    Args:
        target_dir (str): 目标目录路径，存在则先 rmtree 再重建，不存在则直接 makedirs

    Raises:
        PermissionError: 对已存在目录无写权限
        OSError: 文件系统错误
    """
    try:
        if os.path.exists(target_dir):
            if not os.access(target_dir, os.W_OK):
                raise PermissionError(f"没有写权限: {target_dir}")
            shutil.rmtree(target_dir)
            logger.warning(f"✓ 已清理目录: {target_dir}")
        os.makedirs(target_dir, exist_ok=True)
        logger.warning(f"✓ 已创建目录: {target_dir}")
    except PermissionError:
        logger.error(f"❌ 权限错误: 无法访问 {target_dir}")
        raise
    except OSError as e:
        logger.error(f"❌ 文件系统错误: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ 未预期错误: {e}")
        raise


def find_time_strings_in_directory(directory: str) -> Optional[Tuple[str, str]]:
    """从目录字符串中提取起止时间

    目录格式形如：
        /path/to/HLX14B171P0000206_2025-05-06_09_59_00_2025-05-06_11_00_00/

    Args:
        directory (str): 包含起止时间信息的目录路径

    Returns:
        Optional[Tuple[str, str]]: (start, end) 元组，如 ('20250506095900', '20250506110000')；
                                    未匹配返回 None
    """
    pattern = re.compile(
        r".*_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})_(\d{2})"
    )
    match = pattern.search(directory)
    if not match:
        logger.warning("无法从目录字符串中提取时间信息")
        return None

    start_date, sh, sm, ss = match.group(1), match.group(2), match.group(3), match.group(4)
    end_date, eh, em, es = match.group(5), match.group(6), match.group(7), match.group(8)
    start = f"{start_date.replace('-', '')}{sh}{sm}{ss}"
    end = f"{end_date.replace('-', '')}{eh}{em}{es}"
    return start, end


def modify_ini_preserve_comments_and_case(file_path, log_path, time, vin=None, package_version=None):
    """修改 INI 文件中的 logPath、time（及可选 vin、package_version）参数，保留注释和参数大小写

    注释行（以 ; 或 # 开头）不会被修改。vin 为 None 时不修改 vin 行；
    package_version 为 None 时不修改 package_version 行。

    Args:
        file_path (str): INI 文件路径，如 './o.ini'
        log_path (str): 新的 logPath 值，写入 [PARAMETER] 段
        time (str): 新的 time 值，格式 'YYYYMMDDHHMMSS/YYYYMMDDHHMMSS'
        vin (str | None): 新的 vin 值；None 时不修改 vin 行
        package_version (str | None): 新的 package_version 值（大包版本）；None 时不修改该行
    """
    with open(os.path.abspath(file_path), 'r', encoding='utf-8') as file:
        lines = file.readlines()

    def _is_comment(s):
        return s.startswith(';') or s.startswith('#')

    with open(os.path.abspath(file_path), 'w', encoding='utf-8') as file:
        parameter_section = False
        vin_written = vin is None  # 不需要写 vin 时视为已处理
        version_written = package_version is None  # 不需要写 package_version 时视为已处理
        for line in lines:
            stripped_line = line.strip()
            # 进入 [PARAMETER] 段
            if stripped_line.lower().startswith("[parameter]"):
                parameter_section = True
            if parameter_section and not _is_comment(stripped_line):
                low = stripped_line.lower()
                # 匹配 logPath
                if low.startswith("logpath"):
                    key, _sep, _old_value = line.partition("=")
                    line = f"{key.strip()} = {log_path}\n"
                # 匹配 time（排除 time_ 前缀的其他键）
                elif low.startswith("time") and not low.startswith("time_"):
                    key, _sep, _old_value = line.partition("=")
                    line = f"{key.strip()} = {time}\n"
                # 匹配 vin（排除 vin_ 前缀的其他键）
                elif vin is not None and low.startswith("vin") and not low.startswith("vin_"):
                    key, _sep, _old_value = line.partition("=")
                    line = f"{key.strip()} = {vin}\n"
                    vin_written = True
                # 匹配 package_version（排除 package_version_ 前缀的其他键）
                elif package_version is not None and low.startswith("package_version") \
                        and not low.startswith("package_version_"):
                    key, _sep, _old_value = line.partition("=")
                    line = f"{key.strip()} = {package_version}\n"
                    version_written = True
            # 离开 [PARAMETER] 段
            if parameter_section and stripped_line.startswith('[') \
                    and not stripped_line.lower().startswith("[parameter]"):
                parameter_section = False
            file.write(line)
    # 需要写 vin 但 INI 中没有该键时，追加到 [PARAMETER] 段末尾
    if not vin_written or not version_written:
        try:
            with open(os.path.abspath(file_path), 'r', encoding='utf-8') as file:
                lines = file.readlines()
            out = []
            in_param = False
            vin_inserted = not (not vin_written)
            version_inserted = not (not version_written)
            for line in lines:
                stripped = line.strip()
                if stripped.lower().startswith("[parameter]"):
                    in_param = True
                    out.append(line)
                    continue
                if in_param and stripped.startswith('[') \
                        and not stripped.lower().startswith("[parameter]"):
                    # 在进入下一个 section 前插入缺失的键
                    if not vin_inserted and vin is not None:
                        out.append(f"vin = {vin}\n")
                        vin_inserted = True
                    if not version_inserted and package_version is not None:
                        out.append(f"package_version = {package_version}\n")
                        version_inserted = True
                    in_param = False
                out.append(line)
            if not vin_inserted and vin is not None:
                # 文件末尾没有下一个 section，直接追加
                out.append(f"vin = {vin}\n")
            if not version_inserted and package_version is not None:
                out.append(f"package_version = {package_version}\n")
            with open(os.path.abspath(file_path), 'w', encoding='utf-8') as file:
                file.writelines(out)
        except Exception as e:
            logger.exception(f'modify_ini: 追加缺失键行失败: {e}')
    logger.warning("INI 文件修改成功.")


# ======================== clp 解压工具环境检查 ========================
# clp 是 CLP(Compressed Log Processor) 官方 C++ 可执行文件，用于解压 .clp / .clp.zst 单文件归档。
# 该格式为 CLP single-file archive(文件头魔数 'YCLP')，官方 Python 绑定 clp-ffi-py 只支持
# IR stream 格式，无法读取此归档，因此必须依赖外部可执行文件，无纯 Python 替代方案。
CLP_INSTALL_GUIDE_URL = 'https://example.com/approved-clp-installation'


def resolve_clp_binary_path():
    """解析 clp 可执行文件路径，按 环境变量 > PATH 的优先级查找

    查找顺序（与仓库「环境变量 > secrets.json > fallback」的配置约定一致）：
      1. 环境变量 CLP_BINARY_PATH 指定的绝对路径
      2. PATH 中名为 clp 的可执行文件

    Returns:
        str | None: 可执行的 clp 路径；未找到时返回 None（不抛异常、不退出进程，由调用方决定后续处理）
    """
    try:
        custom_path = os.environ.get('CLP_BINARY_PATH')
        if custom_path:
            custom_path = os.path.expanduser(custom_path)
            if os.path.isfile(custom_path) and os.access(custom_path, os.X_OK):
                return custom_path
            logger.warning(
                f"❗环境变量 CLP_BINARY_PATH={custom_path} 指向的文件不存在或不可执行，回退到 PATH 查找"
            )

        return shutil.which('clp')
    except Exception as e:
        logger.warning(f"❗查找 clp 可执行文件异常: {e}")
        return None


def build_clp_missing_message(clp_task_count):
    """构造 clp 工具缺失时的安装指引提示文本

    Args:
        clp_task_count (int): 待解压的 clp 归档数量，用于说明影响范围

    Returns:
        str: 多行提示文本
    """
    return (
        f"\n❌ 未找到 clp 解压工具，无法解压 {clp_task_count} 个 .clp 日志包，程序终止。\n"
        f"   .clp 为 CLP 单文件归档格式，必须使用 clp 可执行文件解压，无纯 Python 替代方案。\n"
        f"   请按文档安装 clp 的 deb 安装包：{CLP_INSTALL_GUIDE_URL}\n"
        f"       sudo dpkg -i <clp 安装包>.deb\n"
        f"   若已有 clp 可执行文件，可直接指定其路径（无需安装）：\n"
        f"       export CLP_BINARY_PATH=/your/path/clp\n"
        f"   安装完成后用以下命令验证，应打印 clp 的 Usage 信息：\n"
        f"       clp\n"
    )


def _collect_existing_files(dirpath):
    """扫描 dirpath 下所有文件的相对路径（去掉前导 '/'），用于 O(1) 判断是否已解压

    Args:
        dirpath (str): 解压目标目录

    Returns:
        set: 相对路径集合，如 {'subdir/file.log', 'a.log'}，与归档内成员路径格式一致
    """
    existing = set()
    base_len = len(dirpath)
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            full = os.path.join(root, f)
            rel = full[base_len:]
            if rel.startswith('/') or rel.startswith(os.sep):
                rel = rel[1:]
            existing.add(rel)
    return existing


def _clp_extracted_log_name(file_path):
    """推断 clp 归档解压后的日志文件名（仅文件名，不含目录）

    clp 归档内保留了压缩时的原始路径前缀（实测形如 log/soc/log/xxx.log），
    该前缀因采集环境而异，因此只推断文件名，由调用方按 basename 匹配。

    命名规律（实测）：
        Thor_soc-VIN-2026_08_19_15_42_14+0800.clp      => Thor_soc-VIN-2026_08_19_15_42_14+0800.log
        rc_container_2026_08_19_16_52_22.clp.zst       => rc_container_2026_08_19_16_52_22.log

    Args:
        file_path (str): clp 归档文件路径

    Returns:
        str: 解压后的日志文件名
    """
    base_name = os.path.basename(file_path)
    if base_name.endswith('.clp.zst'):
        base_name = base_name[:-len('.clp.zst')]
    elif base_name.endswith('.clp'):
        base_name = base_name[:-len('.clp')]
    return base_name + '.log'


def _is_already_extracted(file_path, dirpath, compression_format, existing_files=None):
    """检查压缩包是否已解压：读取归档成员列表，任一成员已存在则视为已解压

    Args:
        file_path (str): 压缩文件路径
        dirpath (str): 解压目标目录，existing_files 为 None 时用于逐个磁盘检查
        compression_format (str): 压缩格式，'zip' / 'tar' / 'clp'
        existing_files (set, optional): dirpath 下已有文件的相对路径集合，传入时走 O(1) 集合查找

    Returns:
        bool: True 表示已解压，False 表示未解压或无法判断
    """
    try:
        if compression_format == 'zip':
            with zipfile.ZipFile(file_path, 'r') as zf:
                members = zf.namelist()
        elif compression_format == 'tar':
            with tarfile.open(file_path, 'r') as tf:
                members = tf.getnames()
        elif compression_format == 'clp':
            # clp 无列举归档成员的子命令（clp l 报 Unknown action），只能按命名规律推断产出文件名，
            # 且归档内保留的原始路径前缀不确定，故统一按 basename 匹配。
            expected_log_name = _clp_extracted_log_name(file_path)
            if existing_files is not None:
                return any(
                    os.path.basename(existing) == expected_log_name
                    for existing in existing_files
                )
            for root, _dirs, files in os.walk(dirpath):
                if expected_log_name in files:
                    full_path = os.path.join(root, expected_log_name)
                    if os.path.getsize(full_path) > 0:
                        return True
            return False
        else:
            return False

        # 归档成员可能是绝对路径，extractall 会去掉前导 '/'，统一 strip 后匹配磁盘相对路径
        normalized_members = [m.lstrip('/') for m in members if not m.endswith('/')]

        if existing_files is not None:
            return any(member in existing_files for member in normalized_members)

        for member in normalized_members:
            full_path = os.path.join(dirpath, member)
            if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                return True
        return False
    except Exception as e:
        logger.warning(f"检查已解压状态失败 {file_path}: {e}")
        return False


def extract_file(file_path, dirpath, compression_format):
    """按压缩格式解压文件到 dirpath

    Args:
        file_path (str): 压缩文件路径
        dirpath (str): 解压目标目录
        compression_format (str): 压缩格式，'zip' / 'tar' / 'clp'

    Raises:
        ValueError: 不支持的压缩格式
    """
    if compression_format == 'zip':
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(dirpath)
        except zipfile.BadZipFile:
            logger.warning(f"❗文件{file_path}损坏或无效=>跳过")
        except Exception as e:
            logger.warning(f"❗unzip失败:{file_path}:{e}")
    elif compression_format == 'tar':
        with tarfile.open(file_path, 'r') as tar_ref:
            tar_ref.extractall(dirpath)
    elif compression_format == 'clp':
        # 直接调用 clp 可执行文件，不再经由 clp.sh 包装脚本：
        #   1. clp.sh 需预装到 PATH，缺失时报 'clp.sh: not found'，排查成本高
        #   2. clp.sh 内的 mv 重命名逻辑基于错误的产出路径假设，实测从未生效（属死代码）
        clp_binary_path = resolve_clp_binary_path()
        if not clp_binary_path:
            logger.warning(f"❗未找到 clp 可执行文件，跳过解压: {file_path}")
            return
        try:
            result = subprocess.run(
                [clp_binary_path, 'x', file_path, dirpath],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
            )
            if result.returncode != 0:
                logger.warning(f"❗clp解压失败{file_path}:{result.stderr.strip()}")
        except Exception as e:
            logger.warning(f"❗clp异常{e}")
    else:
        raise ValueError(f"Unsupported compression format: {compression_format}")


def is_file_time_in_range(filename, time_range):
    """判断文件名中的时间是否在 time_range 范围内，end 向后扩展 1 小时

    Args:
        filename (str): 文件名，需包含 YYYY_MM_DD_HH_MM_SS 格式的时间戳
        time_range (str | None): 'start/end' 格式的时间范围，如 '20260710123300/20260710174100'；
                                  None 表示不校验，直接返回 True

    Returns:
        bool: True 表示在范围内（含 end + 1 小时扩展）

    Raises:
        ValueError: 文件名中未找到 YYYY_MM_DD_HH_MM_SS 时间信息
    """
    if time_range is None:
        return True
    start_time_str, end_time_str = time_range.split('/')
    start_time = datetime.strptime(start_time_str, "%Y%m%d%H%M%S")
    end_time = datetime.strptime(end_time_str, "%Y%m%d%H%M%S") + timedelta(hours=1)

    match = re.search(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})", filename)
    if not match:
        raise ValueError("文件名中未找到时间信息")

    file_time = datetime.strptime(''.join(match.groups()), "%Y%m%d%H%M%S")
    return start_time <= file_time <= end_time


def _safe_extract_worker(args):
    """多进程解压工作函数（须在模块顶层以支持 pickle 序列化）

    Args:
        args (tuple): (file_path, extract_dir, fmt) — 压缩文件路径、解压目录、压缩格式

    Returns:
        tuple: (file_path, error) — error 为 None 表示成功，否则为错误信息字符串
    """
    file_path, extract_dir, fmt = args
    try:
        extract_file(file_path, extract_dir, fmt)
        return file_path, None  # None 表示成功
    except ValueError as e:
        return file_path, f"ValueError: {e}"
    except Exception as e:
        return file_path, f"Exception: {e}"


def extract_and_move_logs(root_dir, folder_names, target_dir, time_range=None, max_workers=8):
    """提取压缩文件并复制 .log / message 文件到统一目录

    流程分两阶段：
      1. 多进程解压 root_dir 下名字匹配 folder_names 的子目录中的 .zip/.tar/.clp 到各自所在目录
      2. 多线程复制解压后的 .log 文件与 message 文件到 target_dir

    Args:
        root_dir (str): 日志根目录，递归搜索其下所有匹配 folder_names 的子目录
        folder_names (set | list): 需要提取的子目录名集合，如 {'log_fsdA', 'log_fsdB', 'soc'}
        target_dir (str): 统一的输出目录，.log 与 message 文件最终落在此处
        time_range (str, optional): 'start/end' 时间范围，用于过滤归档文件；None 表示不过滤
        max_workers (int): 复制阶段的最大线程数（同一 HDD 自动降为 1）

    Returns:
        bool: True 表示预处理完成；False 表示存在待解压的 .clp 归档但缺少 clp 工具，
              已打印安装指引且未执行任何解压，调用方应终止后续流程
    """

    SUPPORTED_FORMATS = ('.zip', '.tar', '.clp')

    os.makedirs(target_dir, exist_ok=True)

    # 找到所有匹配的子目录
    matched_dirs = []
    for dirpath, _dirnames, _filenames in os.walk(root_dir):
        if os.path.basename(dirpath) in folder_names:
            matched_dirs.append(dirpath)

    # ==========================================
    # 阶段一：多进程解压（CPU 密集型，绕过 GIL）
    # ==========================================
    extraction_tasks = []
    skipped_already_extracted = 0
    # 预构建每个 sub_dirpath 下已有文件集合，避免逐个压缩包重复扫描磁盘
    existing_files_cache = {}

    for dirpath in matched_dirs:
        for sub_dirpath, _sub_dirnames, sub_filenames in os.walk(dirpath):
            for filename in sub_filenames:
                matched_format = next(
                    (fmt.lstrip('.') for fmt in SUPPORTED_FORMATS if filename.endswith(fmt)),
                    None
                )
                if not matched_format:
                    continue
                try:
                    if not is_file_time_in_range(filename, time_range):
                        continue
                except ValueError:
                    continue

                file_path = os.path.join(sub_dirpath, filename)
                # 懒构建：同一个 sub_dirpath 只扫描一次
                if sub_dirpath not in existing_files_cache:
                    existing_files_cache[sub_dirpath] = _collect_existing_files(sub_dirpath)

                if _is_already_extracted(file_path, sub_dirpath, matched_format,
                                         existing_files_cache[sub_dirpath]):
                    skipped_already_extracted += 1
                    continue

                extraction_tasks.append((file_path, sub_dirpath, matched_format))

    if skipped_already_extracted:
        logger.warning(f"⏭ 跳过已解压文件：{skipped_already_extracted} 个（归档内文件已存在）")

    # 仅当确实存在待解压的 clp 归档时才要求 clp 工具：纯 zip/tar 的日志包无需安装 clp。
    # 此处只返回标志，不在本函数内退出进程——解压跑在 ProcessPoolExecutor 子进程中，
    # 退出动作必须留给主进程（start.py）执行。
    clp_task_count = sum(1 for task in extraction_tasks if task[2] == 'clp')
    if clp_task_count and not resolve_clp_binary_path():
        logger.error(build_clp_missing_message(clp_task_count))
        return False

    if extraction_tasks:
        max_processes = get_dynamic_max_processes()
        logger.warning(f"🔓 开始多进程解压，共 {len(extraction_tasks)} 个文件，进程数：{max_processes}")
        with ProcessPoolExecutor(max_workers=max_processes) as executor:
            futures = {
                executor.submit(_safe_extract_worker, task): task[0]
                for task in extraction_tasks
            }
            for future in as_completed(futures):
                file_path = futures[future]
                try:
                    _, error = future.result()
                    if error:
                        logger.warning(f"解压失败 {file_path}: {error}")
                except Exception as e:
                    logger.warning(f"解压进程异常 {file_path}: {e}")
        logger.warning("🔓 所有解压任务完成")

    # ==========================================
    # 阶段二：多线程复制（I/O 密集型）
    # ==========================================
    raw_copy_tasks = []
    message_copy_prints = []

    for dirpath in matched_dirs:
        message_sources = []
        message_targets = []

        for sub_dirpath, _sub_dirnames, sub_filenames in os.walk(dirpath):
            for filename in sub_filenames:
                lower_filename = filename.lower()

                if lower_filename.endswith('.log'):
                    source_file = os.path.join(sub_dirpath, filename)
                    base_name = os.path.splitext(filename)[0]
                    normalized_filename = base_name + '.log'
                    target_file = os.path.join(target_dir, normalized_filename)

                    raw_copy_tasks.append((source_file, target_file))

                    # SOC 类型匹配 message 目标文件
                    SOC_TYPES = ("OrinX", "Thor", "M100")
                    soc_file = normalized_filename.split('-', 1)[0] + '.log'
                    if any(soc in soc_file for soc in SOC_TYPES):
                        message_targets.append(os.path.join(target_dir, soc_file))

                elif filename == 'message':
                    message_sources.append(os.path.join(sub_dirpath, filename))

        for m_src, m_tgt in zip(message_sources, message_targets):
            raw_copy_tasks.append((m_src, m_tgt))
            message_copy_prints.append(f"{m_src}=>{m_tgt}")

    # 去重：防止多线程同时写入同一目标文件
    unique_copy_tasks = {tgt: src for src, tgt in raw_copy_tasks}

    # 跳过目标已存在且大小相同的任务
    final_copy_tasks = []
    skipped_copy_count = 0
    for tgt, src in unique_copy_tasks.items():
        if os.path.exists(tgt) and os.path.getsize(tgt) == os.path.getsize(src):
            skipped_copy_count += 1
        else:
            final_copy_tasks.append((src, tgt))

    if skipped_copy_count:
        logger.warning(f"⏭ 跳过已存在文件：{skipped_copy_count} 个（目标大小相同）")

    def safe_link_or_copy(source, target):
        """优先硬链接，跨文件系统等无法链接时回退为复制

        后续统计流程（mawk 提取、绘图）只读取这些日志，不会改写，
        因此用硬链接共享同一份数据即可，省去同盘复制的全量 I/O。

        Args:
            source (str): 源文件路径
            target (str): 目标文件路径
        """
        try:
            os.link(source, target)
            return
        except FileExistsError:
            # 目标已存在（大小不同才进入任务列表），先删除再链接
            try:
                os.remove(target)
                os.link(source, target)
                return
            except Exception as e:
                logger.warning(f"硬链接覆盖失败，回退复制 {source}: {e}")
        except OSError as e:
            # EXDEV(跨文件系统) / EPERM(文件系统不支持硬链接) 等，回退复制
            logger.debug(f"硬链接不可用，回退复制 {source}: {e}")
        except Exception as e:
            logger.warning(f"硬链接异常，回退复制 {source}: {e}")

        try:
            shutil.copy2(source, target)
        except Exception as e:
            logger.warning(f"复制失败 {source}: {e}")

    if final_copy_tasks:
        for msg in message_copy_prints:
            logger.warning(msg)

        # 硬链接不搬运数据，不受磁盘类型影响；仅回退到复制时才需按 HDD 降并发
        src_device = os.stat(final_copy_tasks[0][0]).st_dev
        tgt_device = os.stat(target_dir).st_dev
        same_filesystem = (src_device == tgt_device)
        if same_filesystem:
            # 同文件系统可硬链接，纯元数据操作，单线程已足够快且避免无谓的线程开销
            actual_workers = 1
            logger.warning(f"🔗 source 与 target 同文件系统，使用硬链接，共 {len(final_copy_tasks)} 个文件")
        else:
            # 跨文件系统必须复制；同一块 HDD 多线程会加剧磁头寻道，降为单线程
            actual_workers = 1 if _is_rotational_device(target_dir) else max_workers
            if actual_workers == 1:
                logger.warning(f"📋 跨文件系统且目标为 HDD，使用单线程复制，共 {len(final_copy_tasks)} 个文件")
            else:
                logger.warning(f"📋 跨文件系统，开始多线程复制，共 {len(final_copy_tasks)} 个文件，线程数：{actual_workers}")

        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            futures = [executor.submit(safe_link_or_copy, *task) for task in final_copy_tasks]
            for future in as_completed(futures):
                future.result()

    return True


def set_dir_and_time_from_path(src_dir, vin=None, package_version=None):
    """从目录名提取起止时间，写入 ./o.ini 的 logPath 和 time 参数

    Args:
        src_dir (str): 包含起止时间信息的日志根目录路径
        vin (str | None): 可选 VIN，传入时一并写入 o.ini；None 时不修改 vin
        package_version (str | None): 可选大包版本，传入时一并写入 o.ini；None 时不修改
    """
    start, end = find_time_strings_in_directory(src_dir)
    time = start + '/' + end
    logPath = os.path.join(src_dir, 'perf')
    modify_ini_preserve_comments_and_case('./o.ini', logPath, time, vin, package_version)


def set_dir_and_time_from_args(src_dir, time, vin=None, package_version=None):
    """按传入的 src_dir 和 time 写入 ./o.ini 的 logPath 和 time 参数

    Args:
        src_dir (str): 日志根目录路径
        time (str): 时间范围字符串，如 '20260710123300/20260710174100'
        vin (str | None): 可选 VIN，传入时一并写入 o.ini；None 时不修改 vin
        package_version (str | None): 可选大包版本，传入时一并写入 o.ini；None 时不修改
    """
    logPath = os.path.join(src_dir, 'perf')
    modify_ini_preserve_comments_and_case('./o.ini', logPath, time, vin, package_version)


# ======================== 从日志目录提取 VIN 与大包版本 ========================
# 日志文件名约定：{soc_type}-{VIN}-{timestamp}.log，VIN 为 17 位字母数字
_VIN_PATTERN = re.compile(r'[A-Z0-9]{17}')

# soc 级日志前缀白名单：只有这些文件名携带真实 VIN，
# 借此排除 XVM_BZCU 等同为 17 位设备号的干扰文件
_SOC_LOG_PREFIXES = (
    'FSDA_M100', 'FSDB_M100', 'Thor_soc', 'FSDA_OrinX', 'FSDB_OrinX',
)

# 大包版本在 soc 日志开头打印，形如：
#   version: CCUFSD_SOC_M100-RELSIGN_ota-9.1.0-511.0001-20260709-511.
# 注意日志开头常有 "lios_app_version: deprecated" 这类含 "version:" 子串的行，
# 故必须行首锚定 ^version:，避免误匹配到 deprecated 等无关行。
# 仅读取每个文件前 2MB，避免读取大文件耗时
_VERSION_PATTERN = re.compile(r'^version:\s*(\S+)', re.MULTILINE)
_VERSION_SCAN_BYTES = 2 * 1024 * 1024

# VIN / 版本兜底值
_VIN_FALLBACK = 'VIN'
_VERSION_FALLBACK = '未知'


def _extract_version_from_file(file_path):
    """读取文件开头，提取首个 `version: <token>` 并去掉末尾句点

    仅读取前 _VERSION_SCAN_BYTES 字节，避免读取大日志文件耗时。

    Args:
        file_path (str): 日志文件路径

    Returns:
        str | None: 版本字符串（已去除末尾句点）；未命中返回 None
    """
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(_VERSION_SCAN_BYTES)
        if not chunk:
            return None
        # 容错：兼容非 utf-8 片段，忽略解码错误
        text = chunk.decode('utf-8', errors='ignore')
        m = _VERSION_PATTERN.search(text)
        if not m:
            return None
        version = m.group(1)
        # 去掉末尾句点（日志中版本串常以句点结尾）
        if version.endswith('.'):
            version = version[:-1]
        return version or None
    except Exception:
        return None


def extract_vin_from_log_dir(log_dir):
    """从日志目录下的 soc 级 .log 文件中提取 VIN 与大包版本

    VIN：先按 soc 前缀白名单过滤，再取 17 位 VIN 的众数；
         白名单/全量均无命中时兜底遍历，最终匹配不到返回 'VIN'。

    大包版本：仅扫描 soc 白名单前缀日志开头打印的 `version: <token>`，
             取众数；均无命中返回 '未知'。

    Args:
        log_dir (str): 日志根目录，递归扫描其下所有 .log 文件

    Returns:
        tuple: (vin, package_version)；package_version 为去重排序后用 " / " 连接的全部版本串；
               提取失败时对应位置返回兜底值
    """
    try:
        log_dir = os.path.abspath(log_dir)
        if not os.path.isdir(log_dir):
            logger.warning(f'extract_vin_from_log_dir: 目录不存在 {log_dir}')
            return _VIN_FALLBACK, _VERSION_FALLBACK

        vin_pattern_strict = re.compile(r'-([A-Z0-9]{17})-\d{4}_\d{2}_\d{2}')

        # 统计各 VIN 出现次数：soc 白名单内 / 全量文件两组
        vin_count_whitelist = {}
        vin_count_all = {}
        # 统计 soc 白名单日志开头打印的大包版本出现次数
        version_count = {}
        for _root, _dirs, files in os.walk(log_dir):
            for name in files:
                if not name.endswith('.log'):
                    continue
                m = vin_pattern_strict.search(name)
                if m:
                    vin = m.group(1)
                    vin_count_all[vin] = vin_count_all.get(vin, 0) + 1
                if name.startswith(_SOC_LOG_PREFIXES):
                    if m:
                        vin_count_whitelist[vin] = vin_count_whitelist.get(vin, 0) + 1
                    # 从 soc 白名单日志开头提取大包版本
                    version = _extract_version_from_file(os.path.join(_root, name))
                    if version:
                        version_count[version] = version_count.get(version, 0) + 1

        # ===== VIN =====
        vin_result = _VIN_FALLBACK
        # 优先取 soc 白名单众数
        if vin_count_whitelist:
            best_vin = max(vin_count_whitelist, key=vin_count_whitelist.get)
            if len(vin_count_whitelist) > 1:
                logger.warning(f'VIN 提取: soc 日志含多个 VIN {list(vin_count_whitelist)}，取众数 {best_vin}')
            vin_result = best_vin
        elif vin_count_all:
            # 回退 1：全量文件严格匹配众数
            best_vin = max(vin_count_all, key=vin_count_all.get)
            logger.warning(f'VIN 提取: 无 soc 日志，从全量文件取众数 {best_vin}')
            vin_result = best_vin
        else:
            # 回退 2：遍历 '-' 段匹配首个 17 位字母数字
            for _root, _dirs, files in os.walk(log_dir):
                for name in files:
                    if not name.endswith('.log'):
                        continue
                    for seg in name.split('-'):
                        if _VIN_PATTERN.fullmatch(seg):
                            vin_result = seg
                            break
                    if vin_result != _VIN_FALLBACK:
                        break
                if vin_result != _VIN_FALLBACK:
                    break
            if vin_result == _VIN_FALLBACK:
                logger.warning(f'extract_vin_from_log_dir: 在 {log_dir} 下未匹配到 VIN')

        # ===== 大包版本 =====
        # 双 SOC（如 FSDA_OrinX/FSDB_OrinX）会打印各自的大包版本，全部记录：
        # 去重后按字典序排序，用 " / " 连接成一个字符串。
        version_result = _VERSION_FALLBACK
        if version_count:
            distinct_versions = sorted(version_count.keys())
            if len(distinct_versions) > 1:
                logger.warning(f'大包版本提取: 含多个版本 {distinct_versions}，全部记录')
            version_result = ' / '.join(distinct_versions)
            logger.warning(f'大包版本提取: {version_result}')
        else:
            logger.warning(f'extract_vin_from_log_dir: 在 {log_dir} 下未提取到大包版本')

        return vin_result, version_result
    except Exception as e:
        logger.exception(f'extract_vin_from_log_dir: 提取 VIN/大包版本失败: {e}')
        return _VIN_FALLBACK, _VERSION_FALLBACK


# ======================== 查找keyword统计，是否存于file_list ========================
def find_path_by_keyword(file_list, keyword, new_keyword=None):
    """在 file_list 中按文件名匹配 keyword，可选返回替换为 new_keyword 后的输出路径

    Args:
        file_list (list): 待搜索的文件路径列表
        keyword (str): 需匹配的子串，仅匹配文件名部分
        new_keyword (str, optional): 若提供，将匹配文件名中的 keyword 替换为 new_keyword 作为输出路径

    Returns:
        tuple: (匹配到的原路径, 输出路径)；未匹配返回 (None, None)
    """
    output_file = None
    for p in file_list:
        if keyword in os.path.basename(p):
            if new_keyword:
                dir_path = os.path.dirname(p)
                file_name = os.path.basename(p)
                output_file = os.path.join(dir_path, file_name.replace(keyword, new_keyword))
            return p, output_file
    return None, None
