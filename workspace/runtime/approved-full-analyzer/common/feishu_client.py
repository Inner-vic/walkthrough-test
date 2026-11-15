"""
飞书客户端公共模块

提供与飞书 API 交互的客户端类，包括文件操作、表格操作、图片操作等功能。
从 thor_performance 和 orin_x_performance 中提取的公共代码。
"""

import os
import time
import logging
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from requests_toolbelt import MultipartEncoder

logger = logging.getLogger(__name__)

# 全局 Session：复用 TCP/TLS 连接（HTTP keep-alive），
# 避免每次请求都重新握手（省去约 100-300ms/次）。
# 多个 theme 由上层线程池（general_performance 中 max_workers=16）并发调用本模块，
# 共享同一 Session 依赖 urllib3 连接池的线程安全性；飞书接口无状态、不依赖 cookie，
# 无并发写 Session 状态的隐患。pool_maxsize 对齐并发上限，避免超额时新建即弃连接。
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)


def request(method, url, headers, payload=None, data=None):
    """
    发送 HTTP 请求的通用函数

    Args:
        method: HTTP 方法 (GET, POST, PUT, PATCH, DELETE)
        url: 请求 URL
        headers: 请求头
        payload: JSON 数据 (用于 Content-Type: application/json)
        data: 表单数据 (用于 multipart/form-data)

    Returns:
        dict: 响应的 JSON 数据

    Raises:
        LarkException: 飞书 API 返回错误
        requests.RequestException: 网络请求异常
    """
    try:
        if data is not None:
            response = _session.request(method, url, headers=headers, data=data, timeout=30)
        elif payload is not None:
            response = _session.request(method, url, headers=headers, json=payload, timeout=30)
        else:
            response = _session.request(method, url, headers=headers, timeout=30)

        response.raise_for_status()
        result = response.json()

        if result.get('code') != 0:
            raise LarkException(
                code=result.get('code', -1),
                msg=result.get('msg', 'Unknown error')
            )

        return result
    except requests.RequestException as e:
        logger.error(f"请求失败 {method} {url}: {e}")
        raise
    except Exception as e:
        logger.exception(f"请求发生未预期错误 {method} {url}: {e}")
        raise


class LarkException(Exception):
    """飞书 API 异常"""
    def __init__(self, code=0, msg=None):
        self.code = code
        self.msg = msg

    def __str__(self):
        return f"LarkException(code={self.code}, msg={self.msg})"

    __repr__ = __str__


