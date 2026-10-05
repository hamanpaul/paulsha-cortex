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
        polkitd \
        python3 \
        python3-iniconfig=1.1.1-2 \
        python3-packaging=24.0-1 \
        python3-pip \
        python3-pluggy=1.4.0-1 \
        python3-pytest=7.4.4-1 \
        python3-venv \
        ripgrep \
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

# 系統層 node（#716）：toolchain wrapper 以 `/usr/bin/node` 執行 `.js` 進入點，
# contract 釘住的 openspec 要求 node >=20.19，且 `--jitless` 下只能用 20.x（見 contract 的 NODE_RUNTIME）。
# 版本與 sha256 逐字鏡射 qualification/contract.py 的 NODE_RUNTIME（測試釘住）。
ARG NODE_RUNTIME_VERSION=20.20.2
ARG NODE_RUNTIME_SHA256=19e56f0825510207dd904f087fe52faa0a4eb6b2aab5f0ea7a33830d04888b8b
RUN curl -fsSLo /tmp/node.tar.gz \
        "https://nodejs.org/dist/v${NODE_RUNTIME_VERSION}/node-v${NODE_RUNTIME_VERSION}-linux-x64.tar.gz" \
    && echo "${NODE_RUNTIME_SHA256}  /tmp/node.tar.gz" | sha256sum --check --strict - \
    && tar -xzf /tmp/node.tar.gz -C /tmp --no-same-owner \
        "node-v${NODE_RUNTIME_VERSION}-linux-x64/bin/node" \
    && install -o root -g root -m 0755 \
        "/tmp/node-v${NODE_RUNTIME_VERSION}-linux-x64/bin/node" /usr/bin/node \
    && rm -rf /tmp/node.tar.gz "/tmp/node-v${NODE_RUNTIME_VERSION}-linux-x64" \
    && /usr/bin/node --version

COPY validate.py /usr/local/libexec/cortex-qualification-validate
COPY contract.py /usr/local/libexec/qualification/contract.py
COPY __init__.py /usr/local/libexec/qualification/__init__.py
COPY verify_bundle.py /usr/local/libexec/cortex-qualification-verify-bundle
COPY release_source.py /usr/local/libexec/cortex-qualification-release-source
COPY driver.py /usr/local/libexec/cortex-release-qualification
COPY qualification.schema.json /usr/local/share/cortex/qualification.schema.json
# legacy-adoption profile (#1122): the Phase 2b-shaped fixture and its harness.
COPY legacy_fixture.py /usr/local/libexec/qualification/legacy_fixture.py
COPY legacy_fixture.json /usr/local/libexec/qualification/legacy_fixture.json
COPY legacy_adoption.py /usr/local/libexec/cortex-legacy-adoption

RUN chmod 0755 \
        /usr/local/libexec/cortex-qualification-validate \
        /usr/local/libexec/cortex-qualification-verify-bundle \
        /usr/local/libexec/cortex-qualification-release-source \
        /usr/local/libexec/cortex-release-qualification \
        /usr/local/libexec/cortex-legacy-adoption

STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/sbin/init"]
