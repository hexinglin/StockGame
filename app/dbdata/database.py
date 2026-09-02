"""
模块名称: dbdata/database.py
说明:    数据库初始化 — 提供 db 实例和 init_db 入口
         ORM 模型定义在 models/ 目录下各文件中
"""
import logging

from flask_sqlalchemy import SQLAlchemy

from ..utils.config import Config

logger = logging.getLogger(__name__)


db = SQLAlchemy()


class Base(db.Model):
    """ORM 模型基类 — 继承 db.Model 以获得 .query 快捷查询"""
    __abstract__ = True


def init_db(app):
    """初始化数据库

    Args:
        app: Flask 应用实例
    """
    # 导入所有模型，确保它们注册到 SQLAlchemy
    from . import models  # noqa: F401

    config = Config.get_instance()
    db_type = config.get("database.type", "postgresql")

    if db_type == "postgresql":
        dsn = config.get("database.path", "")
        if not dsn:
            raise RuntimeError(
                "未配置数据库连接。请在 config.yaml 中配置 database.path，"
                "如: postgresql://user:pass@host:5432/stockgame"
            )
        app.config["SQLALCHEMY_DATABASE_URI"] = dsn
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_size": 10,
            "pool_recycle": 300,
        }
    else:
        raise RuntimeError(f"不支持的数据库类型: {db_type}，仅支持 postgresql")

    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        # 引擎跨 app context 持有 ORM 实例（轮次/订单），commit 后对象不得过期
        db.session.configure(expire_on_commit=False)
        db.create_all()
        logger.info(f"数据库初始化完成 (type={db_type})")
