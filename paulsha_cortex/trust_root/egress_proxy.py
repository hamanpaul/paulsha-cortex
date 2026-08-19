"""出口 proxy：`CONNECT` forward proxy ＋ 主機名白名單（#716）。

## 它在整個形態裡的位置

`#716` 選項 B 要關掉 executor 的內層沙箱，只留 systemd 外層。內層擋得住、外層擋不住
的清單裡，**唯一不可接受**的一條是出口網路：`RestrictAddressFamilies=AF_UNIX AF_INET
AF_INET6` 放行 IPv4/IPv6，而 job 讀得到自己的 `auth.json`（兩層都不擋，而且刻意不擋
——token 過期必須 refresh 得回來，見 R-6 取捨）。0819 實機一次跑完的結論逐字是
`CREDS-READABLE / EGRESS-OPEN`：**模型跑的任意命令可讀出自己的 token 並外傳，一條命令
兩步。**

要達成的性質是：

    **executor 連得到自己的模型 API，模型下的任意命令連不到別處。**

形態是兩層：

  1. **核心層**（job unit 上）：`IPAddressDeny=any` ＋ `IPAddressAllow=<本服務位址>/32`。
     位址級、核心強制，不由行程環境決定——模型的命令 `env -u HTTPS_PROXY` 繞得過第 2 層，
     繞不過這一層。
  2. **可用性層**（job unit 上）：`Environment=HTTPS_PROXY=<本服務>`。executor 自己走
     這條路出去；0819 實測 codex／claude／agy **三支都吃** proxy env。

本模組是那條路的另一端：它是**唯一**知道「哪些主機放行」的地方，而那份清單來自
`permgen.egress_allowlist()`——由 `EXECUTOR_TOOLS` 的 `api_hosts` 機械導出，沒有第二份
可以漂移。

## 為什麼是主機名白名單而不是位址白名單

0819 實測 `api.openai.com` 解到 Cloudflare（`162.159.140.245`／`172.66.0.243`）：CIDR 會
漂，而且同一組位址上有無數其他站點。`IPAddressAllow=` 表達不出「只連得到模型 API」。

## 為什麼不用 codex 自帶的 managed proxy

codex 0.147.0 內建一整個 `codex_network_proxy`（MITM、`allowed_domains`／`denied_domains`、
`/etc/codex/managed_config.toml`），但它的功能描述逐字是 *“Apply network proxy
restrictions to **sandboxed sessions** that already have network access.”*——它是套在
**內層沙箱**上的限制，而選項 B 的前提就是關掉那一層；它的強制手段又是把一長串
`HTTP_PROXY`／`NPM_CONFIG_*`／`PIP_PROXY`… 注入子行程環境，沒有內層沙箱時 `unset` 一下
就繞過。⇒ 它在 B 的世界裡結構上不成立。

## 這支程式**不做**什麼（逐條，避免它長成一個信任面）

- **不終結 TLS**：只做 `CONNECT` 隧道。它看不到、也不可能看到請求內容或憑證。
- **不注入任何 header、不持有任何憑證**：它不是認證代理。
- **不做 wildcard 比對**：`*.chatgpt.com` 之類會把 `ab.chatgpt.com`（遙測端點）一起放進
  來，而 0819 實測 codex 在該端點被拒之後照常完成一次 turn。exact match 就夠。
- **不放行非 443 埠**：`CONNECT host:22` 之類一律拒絕。
- **不連到非全域位址**：白名單主機解析結果若落在 loopback／private／link-local，一律拒絕
  （DNS rebinding 之下，「只放行公開主機」這句話會被繞過）。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import socketserver
import sys
import threading
from typing import Iterable, Mapping

from .permgen import (
    EGRESS_PROXY,
    EGRESS_RESIDUAL_RISK,
    EgressProxy,
    egress_allowlist,
    egress_allowed_hosts,
)

__all__ = [
    "ALLOWED_PORTS",
    "EgressProxyServer",
    "allowed_upstream_address",
    "main",
    "parse_connect_target",
]

#: 允許 `CONNECT` 的目的埠。只有 443——模型 API 全走 HTTPS，而多放一個埠就是多一條
#: 「經由白名單主機名到別的服務」的路。
ALLOWED_PORTS: frozenset[int] = frozenset({443})

#: `CONNECT <host>:<port> HTTP/1.1` 的請求行。刻意嚴格：主機名形狀與
#: `permgen.EgressHost` 的驗證一致，不接受 IP 字面量（那會繞過主機名白名單的語意）。
_CONNECT_RE = re.compile(
    r"^CONNECT[ \t]+"
    r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):(?P<port>\d{1,5})"
    r"[ \t]+HTTP/1\.[01]$"
)

#: 讀請求行的上限。正常的 `CONNECT` 請求頭遠小於它；超過就是有人在灌東西。
_MAX_REQUEST_BYTES = 8192
#: 讀請求頭的逾時（秒）。
_HANDSHAKE_TIMEOUT = 20.0
#: 連上游的逾時（秒）。
_UPSTREAM_TIMEOUT = 20.0
#: 同時在跑的隧道上限。`TasksMax=` 已經是一層，這裡再擋一層是為了讓拒絕**可觀測**
#: （log 上看得到），而不是讓行程在 fork 失敗上死掉。
_MAX_TUNNELS = 128


def parse_connect_target(request_line: str) -> tuple[str, int] | None:
    """`CONNECT host:port HTTP/1.1` → `(host, port)`；形狀不合則 `None`。"""
    match = _CONNECT_RE.match(request_line.strip())
    if match is None:
        return None
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        return None
    return match.group("host"), port


def allowed_upstream_address(host: str) -> tuple[str, str] | None:
    """解析 `host` 並回傳第一個**全域**位址；沒有則回 `None` 與理由。

    回傳 `(address, family_note)`，或 `None`。

    非全域位址一律拒絕：白名單是主機名級的，若某個白名單主機（被劫持的 DNS、或
    `/etc/hosts`）解到 `127.0.0.1`，「只連得到模型 API」這句話就被繞過了，而 job 恰好
    是**唯一**能連到本服務的那一邊。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_global and not parsed.is_multicast:
            return address, parsed.version == 6 and "ipv6" or "ipv4"
    return None


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    """一條連線＝一次 `CONNECT` 判定 ＋（放行時）一條雙向隧道。"""

    server: "EgressProxyServer"

    def handle(self) -> None:  # noqa: D102 - socketserver 契約
        client = self.request
        peer = self.client_address[0] if self.client_address else "?"
        with self.server.tunnel_lock:
            if self.server.tunnels >= _MAX_TUNNELS:
                self.server.log("saturated", peer=peer, tunnels=self.server.tunnels)
                self._respond(503, "Service Unavailable")
                return
            self.server.tunnels += 1
        try:
            self._serve(client, peer)
        finally:
            with self.server.tunnel_lock:
                self.server.tunnels -= 1

    def _serve(self, client: socket.socket, peer: str) -> None:
        client.settimeout(_HANDSHAKE_TIMEOUT)
        buffer = b""
        try:
            while b"\r\n\r\n" not in buffer:
                if len(buffer) > _MAX_REQUEST_BYTES:
                    self.server.log("oversized-request", peer=peer, bytes=len(buffer))
                    self._respond(431, "Request Header Fields Too Large")
                    return
                chunk = client.recv(4096)
                if not chunk:
                    return
                buffer += chunk
        except OSError:
            return

        request_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        target = parse_connect_target(request_line)
        if target is None:
            # 非 CONNECT 一律拒絕：明文 HTTP forward 會讓這支程式看得到內容，
            # 也會多出一條「白名單主機名 ＋ 任意路徑」的通道。
            self.server.log("denied", peer=peer, reason="not-connect", line=request_line)
            self._respond(405, "Method Not Allowed")
            return

        host, port = target
        if host not in self.server.allowed_hosts:
            self.server.log("denied", peer=peer, reason="host-not-allowed", host=host, port=port)
            self._respond(403, "Forbidden")
            return
        if port not in ALLOWED_PORTS:
            self.server.log("denied", peer=peer, reason="port-not-allowed", host=host, port=port)
            self._respond(403, "Forbidden")
            return

        resolved = allowed_upstream_address(host)
        if resolved is None:
            self.server.log("denied", peer=peer, reason="no-global-address", host=host)
            self._respond(502, "Bad Gateway")
            return
        address, _family = resolved

        try:
            upstream = socket.create_connection((address, port), timeout=_UPSTREAM_TIMEOUT)
        except OSError as exc:
            self.server.log(
                "upstream-failed", peer=peer, host=host, address=address, error=str(exc)
            )
            self._respond(502, "Bad Gateway")
            return

        self.server.log("allowed", peer=peer, host=host, address=address, port=port)
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            worker = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
            worker.start()
            _pump(upstream, client)
            worker.join(timeout=5.0)
        except OSError:
            pass
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _respond(self, status: int, reason: str) -> None:
        try:
            self.request.sendall(
                f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode("latin-1")
            )
        except OSError:
            pass


