FROM python:3.12-slim

# git 是硬依赖：diff 靠本地 clone + git diff 生成
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/
COPY requirements.txt /app/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 默认起 webhook 服务；也可以覆盖成 ["python","-m","src.cli","check"]
EXPOSE 8080
CMD ["python", "-m", "src.server"]
