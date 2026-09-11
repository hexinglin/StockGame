"""
模块名称: messaging/cache.py
说明:    Redis 缓存层（复用现有实例 db=5）
         Redis 不可用时降级为无缓存模式，不影响核心业务。
"""
import json
import logging
import time

from ..utils.config import Config

logger = logging.getLogger(__name__)

# 账户 Redis Key
ACCT_KEY_PREFIX = "game:acct"
# 轮次进度
ROUND_PROGRESS_PREFIX = "game:round"
# 心跳
HEARTBEAT_LAST_PREFIX = "heartbeat:last"
HEARTBEAT_ALERT_PREFIX = "heartbeat:alert"
# 最新行情（全局实时 + 轮次内）
QUOTE_LIVE_KEY = "game:quote:live"
QUOTE_ROUND_PREFIX = "game:quote"
# 用户配置（网格等参数，可写覆盖，Redis 优先于 config.yaml 默认值）
CFG_KEY_PREFIX = "game:cfg"
# 最新上传记录（QMT 上传的最近一条行情快照）
AGENT_LATEST_KEY = "agent:latest_upload"
# QMT 交易记录拉取：命令单命令槽（TTL 到期自动失效）
# 结果数据不在此缓存 — 持久化在 PostgreSQL（engine/trade_store.py）
TRADE_CMD_KEY = "agent:trade_fetch_cmd"
# 命令有效期：下发后 2 分钟自动失效（Redis TTL 到期即删除）
TRADE_CMD_TTL_SEC = 2 * 60

_TTL_ACCT = 86400 * 30       # 账户 30 天
_TTL_QUOTE = 86400 * 7       # 行情快照 7 天
_TTL_LATEST = 25 * 3600      # 最新上传记录 25 小时


