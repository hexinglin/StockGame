"""
模块名称: main.py
说明:    应用工厂 — 初始化 DB/Redis/APScheduler/Socket.IO，注册蓝图
"""
import logging
import os
import threading

import simple_websocket

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO

# ── WS 帧写串行化（Flask-SocketIO threading 模式的库级缺陷修复）──
# threading 模式下服务端多线程并发写同一 WebSocket 连接：时钟推送线程
# （引擎 0.1s 周期 emit）、请求线程、WS 读线程（engineio 心跳 pong/事件
# 回包）都可能同时调用 simple_websocket.Server.send。该方法内部"编码帧 +
# 写 socket"两步无锁，并发会交错帧字节，客户端报 "Invalid frame header"
# 并断开连接。此处给 send 加全局锁，把全部帧写出串行化（锁粒度为微秒级，
# 对 0.1s 级推送无感知）。必须在任何连接建立前生效，故置于模块顶层。
_ws_send_lock = threading.Lock()
_ws_send_orig = simple_websocket.Server.send


def _ws_send_locked(self, data):
    with _ws_send_lock:
        return _ws_send_orig(self, data)


simple_websocket.Server.send = _ws_send_locked

from .utils.config import Config
from .utils.logger import setup_logging

logger = logging.getLogger(__name__)

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")


def create_app(config_path: str = None, enable_scheduler: bool = True) -> Flask:
    """应用工厂

    Args:
        config_path: 配置文件路径
        enable_scheduler: 是否启用 APScheduler（测试时传 False 手动控制时钟）
    """
    cfg = Config.get_instance(config_path or os.environ.get("STOCKGAME_CONFIG", "config.yaml"))
    setup_logging(level=cfg.get("app.log_level", "INFO"))

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "stockgame-secret"
    CORS(app)

    # 数据库
    from .dbdata.database import init_db
    init_db(app)

    # Redis 缓存
    from .messaging.cache import get_cache
    get_cache().connect()

    # 蓝图
    from .api.agent_routes import agent_bp
    from .api.game_routes import game_bp
    app.register_blueprint(agent_bp)
    app.register_blueprint(game_bp)

    # 健康检查
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "app": "StockGame"})

    # 静态页（前端单页）
    @app.route("/")
    def index():
        return send_from_directory("static", "index.html")

    # Socket.IO
    socketio.init_app(app)
    from .api.socket_events import register_socket_events
    register_socket_events(socketio)

    # 游戏引擎注入推送器
    from .engine.game_engine import get_engine
    engine = get_engine()
    engine.init_app(app)
    engine.set_emitter(
        lambda event, data, room=None: socketio.emit(event, data, room=room)
    )

    # 调度器：心跳检查 + 游戏时钟
    scheduler = None
    if enable_scheduler:
        from flask_apscheduler import APScheduler
        scheduler = APScheduler()
        scheduler.init_app(app)
        scheduler.start()

        from .engine.heartbeat import register_heartbeat_checker
        from .engine.game_engine import register_game_clock
        register_heartbeat_checker(scheduler, app)
        register_game_clock(scheduler)

    # 后端重启恢复：running → paused（进度在 Redis，可手动继续）
    _recover_running_rounds(app)

    logger.info("StockGame 应用启动完成 host=%s port=%s",
                cfg.get("app.host", "0.0.0.0"), cfg.get("app.port", 16000))
    return app


def _recover_running_rounds(app):
    """后端重启后，将 running 轮次置为 paused（进度在 Redis 可续）"""
    try:
        with app.app_context():
            from .dbdata.models import GameRound
            from .dbdata.database import db
            rows = GameRound.query.filter(GameRound.status == "running").all()
            for r in rows:
                r.status = "paused"
                logger.info("重启恢复: 轮次 %s running → paused", r.id)
            if rows:
                db.session.commit()
    except Exception as e:
        logger.warning("恢复 running 轮次失败: %s", e)