class EgressProxyServer(socketserver.ThreadingTCPServer):
    """綁在專屬 loopback 位址上的 `CONNECT` proxy。"""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        proxy: EgressProxy = EGRESS_PROXY,
        allowed_hosts: Iterable[str] | None = None,
        stream=None,
    ) -> None:
        self.proxy = proxy
        # 白名單只在啟動時取一次：它是部署樹裡的程式碼，改它必須重新部署 ＋ 重啟服務。
        # 「執行中的服務能不能換白名單」在這裡是一個**設計上的否**——可熱換的白名單
        # 就是一個新的、需要自己的權限模型的控制面。
        self.allowed_hosts = frozenset(
            allowed_hosts if allowed_hosts is not None else egress_allowed_hosts()
        )
        self.tunnels = 0
        self.tunnel_lock = threading.Lock()
        self._stream = stream if stream is not None else sys.stdout
        super().__init__((proxy.bind_address, proxy.port), _Handler)

    def log(self, event: str, **fields: object) -> None:
        """單行結構化 log（走 journal）。**不記任何請求內容**——只有隧道的兩端。"""
        parts = " ".join(f"{key}={value!r}" for key, value in sorted(fields.items()))
        print(f"egress-proxy {event} {parts}".rstrip(), file=self._stream, flush=True)


