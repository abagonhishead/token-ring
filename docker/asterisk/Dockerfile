# Asterisk LTS built from source (there's no official image), with the token-ring config.
# Build from the repository root:  docker build -f docker/asterisk/Dockerfile -t token-ring-asterisk .
# Your own image: FROM this, COPY your asterisk configs over /etc/asterisk.
ARG DEBIAN=trixie

FROM debian:${DEBIAN}-slim AS build
ARG ASTERISK_VERSION=22.11.0
# Core sounds to install, as menuselect names (e.g. CORE-SOUNDS-EN_GB-WAV for British English)
ARG SOUNDS="CORE-SOUNDS-EN-WAV CORE-SOUNDS-EN-G722"
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl bzip2 patch pkg-config python3 \
        libedit-dev libjansson-dev libsqlite3-dev uuid-dev libxml2-dev libssl-dev libsrtp2-dev \
        libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl -fsSL https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${ASTERISK_VERSION}.tar.gz \
        | tar xz --strip-components=1
RUN ./configure --with-jansson-bundled=no --with-pjproject-bundled --libdir=/usr/lib >/dev/null \
    && make menuselect.makeopts >/dev/null \
    && menuselect/menuselect --disable CORE-SOUNDS-EN-GSM $(for s in ${SOUNDS}; do printf -- '--enable %s ' "$s"; done) \
        --enable res_audiosocket --enable chan_audiosocket --enable app_audiosocket \
        --enable app_echo --enable codec_g722 --disable BUILD_NATIVE menuselect.makeopts \
    && make -j"$(nproc)" >/tmp/make.log 2>&1 || { tail -50 /tmp/make.log; exit 1; }
RUN make install DESTDIR=/out >/tmp/install.log 2>&1 || { tail -50 /tmp/install.log; exit 1; } \
    && mkdir -p /out/etc/asterisk \
    && cp configs/samples/indications.conf.sample /out/etc/asterisk/indications.conf \
    && rm -rf /out/usr/include \
    && if [ -d /out/var/run ]; then mkdir -p /out/run && cp -a /out/var/run/. /out/run/ && rm -rf /out/var/run; fi

FROM debian:${DEBIAN}-slim
# The bridge writes call files into /var/spool/asterisk/outgoing, so both run as this uid (and
# gid, which is what the secrets must be readable by)
ARG UID=1000
RUN apt-get update && apt-get install -y --no-install-recommends \
        libedit2 libjansson4 libsqlite3-0 libuuid1 libxml2 libssl3t64 libsrtp2-1 libcurl4t64 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid ${UID} asterisk \
    && useradd --system --uid ${UID} --gid ${UID} --home-dir /var/lib/asterisk asterisk
COPY --from=build /out/ /
COPY asterisk/ /etc/asterisk/
# Empty stubs for modules that otherwise log about missing configs.
# Already running as asterisk: no need to switch user
RUN sed -i '/^run\(user\|group\)/d' /etc/asterisk/asterisk.conf \
    && for f in acl aeap ari ccss cdr cel chan_websocket cli_aliases features musiconhold pjproject pjsip_notify pjsip_wizard \
        res_http_media_cache res_stun_monitor stasis udptl voicemail websocket_client; do \
        test -e /etc/asterisk/$f.conf || : > /etc/asterisk/$f.conf; done \
    && mkdir -p /var/spool/asterisk/outgoing /var/spool/asterisk/token-ring /var/lib/asterisk/db /run/asterisk /var/log/asterisk \
    && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk /run/asterisk /var/log/asterisk
USER asterisk
# Keep /var/lib/asterisk/db (registrations) on a volume, or phones are forgotten on restart until
# they register again. SIP (5060 UDP/TCP) and RTP (10000-10999 UDP, rtp.conf): host networking
CMD ["asterisk", "-f", "-vvv"]