class Client(object):
    """
    飞书客户端

    提供与飞书 API 交互的功能，包括：
    - 认证：获取 tenant_access_token
    - 文件操作：复制文件、上传文件、创建文件夹
    - 表格操作：读取/更新单元格、合并单元格、设置样式
    - 图片操作：上传图片、下载图片
    """

    # 从环境变量或配置文件读取，保留回退值以向后兼容
    from common.secrets_manager import get_secret

    APP_ID = get_secret(
        'feishu', 'app_id',
        fallback=''  # 向后兼容的回退值
    )
    APP_SECRET = get_secret(
        'feishu', 'app_secret',
        fallback=''  # 向后兼容的回退值
    )
    LARK_HOST = 'https://open.feishu.cn'

    def __init__(self):
        self._host = self.LARK_HOST
        # 分片目录文件数缓存：{folder_token: count}
        # 单次报告生成周期内避免重复翻页统计；copy 成功 +1，目录满作废
        self._folder_file_count_cache = {}

    @staticmethod
    def _make_json_headers(access_token=None):
        """
        创建 JSON 请求头

        Args:
            access_token: 访问令牌（可选）

        Returns:
            dict: 请求头字典
        """
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': 'Bearer ' + access_token
        } if access_token else {
            'Content-Type': 'application/json; charset=utf-8'
        }
        return headers

    @staticmethod
    def _make_auth_header(access_token):
        """
        创建仅包含 Authorization 的请求头

        Args:
            access_token: 访问令牌

        Returns:
            dict: 请求头字典
        """
        headers = {
            'Authorization': 'Bearer ' + access_token
        }
        return headers

    def get_tenant_access_token(self):
        """获取 tenant_access_token"""
        url = self._host + "/open-apis/auth/v3/app_access_token/internal/"
        headers = self._make_json_headers()
        payload = {'app_id': self.APP_ID, 'app_secret': self.APP_SECRET}
        resp = request("POST", url, headers, payload)
        return resp['tenant_access_token']

    def list_folder_files(self, access_token, folder_token, filter_type=None,
                          max_pages=50):
        """
        翻页列举指定文件夹下的全部子项。

        飞书 GET /drive/v1/files 单页最大 200 条，且不支持服务端按 type 过滤，
        需要客户端翻页 + 过滤。单层节点上限 1500，最多 ⌈1500/200⌉=8 页。

        Args:
            access_token: 访问令牌
            folder_token: 文件夹 token
            filter_type: 可选，按 type 过滤（如 'folder'）；None 表示不过滤
            max_pages: 安全上限，避免异常情况下死循环

        Returns:
            list[dict]: 子项列表，每项含 token / name / type 等字段
        """
        headers = self._make_json_headers(access_token)
        all_files = []
        page_token = None
        page = 0
        while True:
            page += 1
            if page > max_pages:
                logger.warning(f"list_folder_files 翻页超上限 {max_pages}，folder={folder_token}")
                break
            list_url = self._host + \
                f"/open-apis/drive/v1/files?folder_token={folder_token}&page_size=200"
            if page_token:
                list_url += f"&page_token={page_token}"
            try:
                resp = request("GET", list_url, headers)
            except Exception as e:
                logger.error(f"list_folder_files 翻页失败 folder={folder_token} page={page}: {e}")
                break
            data = resp.get('data', {})
            files = data.get('files', [])
            all_files.extend(files)
            has_more = data.get('has_more', False)
            # 飞书应答的翻页字段名为 next_page_token（非 page_token），
            # 误读会导致 page_token 恒为 None，翻页在首页(200条)后即中断，
            # 造成 >200 文件的目录被截断、文件总数统计偏小。
            page_token = data.get('next_page_token')
            logger.info(
                f"当前文件地址:{folder_token}, 累计文件总数:{len(all_files)}"
            )
            if not has_more or not page_token:
                break
        if filter_type:
            all_files = [f for f in all_files if f.get('type') == filter_type]
        logger.info(
            f"当前文件地址:{folder_token}, 累计文件总数:{len(all_files)}"
        )
        return all_files

    @staticmethod
    def _parse_shard_index(name, month):
        """
        从分片文件夹名 YYYYMM_pNN 解析编号 NN；不匹配返回 -1。
        """
        prefix = f"{month}_p"
        if not name.startswith(prefix):
            return -1
        tail = name[len(prefix):]
        if not tail.isdigit():
            return -1
        return int(tail)

    def get_or_create_folder(self, access_token, parent_token, name):
        """
        在 parent_token 下查找名为 name 的子文件夹，找不到则创建。
        使用翻页列举避免漏判（飞书 create_folder 允许同名且不报错，
        若列举不全会重复创建同名文件夹）。
        """
        logger.info(
            f"当前文件地址:{parent_token}, 文件名称:{name}"
        )
        try:
            sub_folders = self.list_folder_files(
                access_token, parent_token, filter_type='folder'
            )
            for f in sub_folders:
                if f.get('name') == name:
                    logger.info(
                        f"当前文件地址:{parent_token}, 文件名称:{name}, 选片:{f.get('token')}"
                    )
                    return f['token']
        except Exception as e:
            logger.error(
                f"get_or_create_folder 列举失败 folder={parent_token} name={name}: {e}"
            )

        create_url = self._host + "/open-apis/drive/v1/files/create_folder"
        headers = self._make_json_headers(access_token)
        payload = {"name": name, "folder_token": parent_token}
        resp = request("POST", create_url, headers, payload)
        new_token = resp['data']['token']
        logger.warning(
            f"get_or_create_folder 新建 name={name} parent={parent_token} token={new_token}"
        )
        return new_token

    def get_or_create_monthly_folder(self, access_token, parent_token):
        """
        在指定的父文件夹下查找当前年月的子文件夹，如果不存在则创建。
        保留原签名以向后兼容，内部委托给 get_or_create_folder。
        """
        folder_name = datetime.now().strftime("%Y%m")
        return self.get_or_create_folder(access_token, parent_token, folder_name)

    def get_writable_folder(self, access_token, root_folder_token):
        """
        在 root_folder_token 下取/建 YYYYMM 月度文件夹，
        再在月度文件夹下取/建 YYYYMM_pNN 分片子文件夹，
        返回一个文件数 < 阈值(默认1350) 的可写分片 token。

        阈值预留 150 余量（飞书单层硬上限 1500）。
        分片名 YYYYMM_p01 / YYYYMM_p02 ...，按编号自增。
        """
        threshold = 1350
        try:
            from common.secrets_manager import get_secret
            threshold = int(get_secret(
                'feishu_folders', 'folder_capacity_threshold',
                fallback=1350
            ))
        except Exception as e:
            logger.warning(f"读取 folder_capacity_threshold 失败，使用默认 1350: {e}")
        logger.info(
            f"当前文件地址:{root_folder_token}, 累计文件总数:0, 选片:待定 阈值={threshold}"
        )

        month = datetime.now().strftime("%Y%m")
        month_token = self.get_or_create_folder(
            access_token, root_folder_token, month
        )
        logger.info(f"当前文件地址:{month_token}, 文件名称:{month}")

        try:
            sub_folders = self.list_folder_files(
                access_token, month_token, filter_type='folder'
            )
        except Exception as e:
            logger.error(f"get_writable_folder 列举分片失败 month={month}: {e}")
            sub_folders = []

        shards = []
        for f in sub_folders:
            idx = self._parse_shard_index(f.get('name', ''), month)
            if idx > 0:
                shards.append((idx, f.get('name'), f.get('token')))
        shards.sort(key=lambda x: x[0])
        if shards:
            shard_desc = ", ".join(f"{s[1]}(idx={s[0]})" for s in shards)
        else:
            shard_desc = "无"
        logger.warning(
            f"当前文件地址:{month_token}, 文件名称:{month}, 已有分片:{shard_desc}"
        )

        if not shards:
            new_name = f"{month}_p01"
            new_token = self.get_or_create_folder(
                access_token, month_token, new_name
            )
            self._folder_file_count_cache[new_token] = 0
            logger.warning(
                f"当前文件地址:{new_token}, 文件名称:{new_name}, 累计文件总数:0, 选片:{new_token}"
            )
            return new_token

        max_idx, max_name, max_token = shards[-1]
        count = self._count_folder_files(access_token, max_token)
        if count < threshold:
            self._folder_file_count_cache[max_token] = count
            logger.warning(
                f"当前文件地址:{max_token}, 文件名称:{max_name}, 累计文件总数:{count}, 选片:{max_token}"
            )
            return max_token

        next_idx = max_idx + 1
        new_name = f"{month}_p{next_idx:02d}"
        new_token = self.get_or_create_folder(
            access_token, month_token, new_name
        )
        self._folder_file_count_cache[new_token] = 0
        logger.warning(
            f"当前文件地址:{max_token}, 文件名称:{max_name}, 累计文件总数:{count}≥{threshold} 已满,"
            f"新建 {new_name}, 选片:{new_token}"
        )
        return new_token

    def _count_folder_files(self, access_token, folder_token):
        """
        统计文件夹下文件数量（不过滤类型），带实例缓存。
        """
        cached = self._folder_file_count_cache.get(folder_token)
        if cached is not None:
            logger.info(f"当前文件地址:{folder_token}, 累计文件总数:{cached} (缓存)")
            return cached
        try:
            files = self.list_folder_files(access_token, folder_token)
        except Exception as e:
            logger.error(f"_count_folder_files 统计失败 folder={folder_token}: {e}")
            return 0
        count = len(files)
        self._folder_file_count_cache[folder_token] = count
        return count

    def _is_folder_full_error(self, exc):
        """
        判断异常是否表示目标文件夹已满（单层节点超限）。
        飞书无稳定公开错误码，依据 msg 关键字判定。
        """
        if not isinstance(exc, LarkException):
            return False
        msg = (exc.msg or '').lower()
        keywords = ['sibling', 'out of sibling', 'folder full', '目录已满',
                    '超出限制', 'node']
        return any(k in msg for k in keywords)

    def copy_file(self, access_token, file_token, file_name, folder_token=''):
        """
        复制文件到指定文件夹。

        Args:
            folder_token: 显式指定目标文件夹（不参与分片滚动，用于归档等场景）；
                          为空时自动走"月度+分片"滚动逻辑，避免单目录写满。
        Returns:
            dict: 新文件信息（含 token/url）；失败返回 None。
        """
        from common.secrets_manager import get_secret
        try:
            url = self._host + f"/open-apis/drive/v1/files/{file_token}/copy"
            headers = self._make_json_headers(access_token)

            if folder_token:
                target_folder_token = folder_token
                logger.info(f"copy_file 显式 folder={target_folder_token}")
            else:
                root_folder_token = get_secret(
                    'feishu_folders', 'root_folder_token',
                    fallback=''
                )
                target_folder_token = self.get_writable_folder(
                    access_token, root_folder_token
                )
                logger.info(f"copy_file 选定分片 folder={target_folder_token}")

            payload = {
                "name": file_name, "type": "sheet",
                "folder_token": target_folder_token
            }

            max_retries = 3
            wait = 2
            for attempt in range(max_retries):
                try:
                    resp = request("POST", url, headers, payload)
                    new_file = resp['data']['file']
                    self._folder_file_count_cache[target_folder_token] = \
                        self._folder_file_count_cache.get(
                            target_folder_token, 0) + 1
                    logger.info(
                        f"当前文件地址:{target_folder_token}, 文件名称:{file_name}, 选片:{new_file.get('token')}"
                    )
                    return new_file
                except LarkException as e:
                    if self._is_folder_full_error(e):
                        logger.warning(
                            f"copy_file 目录满 folder={target_folder_token} err={e}，切下一分片重试"
                        )
                        self._folder_file_count_cache.pop(
                            target_folder_token, None
                        )
                        if not folder_token:
                            root_folder_token = get_secret(
                                'feishu_folders', 'root_folder_token',
                                fallback=''
                            )
                            next_token = self.get_writable_folder(
                                access_token, root_folder_token
                            )
                            if next_token != target_folder_token:
                                target_folder_token = next_token
                                payload['folder_token'] = target_folder_token
                                continue
                        logger.error(f"copy_file 目录满且无法切分片，放弃")
                        return None
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"copy_file 失败 {attempt + 1}/{max_retries}: {e}，{wait}s 后重试"
                        )
                        time.sleep(wait)
                        wait = min(wait * 2, 30)
                    else:
                        logger.error(f"copy_file 重试 {max_retries} 次仍失败: {e}")
                        return None
        except Exception as e:
            logger.exception(f"copy_file 未预期异常 name={file_name}: {e}")
            return None

    def update_permission(self, access_token, file_token, file_type="sheet"):
        """更新文件权限（目前已禁用）

        20251205 飞书收紧了文档权限，设置全体权限会失败，故直接返回 True 跳过。
        保留方法签名以兼容历史调用点。
        """
        return True

    def find_cell(self, access_token, spreadsheet_token, sheet_id, find_str):
        """在表格中查找单元格"""
        url = self._host + \
            f"/open-apis/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/{sheet_id}/find"
        headers = self._make_json_headers(access_token)
        payload = {"find_condition": {"range": sheet_id + "!A:E", "match_case": False, "match_entire_cell": False,
                                      "search_by_regex": False, "include_formulas": False}, "find": find_str}
        resp = request("POST", url, headers, payload)
        return resp['data']

    def get_values_batch(self, access_token, spreadsheet_token, ranges, value_render="Formula"):
        """批量读取单元格值"""
        url = self._host + \
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_get?" f"ranges={ranges}&valueRenderOption={value_render}"
        headers = self._make_json_headers(access_token)
        payload = {"ranges": ranges, "valueRenderOption": "Formula"}
        resp = request("GET", url, headers, payload)
        return resp['data']

    def update_values(self, access_token, spreadsheet_token, crange, values):
        """更新单元格值"""
        url = self._host + \
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"
        headers = self._make_json_headers(access_token)
        payload = {"valueRange": {"range": crange, "values": values}}
        resp = request("PUT", url, headers, payload)
        return resp['data']

    def update_values_batch(self, access_token, spreadsheet_token, value_ranges):
        """批量更新多个区间的单元格值

        一次请求写入多个 range，替代逐个 range 调用 update_values，
        减少 HTTP 往返。飞书 values_batch_update 接口单次上限 5000 行 x 100 列。

        Args:
            value_ranges (list): [{"range": str, "values": 二维列表}, ...]

        Returns:
            dict: 接口返回的 data
        """
        url = self._host + \
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values_batch_update"
        headers = self._make_json_headers(access_token)
        payload = {"valueRanges": value_ranges}
        resp = request("POST", url, headers, payload)
        return resp['data']

    def batch_set_cell_style(self, access_token, spreadsheet_token, crange):
        """批量设置单元格样式"""
        url = self._host + \
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/style"
        headers = self._make_json_headers(access_token)
        payload = {"appendStyle": {
            "range": crange, "style": {"formatter": "@"}}}
        resp = request("PUT", url, headers, payload)
        return resp['data']

    def insert_image(self, access_token, spreadsheet_token, image_data):
        """在表格中插入图片"""
        url = self._host + "/open-apis/sheets/v2/spreadsheets/%s/values_image" % spreadsheet_token
        headers = self._make_auth_header(access_token)
        payload = {"range": image_data[0],
                   "image": image_data[1], "name": image_data[2]}
        resp = request("POST", url, headers, payload)
        return resp['data']

    def merge_cells(self, access_token, spreadsheet_token, crange):
        """合并单元格"""
        url = self._host + "/open-apis/sheets/v2/spreadsheets/%s/merge_cells" % spreadsheet_token
        headers = self._make_json_headers(access_token)
        payload = {"range": crange, "mergeType": "MERGE_ALL"}
        resp = request("POST", url, headers, payload)
        return resp['data']

    def create_spreadsheet(self, access_token, foldertoken, title):
        """创建电子表格"""
        url = self._host + "/open-apis/sheets/v3/spreadsheets"
        headers = self._make_json_headers(access_token)
        payload = {"title": title, "folder_token": foldertoken}
        resp = request("POST", url, headers, payload)
        return resp['data']['spreadsheet']['spreadsheet_token'], resp['data']['spreadsheet']['url']

    def _get_metainfo_with_retry(self, access_token, doctoken,
                                 max_retries=3, base_wait_seconds=3):
        """
        获取电子表格元数据，对可重试的临时错误自动退避重试。

        可重试场景：
        - code=95201：复制文件后元数据异步初始化中（ContentMeta is processing）
        - code=95203：飞书服务端临时错误（server error）
        - requests.RequestException：网络超时/连接异常等
        采用指数退避：首次等待 base_wait_seconds 秒，后续加倍，最多重试 max_retries 次。
        """
        url = self._host + "/open-apis/sheets/v2/spreadsheets/" + doctoken + "/metainfo"
        headers = self._make_json_headers(access_token)
        retryable_codes = {95201, 95203}
        wait_seconds = base_wait_seconds
        for attempt in range(max_retries):
            try:
                resp = request("GET", url, headers)
                return resp
            except (LarkException, requests.RequestException) as e:
                is_retryable = isinstance(e, requests.RequestException) or \
                    (isinstance(e, LarkException) and e.code in retryable_codes)
                if is_retryable and attempt < max_retries - 1:
                    logger.warning(
                        f"获取文档元数据失败({e})，"
                        f"等待 {wait_seconds}s 后重试 ({attempt + 1}/{max_retries})..."
                    )
                    time.sleep(wait_seconds)
                    wait_seconds = min(wait_seconds * 2, 30)  # 指数退避，上限30s
                else:
                    raise
        # 不会执行到这里，但为了类型安全加上
        raise LarkException(code=95203, msg="获取文档元数据失败，已超出最大重试次数")

    def get_first_sheetid(self, access_token, doctoken):
        """获取电子表格的第一个sheet的ID"""
        resp = self._get_metainfo_with_retry(access_token, doctoken)
        return resp['data']['sheets'][0]["sheetId"]

    def get_sheets_info(self, access_token, doctoken):
        """获取电子表格的所有sheet信息"""
        resp = self._get_metainfo_with_retry(access_token, doctoken)
        return resp['data']['sheets']

    def sheets_batch_update(self, access_token, spreadsheet_token, sheetid):
        """批量更新表格设置（冻结行/列）"""
        url = self._host + \
            "/open-apis/sheets/v2/spreadsheets/%s/sheets_batch_update" % spreadsheet_token
        headers = self._make_json_headers(access_token)
        payload = {"requests": [
            {"updateSheet": {"properties": {"sheetId": sheetid, "frozenColCount": 0, "frozenRowCount": 1}}}]}
        resp = request("POST", url, headers, payload)
        return resp['data']

    def media_download_image(self, access_token, file_token):
        """下载图片"""
        url = self._host + f"/open-apis/drive/v1/medias/{file_token}/download"
        headers = self._make_json_headers(access_token)
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        cl = response.content
        return cl

    def upload_file_to_drive(self, access_token, file_path, parent_node=None, file_name=None):
        """上传文件到飞书云盘"""
        prepare_url = self._host + f"/open-apis/drive/v1/files/upload_prepare"
        part_url = self._host + f"/open-apis/drive/v1/files/upload_part"
        finish_url = self._host + f"/open-apis/drive/v1/files/upload_finish"
        if file_name is None:
            file_name = os.path.basename(file_path)
        part_size = 4194304
        if parent_node is None:
            from common.secrets_manager import get_secret
            parent_node = get_secret(
                'feishu_folders', 'upload_parent_node',
                fallback=''
            )

        def cal_file_block(local_path):
            result = []
            with open(local_path, 'rb') as f:
                index = 0
                while True:
                    chunk = f.read(part_size)
                    if not chunk:
                        break
                    result.append(chunk)
                    index += 1
            return result

        file_stat = os.stat(file_path)
        payload = {'file_name': file_name, 'parent_type': 'explorer', 'parent_node': parent_node,
                   'size': str(file_stat.st_size)}
        headers = self._make_auth_header(access_token)
        result = request("POST", prepare_url, headers=headers, data=payload)
        upload_id = result['data']['upload_id']
        block_num = result['data']['block_num']
        block_size = result['data']['block_size']
        blocks = cal_file_block(file_path)
        for index, block in enumerate(blocks):
            payload = {'upload_id': upload_id, 'seq': str(
                index), 'size': str(len(block)), 'file': block}
            multi_form = MultipartEncoder(payload)
            part_headers = headers.copy()
            part_headers['Content-Type'] = multi_form.content_type
            response = request("POST", part_url, part_headers, data=multi_form)
        finish_payload = {'upload_id': upload_id, 'block_num': block_num}
        response = request("POST", finish_url, headers, data=finish_payload)
        return response['data']

    def add_work_sheet(self, access_token, spreadsheet_token, title, index=0):
        """在电子表格中添加新的工作表"""
        url = self._host + \
            f"/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/sheets_batch_update"
        headers = self._make_json_headers(access_token)
        payload = {"requests": [
            {"addSheet": {"properties": {"title": title, "index": index}}}]}
        resp = request("POST", url, headers, payload)
        return resp['data']


# 单例实例
one_client = Client()
