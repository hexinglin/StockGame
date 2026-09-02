"""开发环境启动入口: python run.py"""
import os

os.environ.setdefault("STOCKGAME_CONFIG", "config.yaml")

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=16000, debug=True)