class RedisCache:
    """Redis 缓存客户端"""

    def __init__(self, config: Config = None):
        self._config = config or Config.get_instance()
        self._client = None
        self._enabled = self._config.get("redis.enabled", True)

    def connect(self):
        """连接 Redis（懒加载）"""
        if not self._enabled:
            logger.info("Redis 缓存已禁用")
            return
        if self._client is not None:
            return
        try:
            import redis
            host = self._config.get("redis.host", "localhost")
            port = self._config.get("redis.port", 6379)
            db = self._config.get("redis.db", 0)
            password = self._config.get("redis.password", None) or None
            self._client = redis.Redis(
                host=host, port=port, db=db,
                password=password, decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            self._client.ping()
            logger.info(f"Redis 连接成功: {host}:{port}/{db}")
        except ImportError:
            logger.warning("redis 模块未安装，缓存降级为无缓存模式")
            self._enabled = False
        except Exception as e:
            logger.warning(f"Redis 连接失败: {e}，降级为无缓存模式")
            self._client = None
            self._enabled = False

    @property
    def available(self) -> bool:
        return self._enabled and self._client is not None

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    # ── 账户快照（按轮次） ──

    def save_account(self, round_id, acct_dict: dict):
        """保存轮次账户快照"""
        if not self.available:
            return
        try:
            self._client.setex(
                f"{ACCT_KEY_PREFIX}:{round_id}", _TTL_ACCT,
                json.dumps(acct_dict),
            )
        except Exception as e:
            logger.warning("账户保存失败: %s", e)

    def load_account(self, round_id):
        """加载轮次账户快照，无则返回 None"""
        if not self.available:
            return None
        try:
            raw = self._client.get(f"{ACCT_KEY_PREFIX}:{round_id}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("账户加载失败: %s", e)
        return None

    def delete_account(self, round_id):
        """删除轮次账户"""
        if not self.available:
            return
        try:
            self._client.delete(f"{ACCT_KEY_PREFIX}:{round_id}")
        except Exception as e:
            logger.warning("账户删除失败: %s", e)

    # ── 轮次进度 ──

    def save_progress(self, round_id, index: int):
        if not self.available:
            return
        try:
            self._client.setex(f"{ROUND_PROGRESS_PREFIX}:{round_id}:index", _TTL_ACCT, index)
        except Exception as e:
            logger.warning("进度保存失败: %s", e)

    def load_progress(self, round_id) -> int:
        if not self.available:
            return 0
        try:
            raw = self._client.get(f"{ROUND_PROGRESS_PREFIX}:{round_id}:index")
            return int(raw) if raw else 0
        except Exception as e:
            logger.warning("进度加载失败: %s", e)
        return 0

    def delete_progress(self, round_id):
        if not self.available:
            return
        try:
            self._client.delete(f"{ROUND_PROGRESS_PREFIX}:{round_id}:index")
        except Exception as e:
            logger.warning("进度删除失败: %s", e)

    # ── 心跳 ──

    def set_heartbeat(self, agent_name: str, ts: float = None):
        """记录心跳时间戳（unix 秒）"""
        if not self.available:
            return
        try:
            self._client.set(
                f"{HEARTBEAT_LAST_PREFIX}:{agent_name}",
                ts if ts is not None else time.time(),
            )
        except Exception as e:
            logger.warning("心跳写入失败: %s", e)

    def get_heartbeat(self, agent_name: str) -> float:
        """读取最后心跳时间戳，无则 0"""
        if not self.available:
            return 0
        try:
            raw = self._client.get(f"{HEARTBEAT_LAST_PREFIX}:{agent_name}")
            return float(raw) if raw else 0
        except Exception as e:
            logger.warning("心跳读取失败: %s", e)
        return 0

    def delete_heartbeat(self, agent_name: str):
        """删除心跳时间戳（agent 下线/测试清理用）"""
        if not self.available:
            return
        try:
            self._client.delete(f"{HEARTBEAT_LAST_PREFIX}:{agent_name}")
        except Exception as e:
            logger.warning("心跳删除失败: %s", e)

    def set_alert(self, agent_name: str, ttl: int = 1800):
        """设置告警防抖标记"""
        if not self.available:
            return
        try:
            self._client.setex(f"{HEARTBEAT_ALERT_PREFIX}:{agent_name}", ttl, "1")
        except Exception as e:
            logger.warning("告警标记写入失败: %s", e)

    def has_alert(self, agent_name: str) -> bool:
        if not self.available:
            return False
        try:
            return bool(self._client.exists(f"{HEARTBEAT_ALERT_PREFIX}:{agent_name}"))
        except Exception as e:
            logger.warning("告警标记读取失败: %s", e)
        return False

    def clear_alert(self, agent_name: str):
        if not self.available:
            return
        try:
            self._client.delete(f"{HEARTBEAT_ALERT_PREFIX}:{agent_name}")
        except Exception as e:
            logger.warning("告警标记删除失败: %s", e)

    # ── 最新行情快照 ──

    def save_quote(self, key: str, quote: dict):
        """保存最新行情快照，key 如 'live' 或 round_id"""
        if not self.available:
            return
        try:
            prefix = QUOTE_LIVE_KEY if key == "live" else f"{QUOTE_ROUND_PREFIX}:{key}"
            self._client.setex(prefix, _TTL_QUOTE, json.dumps(quote))
        except Exception as e:
            logger.warning("行情快照保存失败: %s", e)

    def load_quote(self, key: str):
        if not self.available:
            return None
        try:
            prefix = QUOTE_LIVE_KEY if key == "live" else f"{QUOTE_ROUND_PREFIX}:{key}"
            raw = self._client.get(prefix)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("行情快照加载失败: %s", e)
        return None

    def delete_quote(self, key: str):
        if not self.available:
            return
        try:
            prefix = QUOTE_LIVE_KEY if key == "live" else f"{QUOTE_ROUND_PREFIX}:{key}"
            self._client.delete(prefix)
        except Exception as e:
            logger.warning("行情快照删除失败: %s", e)

    # ── 最新上传记录（按 Agent 分键 + 全局最新） ──

    def save_latest_upload(self, record: dict):
        """保存 QMT 最新上传的行情快照记录（25 小时过期）

        record 需携带 agent_name：按 Agent 存 agent:latest_upload:{name}
        （供监控面板查看各 Agent 的最近上传），同时覆盖全局键（保留
        "最近一次上传"语义，兼容旧查询）。
        """
        if not self.available:
            return
        try:
            raw = json.dumps(record)
            name = str(record.get("agent_name") or "").strip()
            if name:
                self._client.setex(f"{AGENT_LATEST_KEY}:{name}",
                                   _TTL_LATEST, raw)
            self._client.setex(AGENT_LATEST_KEY, _TTL_LATEST, raw)
        except Exception as e:
            logger.warning("最新上传记录保存失败: %s", e)

    def load_latest_upload(self, agent_name: str = None):
        """读取最新上传记录：指定 agent_name → 该 Agent 最近一条；
        否则全局最近一条（任何 Agent 的最后一次上传）；无则 None"""
        if not self.available:
            return None
        try:
            key = f"{AGENT_LATEST_KEY}:{agent_name}" if agent_name else AGENT_LATEST_KEY
            raw = self._client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("最新上传记录读取失败: %s", e)
        return None

    def has_latest_upload(self, agent_name: str) -> bool:
        """该 Agent 是否有最近上传记录（监控面板据此显示"上传记录"入口）"""
        if not self.available:
            return False
        try:
            return bool(self._client.exists(f"{AGENT_LATEST_KEY}:{agent_name}"))
        except Exception as e:
            logger.warning("最新上传记录检查失败: %s", e)
            return False

    def delete_latest_upload(self, agent_name: str = None):
        """删除上传记录：指定 agent_name → 删该 Agent 分键；缺省删全局键
        （删除 Agent / 测试清理用）"""
        if not self.available:
            return
        try:
            key = f"{AGENT_LATEST_KEY}:{agent_name}" if agent_name else AGENT_LATEST_KEY
            self._client.delete(key)
        except Exception as e:
            logger.warning("最新上传记录删除失败: %s", e)

    # ── QMT 交易记录拉取命令（结果落库，见 engine/trade_store.py） ──

    def save_trade_fetch_cmd(self, cmd: dict):
        """写入交易记录拉取命令（单命令槽，TTL 2 分钟自动失效）

        再次下发直接覆盖旧命令；Agent 每 10s 直读该键；结果上报后由
        delete_trade_fetch_cmd 删除；超时未执行则 Redis TTL 到期自动消失。
        """
        if not self.available:
            return
        try:
            cmd.setdefault("ts", time.time())
            self._client.setex(TRADE_CMD_KEY, TRADE_CMD_TTL_SEC,
                               json.dumps(cmd, ensure_ascii=False))
        except Exception as e:
            logger.warning("交易拉取命令写入失败: %s", e)

    def load_trade_fetch_cmd(self):
        """读取当前命令（直读 Redis 单键），无则 None"""
        if not self.available:
            return None
        try:
            raw = self._client.get(TRADE_CMD_KEY)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("交易拉取命令读取失败: %s", e)
        return None

    def delete_trade_fetch_cmd(self, cmd_id: str = ""):
        """删除命令（结果上报后调用）；cmd_id 匹配时才删，
        防止误删上报期间用户重新下发、已覆盖的新命令
        """
        if not self.available:
            return
        try:
            if cmd_id:
                cmd = self.load_trade_fetch_cmd()
                if cmd and cmd.get("cmd_id") != cmd_id:
                    return
            self._client.delete(TRADE_CMD_KEY)
        except Exception as e:
            logger.warning("交易拉取命令删除失败: %s", e)

    def reset_trade_fetch_commands(self):
        """清空命令（测试/维护辅助）"""
        if not self.available:
            return
        try:
            self._client.delete(TRADE_CMD_KEY)
        except Exception as e:
            logger.warning("交易拉取命令重置失败: %s", e)

    # ── 用户配置（可写覆盖） ──

    def save_config(self, namespace: str, cfg: dict):
        """保存用户配置命名空间（如 grid），写入 Redis（TTL 1 年）"""
        if not self.available:
            return
        try:
            self._client.setex(f"{CFG_KEY_PREFIX}:{namespace}", 86400 * 365, json.dumps(cfg))
        except Exception as e:
            logger.warning("配置保存失败 namespace=%s: %s", namespace, e)

    def load_config(self, namespace: str):
        """读取用户配置命名空间，无则返回 None"""
        if not self.available:
            return None
        try:
            raw = self._client.get(f"{CFG_KEY_PREFIX}:{namespace}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.warning("配置读取失败 namespace=%s: %s", namespace, e)
        return None


# 全局单例
_cache: "RedisCache" = None


def get_cache() -> "RedisCache":
    """获取全局缓存实例"""
    global _cache
    if _cache is None:
        _cache = RedisCache()
    return _cache
