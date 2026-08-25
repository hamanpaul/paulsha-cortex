FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ENV container=docker

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        acl \
        bubblewrap \
        ca-certificates \
        curl \
        gh \
        git \
        jq \
        nodejs \
        polkitd \
        python3 \
        python3-pip \
        python3-venv \
        socat \
        sudo \
        systemd \
        systemd-sysv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask \
        dev-hugepages.mount \
        sys-fs-fuse-connections.mount \
        systemd-remount-fs.service \
        getty.target \
        console-getty.service

COPY validate.py /usr/local/libexec/cortex-qualification-validate
COPY verify_bundle.py /usr/local/libexec/cortex-qualification-verify-bundle
COPY driver.py /usr/local/libexec/cortex-release-qualification
COPY qualification.schema.json /usr/local/share/cortex/qualification.schema.json

RUN chmod 0755 \
        /usr/local/libexec/cortex-qualification-validate \
        /usr/local/libexec/cortex-qualification-verify-bundle \
        /usr/local/libexec/cortex-release-qualification

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/sbin/init"]
