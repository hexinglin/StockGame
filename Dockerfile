FROM python:3.11-slim

WORKDIR /app

# ── 国内源注入（默认清华镜像，可用 --build-arg 覆盖）──
ARG DEBIAN_MIRROR="mirrors.tuna.tsinghua.edu.cn"
ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"

# 切换 Debian apt 源为国内镜像（兼容 deb822 与旧版 sources.list 两种格式）
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g; s|security.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g; s|security.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list; \
    fi

# pip 国内源（pip 原生读取该环境变量）
ENV PIP_INDEX_URL="${PIP_INDEX_URL}"

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:16000/health')"

EXPOSE 16000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
