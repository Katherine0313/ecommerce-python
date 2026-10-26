FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# 单进程即可；SSE 依赖长连接，worker 数按机器内存再调
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