def _describe(proxy: EgressProxy) -> list[str]:
    lines = [
        f"bind          : {proxy.bind_address}:{proxy.port}",
        f"url           : {proxy.url}",
        f"account       : {proxy.account}",
        f"unit          : {proxy.unit_name}",
        f"job IPAllow   : {proxy.ip_address_allow}",
        f"allowed ports : {sorted(ALLOWED_PORTS)}",
        "",
        "allowlist（permgen.egress_allowlist()，唯一來源）：",
    ]
    for entry in egress_allowlist():
        mark = "" if entry.measured else "   ⚠ 未實機量測"
        lines.append(f"  - {entry.host}{mark}")
    lines += ["", "已知殘留（EGRESS_RESIDUAL_RISK）："]
    lines += [f"  - {risk}" for risk in EGRESS_RESIDUAL_RISK]
    return lines


def main(argv: "list[str] | None" = None, env: Mapping[str, str] | None = None) -> int:
    """`cortex egress-proxy` 的進入點。

    `--check` 只印出實際生效的設定與白名單就結束（runbook 用它核對「落檔的 unit
    指到的位址」與「這支程式會綁的位址」是同一個）。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        print("usage: cortex egress-proxy [--check]")
        print()
        print(__doc__.strip().splitlines()[0])
        return 0
    check_only = "--check" in args
    unknown = [a for a in args if a not in {"--check"}]
    if unknown:
        print(f"unknown argument: {unknown[0]}", file=sys.stderr)
        return 2

    proxy = EGRESS_PROXY
    if check_only:
        print("\n".join(_describe(proxy)))
        return 0

    server = EgressProxyServer(proxy)
    server.log(
        "listening",
        bind=f"{proxy.bind_address}:{proxy.port}",
        hosts=len(server.allowed_hosts),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - 服務進入點走 console script
    raise SystemExit(main())
