FROM python:3.13-slim AS litestream
ARG LITESTREAM_VERSION=0.5.18
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-x86_64.tar.gz /tmp/litestream.tar.gz
RUN mkdir /tmp/ls && tar -xzf /tmp/litestream.tar.gz -C /tmp/ls \
    && find /tmp/ls -type f -name litestream -exec install -m 0755 {} /usr/local/bin/litestream \; \
    && /usr/local/bin/litestream version

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/farsi.db
WORKDIR /srv
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY litestream.yml /etc/litestream.yml
COPY entrypoint.sh /entrypoint.sh
COPY app ./app
RUN chmod +x /entrypoint.sh && mkdir -p /data
CMD ["/entrypoint.sh"]
