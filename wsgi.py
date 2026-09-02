"""gunicorn 入口"""
import os

os.environ.setdefault("STOCKGAME_CONFIG", "config.yaml")

from app.main import create_app

app = create_app()
