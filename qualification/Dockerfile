FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ENV container=docker

# `name=version` 的列逐字鏡射 qualification/contract.py 的 IMAGE_APT_PINS（測試釘住）：
# probe gate 的系統層 pytest 與 policy-check R-22 需要的 universal-ctags。
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
        python3-iniconfig=1.1.1-2 \
        python3-packaging=24.0-1 \
        python3-pip \
        python3-pluggy=1.4.0-1 \
        python3-pytest=7.4.4-1 \
        python3-venv \
        socat \
        sudo \
        systemd \
        systemd-sysv \
        universal-ctags=5.9.20210829.0-1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && systemctl mask \
        dev-hugepages.mount \
        sys-fs-fuse-connections.mount \
        systemd-remount-fs.service \
        getty.target \
        console-getty.service

COPY validate.py /usr/local/libexec/cortex-qualification-validate
COPY contract.py /usr/local/libexec/qualification/contract.py
COPY __init__.py /usr/local/libexec/qualification/__init__.py
COPY verify_bundle.py /usr/local/libexec/cortex-qualification-verify-bundle
COPY driver.py /usr/local/libexec/cortex-release-qualification
COPY qualification.schema.json /usr/local/share/cortex/qualification.schema.json

RUN chmod 0755 \
        /usr/local/libexec/cortex-qualification-validate \
        /usr/local/libexec/cortex-qualification-verify-bundle \
        /usr/local/libexec/cortex-release-qualification

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/sbin/init"]
