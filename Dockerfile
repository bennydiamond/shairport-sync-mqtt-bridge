ARG SHAIRPORT_VERSION=5.6.2
FROM mikebrady/shairport-sync:${SHAIRPORT_VERSION}

ARG SHAIRPORT_VERSION=5.6.2
ARG BUILD_INDEX=1
LABEL org.opencontainers.image.version="${SHAIRPORT_VERSION}-${BUILD_INDEX}" \
      io.shairport-sync.base-version="${SHAIRPORT_VERSION}" \
      io.shairport-sync.build-index="${BUILD_INDEX}"

RUN apk add --no-cache \
        python3 \
        py3-paho-mqtt \
        py3-dbus \
        py3-gobject3 \
        alsa-utils \
        alsa-lib \
        gettext \
    && apk add --no-cache --virtual .build-deps \
        py3-pip \
        python3-dev \
        alsa-lib-dev \
        gcc \
        musl-dev \
    && pip3 install --no-cache-dir --break-system-packages pyalsaaudio \
    && apk del .build-deps

COPY shairport-sync.conf.template /etc/shairport-sync.conf.template
COPY volume_bridge.py /usr/local/bin/volume_bridge.py
COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh /usr/local/bin/volume_bridge.py

ENTRYPOINT ["/usr/local/bin/start.sh"]
