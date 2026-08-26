FROM python:3.11-slim

RUN sed -i 's|http://deb.debian.org/debian$|http://mirror.nju.edu.cn/debian|' \
        /etc/apt/sources.list.d/debian.sources

RUN apt-get -o Acquire::ForceIPv4=true -o Acquire::http::Timeout=30 update \
    && apt-get install -y --no-install-recommends bash git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir \
    pytest \
    pytest-asyncio \
    pytest-cov

WORKDIR /workspace
