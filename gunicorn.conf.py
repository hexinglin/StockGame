# gunicorn 配置（参照 AutoTrade）
import multiprocessing

bind = "0.0.0.0:16000"
# 必须单 worker：游戏引擎（轮次内存态、0.1s 时钟、撮合）是进程内状态，
# 无跨进程锁与广播。多 worker 会各自注册并运行时钟，轮次上下文分裂
# （WS 收不到行情推送、HTTP 报"上下文未初始化"）。并发由 threads 提供；
# 若要多 worker 需先做 Redis pub/sub + 分布式锁重构。
workers = 1
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
