FROM python:3.10-slim-buster

WORKDIR /usr/src/app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt

COPY . .