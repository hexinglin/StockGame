"""开发环境启动入口: python run.py"""
import os

os.environ.setdefault("STOCKGAME_CONFIG", "config.yaml")

from app.main import create_app, socketio

app = create_app()

if __name__ == "__main__":
    # 用 socketio.run 而非 app.run：Flask-SocketIO threading 模式需由其接管
    # WebSocket 升级；显式关闭 reloader —— debug 自动重载与长连接不兼容
    # （改代码即重启会掐断 WS 连接，且 reload 过程易致服务 HTTP 层半死），
    # 开发中改代码后手动重启即可（PyCharm 重跑 Run 配置）
    socketio.run(app, host="0.0.0.0", port=16000,
                 debug=False, use_reloader=False)
