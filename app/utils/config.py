"""
模块名称: config.py
说明:    配置管理器，YAML + 单例模式，支持环境变量引用（参照 AutoTrade）
"""
import os
import yaml


class Config:
    """配置管理器（单例模式），支持嵌套键访问和环境变量引用"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config = {}
            cls._instance._initialized = False
        return cls._instance

    def init(self, path: str = None):
        """初始化配置，从 YAML 文件加载"""
        if self._initialized:
            return
        if path is None:
            path = os.environ.get("STOCKGAME_CONFIG", "config.yaml")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        self._apply_env_overrides()
        self._initialized = True

    def _apply_env_overrides(self):
        """支持 STOCKGAME__KEY__SUBKEY 格式的环境变量覆盖（自动类型转换）"""
        prefix = "STOCKGAME__"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                parts = key[len(prefix):].lower().split("__")
                target = self._config
                for part in parts[:-1]:
                    if part not in target:
                        target[part] = {}
                    target = target[part]
                target[parts[-1]] = self._coerce(value)

    @staticmethod
    def _coerce(value: str):
        """字符串转原生类型：数字 → int/float，true/false → bool，其余保持原样"""
        v = value.strip()
        low = v.lower()
        if low in ("true", "false"):
            return low == "true"
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            return v

    def get(self, key: str, default=None):
        """获取配置项，支持嵌套键如 'database.path'"""
        parts = key.split(".")
        target = self._config
        for part in parts:
            if isinstance(target, dict):
                target = target.get(part)
                if target is None:
                    return default
            else:
                return default
        return target

    def all(self) -> dict:
        """返回全部配置"""
        return self._config

    @classmethod
    def get_instance(cls, path: str = None) -> "Config":
        """获取全局单例，首次调用时自动初始化"""
        inst = cls()
        if not inst._initialized:
            inst.init(path)
        return inst
