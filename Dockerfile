FROM mikebrady/shairport-sync:latest

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
