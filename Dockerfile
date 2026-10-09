# syntax=docker/dockerfile:1
#
# Shairport Sync (AirPlay 2, ALSA output, no volume processing) + volume bridge
# that forwards AirPlay volume to an external amplifier and reports the
# amplifier's volume back to the AirPlay source.
#
# Shairport Sync is pinned to a commit on its `development` branch because that
# branch is where AirPlay 2 remote volume control lives (sending a volume change
# from the receiver back to the AirPlay 2 source) and where `ignore_volume_control`
# no longer hides the volume slider on the source.

ARG ALPINE_VERSION=3.23.4
ARG FFMPEG_BRANCH=release/8.1
# nqptp development branch: provides shared-memory interface (SMI) version 11,
# which this Shairport Sync commit requires (nqptp main only provides 10).
ARG NQPTP_REF=0a88cd7ca9325f023ca5d891714695167672bdb9
ARG SHAIRPORT_SYNC_REF=b45f15b32882409a7d39b01293d43da3d5519416

##### FFmpeg (decoders only: AAC + ALAC) #####
FROM alpine:${ALPINE_VERSION} AS ffmpeg
RUN apk -U add build-base git nasm pkgconf
ARG FFMPEG_BRANCH
RUN git clone --depth=1 -b "${FFMPEG_BRANCH}" https://github.com/FFmpeg/FFmpeg /src/ffmpeg
WORKDIR /src/ffmpeg
RUN ./configure \
      --prefix=/ffmpeg \
      --extra-libs="-lpthread -lm" \
      --ld="g++" \
      --disable-static --enable-shared \
      --disable-programs --disable-doc --disable-everything \
      --disable-iconv --disable-avdevice --disable-avfilter --disable-swscale \
      --disable-network --disable-iamf --disable-pixelutils \
      --enable-decoder=alac --enable-decoder=aac \
      --enable-small --extra-cflags="-Os" \
 && make -j"$(nproc)" \
 && make install

##### NQPTP (PTP timing companion required for AirPlay 2) #####
FROM alpine:${ALPINE_VERSION} AS nqptp
RUN apk -U add autoconf automake build-base git linux-headers musl-dev pkgconf
ARG NQPTP_REF
WORKDIR /src/nqptp
RUN git init -q . \
 && git remote add origin https://github.com/mikebrady/nqptp.git \
 && git fetch -q --depth=1 origin "${NQPTP_REF}" \
 && git checkout -q FETCH_HEAD \
 && autoreconf -i && ./configure && make -j"$(nproc)"

##### Shairport Sync #####
FROM alpine:${ALPINE_VERSION} AS shairport-sync
RUN apk -U add \
      alsa-lib-dev autoconf automake avahi-dev build-base dbus git glib-dev \
      libconfig-dev libgcrypt-dev libplist-dev libplist-util libsodium-dev libtool \
      openssl-dev popt-dev pkgconf util-linux-dev xxd
COPY --from=ffmpeg /ffmpeg/lib/ /usr/local/lib/
COPY --from=ffmpeg /ffmpeg/include/ /usr/local/include/
ARG SHAIRPORT_SYNC_REF
WORKDIR /src/shairport-sync
RUN git init -q . \
 && git remote add origin https://github.com/mikebrady/shairport-sync.git \
 && git fetch -q --depth=1 origin "${SHAIRPORT_SYNC_REF}" \
 && git checkout -q FETCH_HEAD
WORKDIR /src/shairport-sync/build
# Deliberately minimal: ALSA output only, no soxr, no convolution, no loudness DSP.
# The D-Bus interface is what the volume bridge talks to.
RUN autoreconf -i .. \
 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ../configure \
      --sysconfdir=/etc \
      --with-airplay-2 \
      --with-alsa \
      --with-avahi \
      --with-ssl=openssl \
      --with-ffmpeg \
      --with-metadata \
      --with-dbus-interface \
 && make -j"$(nproc)" \
 && DESTDIR=/install make install

##### Volume bridge (Python) #####
FROM alpine:${ALPINE_VERSION} AS volume-bridge
RUN apk -U add python3 py3-pip
COPY volume_bridge /src/volume_bridge
RUN python3 -m venv /opt/volume-bridge \
 && /opt/volume-bridge/bin/pip install --no-cache-dir /src/volume_bridge

##### Runtime #####
FROM alpine:${ALPINE_VERSION}
RUN apk -U --no-cache add \
      alsa-lib alsa-utils avahi bash dbus glib libconfig libgcrypt libplist \
      libcrypto3 libsodium libuuid popt python3 su-exec tini \
 && rm -f /etc/avahi/services/*.service \
 && addgroup -S shairport-sync \
 && adduser -S -D -H -G shairport-sync shairport-sync \
 && addgroup -g 29 docker_audio 2>/dev/null || true \
 && addgroup shairport-sync docker_audio \
 && addgroup shairport-sync audio \
 && mkdir -p /run/dbus

COPY --from=ffmpeg /ffmpeg/lib/ /usr/local/lib/
COPY --from=nqptp /src/nqptp/nqptp /usr/local/bin/nqptp
COPY --from=shairport-sync /install/usr/local/bin/shairport-sync /usr/local/bin/shairport-sync
COPY --from=shairport-sync /install/etc/dbus-1/system.d/shairport-sync-dbus.conf /etc/dbus-1/system.d/
COPY --from=volume-bridge /opt/volume-bridge /opt/volume-bridge

COPY config/shairport-sync.conf /etc/shairport-sync.conf
COPY config/volume-bridge.toml /etc/volume-bridge.toml
COPY --chmod=755 rootfs/usr/local/bin/entrypoint.sh /usr/local/bin/entrypoint.sh

ENV ENABLE_AVAHI=1 \
    ENABLE_NQPTP=1 \
    ENABLE_VOLUME_BRIDGE=1 \
    VOLUME_BRIDGE_CONFIG=/etc/volume-bridge.toml

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
