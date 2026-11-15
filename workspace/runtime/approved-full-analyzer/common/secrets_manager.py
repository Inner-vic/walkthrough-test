"""
敏感信息管理器

优先级: 环境变量 > secrets.json > 旧的硬编码值（兼容）
"""

import os
import json
from pathlib import Path
from typing import Optional
import warnings


class SecretsManager:
    """管理敏感配置信息"""

    def __init__(self, secrets_file: Optional[str] = None):
        self.secrets_file = secrets_file or self._find_secrets_file()
        self._secrets = None

    def _find_secrets_file(self) -> Optional[str]:
        """查找 secrets.json 文件"""
        # 在项目根目录查找
        root_dir = Path(__file__).parent.parent
        secrets_path = root_dir / 'secrets.json'

        if secrets_path.exists():
            return str(secrets_path)

        return None

    def _load_secrets(self) -> dict:
        """加载敏感配置"""
        if self._secrets is not None:
            return self._secrets

        self._secrets = {}

        if self.secrets_file and os.path.exists(self.secrets_file):
            try:
                with open(self.secrets_file, 'r', encoding='utf-8') as f:
                    self._secrets = json.load(f)
            except Exception as e:
                warnings.warn(f"无法加载 secrets.json: {e}")

        return self._secrets

    def get(self, *keys, default=None, fallback=None):
        """
        获取敏感配置

        优先级: 环境变量 > secrets.json > fallback

        Args:
            *keys: 配置键路径，如 ('feishu', 'app_id')
            default: 环境变量的默认值
            fallback: 当所有方式都失败时的回退值（用于向后兼容）

        Returns:
            配置值
        """
        # 1. 尝试从环境变量获取
        env_key = '_'.join(keys).upper()
        env_value = os.getenv(env_key, default)
        if env_value:
            return env_value

        # 2. 尝试从 secrets.json 获取
        secrets = self._load_secrets()
        value = secrets
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
                break

        if value:
            return value

        # 3. 使用回退值（向后兼容）
        if fallback:
            warnings.warn(
                f"未找到配置 {'.'.join(keys)}，使用硬编码回退值。"
                f"建议设置环境变量 {env_key} 或配置 secrets.json",
                DeprecationWarning,
                stacklevel=2
            )
            return fallback

        raise ValueError(
            f"未找到配置: {'.'.join(keys)}。"
            f"请设置环境变量 {env_key} 或创建 secrets.json 文件"
        )


# 全局单例
_secrets_manager = SecretsManager()


def get_secret(*keys, default=None, fallback=None):
    """快捷函数：获取敏感配置"""
    return _secrets_manager.get(*keys, default=default, fallback=fallback)
