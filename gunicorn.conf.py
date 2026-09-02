# gunicorn 配置（参照 AutoTrade）
import multiprocessing

bind = "0.0.0.0:16000"
workers = 2
threads = 4
worker_class = "gthread"
timeout = 120
graceful_timeout = 30
keepalive = 5
max_requests = 2000
max_requests_jitter = 200

# 日志
accesslog = "-"
errorlog = "-"
loglevel = "info"
