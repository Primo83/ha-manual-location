FROM ghcr.io/home-assistant/base-python@sha256:51837e17a94b578cc00357c98482b61b53b27e37d910c97b86dd6448cfb11a6f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apk add --no-cache nginx=1.30.4-r1 openssl=3.5.9-r0
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
COPY gateway /opt/manual-location/gateway
COPY rootfs /
WORKDIR /opt/manual-location
RUN chmod 755 /etc/s6-overlay/s6-rc.d/init-manual-location/run \
    /etc/s6-overlay/s6-rc.d/gateway/run /etc/s6-overlay/s6-rc.d/gateway/finish \
    /etc/s6-overlay/s6-rc.d/nginx/run /etc/s6-overlay/s6-rc.d/nginx/finish
