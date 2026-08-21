"""#716：出口網路管制（選項 B 的前置條件）的結構性不變式。

本檔驗的是「產生器會產出什麼」與「兩層之間的契約」，**不是**「這台機器上網路怎麼跑」
——後者是 runbook 第 4e-2i 步的實機量測（本檔的 docstring 逐字引用那些輸出，但不重跑
它們：CI 上沒有 systemd 加固面，在那裡宣稱驗過網路行為就是本 repo 記過八次以上的假綠）。

實機量測的逐字結果（0819，builder 真實加固面複本、`unit_replica_properties()` 全量導出）：

    IPAddressDeny=any                      → EXT-1.1.1.1:443 BLOCKED TimeoutError
    ＋ IPAddressAllow=127.0.7.16/32        → LOOPBACK proxy OK、EXT 仍 BLOCKED
    ＋ Environment=HTTPS_PROXY=            → curl https://chatgpt.com/... http=405（連得到）
    白名單外                                → curl: (56) CONNECT tunnel failed, response 403
    env -u HTTPS_PROXY 後直連四個位址        → 全 TimeoutError
    真實 `codex exec`                       → rc=0、模型逐字回 PSC716-EGRESS-OK、10,659 tokens
"""

from __future__ import annotations

import socket
import threading
import unittest

from paulsha_cortex.coordinator import job_shim
from paulsha_cortex.trust_root import egress_proxy, permgen
from paulsha_cortex.trust_root.registry import Principal


class EgressAllowlistTests(unittest.TestCase):
    """白名單是**由 `EXECUTOR_TOOLS` 機械導出的單一來源**。"""

    def _executor_hosts(self, name: str) -> set[str]:
        for tool in permgen.EXECUTOR_TOOLS:
            if tool.name == name:
                return {entry.host for entry in tool.api_hosts}
        raise AssertionError(f"executor {name} 不存在於 EXECUTOR_TOOLS")

    def test_allowlist_is_derived_from_executor_tools(self) -> None:
        declared = {
            entry.host for tool in permgen.EXECUTOR_TOOLS for entry in tool.api_hosts
        }
        self.assertEqual(declared, set(permgen.egress_allowed_hosts()))

    def test_allowlist_is_deterministic_and_deduped(self) -> None:
        hosts = permgen.egress_allowed_hosts()
        self.assertEqual(list(hosts), sorted(hosts))
        self.assertEqual(len(hosts), len(set(hosts)))
        self.assertEqual(hosts, permgen.egress_allowed_hosts())

    def test_every_model_executor_declares_at_least_one_host(self) -> None:
        # 一個 executor 一格都沒宣告時，走它的 job 會在出口管制下靜默連不上模型——
        # 症狀是逾時，離原因很遠。這條讓「忘了填」在 import 之後的第一個測試就紅。
        for tool in permgen.EXECUTOR_TOOLS:
            with self.subTest(tool.name):
                self.assertTrue(
                    tool.api_hosts, f"executor {tool.name} 沒有宣告任何 api_hosts"
                )

    def test_copilot_live_observation_hosts_are_declared(self) -> None:
        expected = {
            "api.individual.githubcopilot.com",
            "telemetry.individual.githubcopilot.com",
        }
        actual = self._executor_hosts("copilot")
        self.assertFalse(
            expected - actual,
            f"copilot 缺少 live-observed api_hosts: {sorted(expected - actual)}",
        )

    def test_agy_live_observation_hosts_are_declared(self) -> None:
        expected = {
            "oauth2.googleapis.com",
            "daily-cloudcode-pa.googleapis.com",
            "cloudcode-pa.googleapis.com",
            "www.googleapis.com",
            "lh3.googleusercontent.com",
        }
        actual = self._executor_hosts("agy")
        self.assertFalse(
            expected - actual,
            f"agy 缺少 live-observed api_hosts: {sorted(expected - actual)}",
        )

    def test_declared_hosts_are_exact_hostnames_not_wildcards(self) -> None:
        for tool in permgen.EXECUTOR_TOOLS:
            for entry in tool.api_hosts:
                with self.subTest(tool=tool.name, host=entry.host):
                    self.assertNotIn("*", entry.host)
                    self.assertFalse(entry.host.startswith("."))
                    self.assertFalse(entry.host.endswith("."))

    def test_unmeasured_hosts_are_listed_not_hidden(self) -> None:
        unmeasured = permgen.unmeasured_egress_hosts()
        # 「量過」與「沒量過」必須是兩個看得出來的值（#714 的原型錯誤：一格空白同時
        # 代表「沒有」與「還沒量」）。這裡不斷言數量，只斷言兩件事：
        #   1. 未量測的格子**仍在**白名單上（fail-closed 的方向是「沒宣告的連不到」）；
        #   2. 它們列得出來。
        allowed = set(permgen.egress_allowed_hosts())
        for entry in unmeasured:
            self.assertIn(entry.host, allowed)
            self.assertFalse(entry.measured)
            self.assertTrue(entry.evidence.strip())

    def test_measured_host_requires_evidence(self) -> None:
        with self.assertRaises(ValueError):
            permgen.EgressHost("example.com", evidence="", measured=True)

    def test_host_shape_is_validated(self) -> None:
        for bad in ("*.chatgpt.com", "chatgpt.com/x", "", "-bad", "a b"):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    permgen.EgressHost(bad, evidence="x")


class EgressProxyConfigTests(unittest.TestCase):
    def test_bind_address_must_not_be_plain_loopback(self) -> None:
        # 0819 實測：IPAddressAllow=127.0.0.1/32 之下 job 連得到 127.0.0.1:2375
        # （未認證的 docker daemon TCP API ＝ 宿主 root）。
        with self.assertRaises(ValueError):
            permgen.EgressProxy(bind_address="127.0.0.1")

    def test_bind_address_must_be_loopback(self) -> None:
        with self.assertRaises(ValueError):
            permgen.EgressProxy(bind_address="10.0.0.5")

    def test_job_env_covers_both_cases_and_clears_no_proxy(self) -> None:
        env = permgen.EGRESS_PROXY.job_env()
        url = permgen.EGRESS_PROXY.url
        self.assertEqual(env["HTTPS_PROXY"], url)
        self.assertEqual(env["https_proxy"], url)
        self.assertEqual(env["HTTP_PROXY"], url)
        self.assertEqual(env["http_proxy"], url)
        self.assertEqual(env["NO_PROXY"], "")
        self.assertEqual(env["no_proxy"], "")

    def test_ip_address_allow_is_a_single_address(self) -> None:
        self.assertEqual(
            permgen.EGRESS_PROXY.ip_address_allow,
            f"{permgen.EGRESS_PROXY.bind_address}/32",
        )


class EgressCoverageTests(unittest.TestCase):
    def test_every_job_principal_has_an_explicit_decision(self) -> None:
        covered = permgen.EGRESS_CONTROLLED_JOB_PRINCIPALS | set(
            permgen.EGRESS_UNCONTROLLED_JOB_PRINCIPALS
        )
        for principal in permgen.DOWNGRADED_JOB_PRINCIPALS:
            with self.subTest(principal.value):
                self.assertIn(principal, covered)

    def test_uncontrolled_principals_carry_a_reason(self) -> None:
        for principal, reason in permgen.EGRESS_UNCONTROLLED_JOB_PRINCIPALS.items():
            with self.subTest(principal.value):
                self.assertGreater(len(reason.strip()), 80, "理由不得是一句空話")

    def test_coverage_check_is_fail_closed(self) -> None:
        # 新增一個 job principal 而沒有為它做出「要不要管出口」的決定時，模組載不起來。
        original = permgen.DOWNGRADED_JOB_PRINCIPALS
        try:
            permgen.DOWNGRADED_JOB_PRINCIPALS = original + (Principal.MONITOR,)
            with self.assertRaises(ValueError):
                permgen._validate_egress_coverage()
        finally:
            permgen.DOWNGRADED_JOB_PRINCIPALS = original


class JobUnitEgressTests(unittest.TestCase):
    """四份模型 job unit 拿到出口管制；gate 的兩份**明示**沒有。"""

    scheme = permgen.SCHEMES["four-way"]

    def _unit(self, principal: Principal, profile_id: str) -> str:
        return permgen.build_job_unit(
            self.scheme,
            principal=principal,
            profile=permgen.HARDENING_PROFILES_BY_ID[profile_id],
        ).content

    def _directives(self, text: str) -> list[tuple[str, str]]:
        return permgen._unit_service_directives(text)

    def test_model_job_units_carry_both_halves(self) -> None:
        proxy = permgen.EGRESS_PROXY
        for principal in sorted(
            permgen.EGRESS_CONTROLLED_JOB_PRINCIPALS, key=lambda p: p.value
        ):
            for profile_id in sorted(permgen.HARDENING_PROFILES_BY_ID):
                with self.subTest(principal=principal.value, profile=profile_id):
                    directives = dict(self._directives(self._unit(principal, profile_id)))
                    self.assertEqual(directives.get("IPAddressDeny"), "any")
                    self.assertEqual(
                        directives.get("IPAddressAllow"), proxy.ip_address_allow
                    )
                    envs = {
                        line.split("=", 1)[0]: line.split("=", 1)[1]
                        for key, line in self._directives(
                            self._unit(principal, profile_id)
                        )
                        if key == "Environment"
                    }
                    for name, value in proxy.job_env().items():
                        self.assertIn(name, envs)
                        self.assertEqual(envs[name], value)

    def test_gate_job_units_have_no_egress_control(self) -> None:
        for profile_id in sorted(permgen.HARDENING_PROFILES_BY_ID):
            with self.subTest(profile=profile_id):
                text = self._unit(Principal.GATE, profile_id)
                keys = {key for key, _value in self._directives(text)}
                self.assertNotIn("IPAddressDeny", keys)
                self.assertNotIn("IPAddressAllow", keys)
                envs = {
                    line.split("=", 1)[0]
                    for key, line in self._directives(text)
                    if key == "Environment"
                }
                self.assertFalse(envs & set(permgen.EGRESS_PROXY_ENV_NAMES))

    def test_all_job_units_carry_containment(self) -> None:
        expected = {key: value for key, value, _why in permgen._JOB_CONTAINMENT}
        for principal in permgen.DOWNGRADED_JOB_PRINCIPALS:
            for profile_id in sorted(permgen.HARDENING_PROFILES_BY_ID):
                with self.subTest(principal=principal.value, profile=profile_id):
                    directives = dict(self._directives(self._unit(principal, profile_id)))
                    for key, value in expected.items():
                        self.assertEqual(directives.get(key), value)

    def test_memory_max_never_appears_without_swap_max(self) -> None:
        # 0819 實測：只設 MemoryMax=64M 時 200MB 配置仍 `ALLOC-OK`（配置整個溢到 swap）。
        # 這一條把「成對」變成結構不變式，而不是註解裡的一句提醒。
        table = {key for key, _value, _why in permgen._JOB_CONTAINMENT}
        self.assertEqual("MemoryMax" in table, "MemorySwapMax" in table)
        for principal in permgen.DOWNGRADED_JOB_PRINCIPALS:
            for profile_id in sorted(permgen.HARDENING_PROFILES_BY_ID):
                with self.subTest(principal=principal.value, profile=profile_id):
                    directives = dict(self._directives(self._unit(principal, profile_id)))
                    self.assertEqual(
                        "MemoryMax" in directives, "MemorySwapMax" in directives
                    )

    def test_deferred_keys_are_not_silently_emitted(self) -> None:
        # `SocketBindDeny=any` 在本環境量到**不生效**。放進 unit 當安慰劑會讓讀 unit
        # 的人以為那一面已經關上——因此它只能以註解形式出現。
        deferred = {key for key, _value, _why in permgen.HARDENING_DEFERRED}
        for principal in permgen.DOWNGRADED_JOB_PRINCIPALS:
            for profile_id in sorted(permgen.HARDENING_PROFILES_BY_ID):
                text = self._unit(principal, profile_id)
                keys = {key for key, _value in self._directives(text)}
                with self.subTest(principal=principal.value, profile=profile_id):
                    self.assertFalse(keys & deferred)
                    for key in deferred:
                        self.assertIn(key, text, "未加入的理由必須留在 unit 上看得到")


class EgressProxyUnitTests(unittest.TestCase):
    scheme = permgen.SCHEMES["four-way"]

    def setUp(self) -> None:
        self.unit = permgen.build_egress_proxy_unit(self.scheme)
        self.directives = permgen._unit_service_directives(self.unit.content)

    def test_account_is_not_a_job_or_manager_account(self) -> None:
        account = permgen.EGRESS_PROXY.account
        self.assertNotIn(account, self.scheme.headless_accounts())
        self.assertNotEqual(account, self.scheme.durable_state_owner)
        self.assertNotEqual(account, self.scheme.deploy_account)
        self.assertEqual(dict(self.directives)["User"], account)

    def test_no_root_and_no_capabilities(self) -> None:
        directives = dict(self.directives)
        self.assertEqual(directives["NoNewPrivileges"], "yes")
        self.assertEqual(directives["CapabilityBoundingSet"], "")
        self.assertEqual(directives["AmbientCapabilities"], "")

    def test_has_no_read_write_paths(self) -> None:
        self.assertEqual(self.unit.read_write_paths, ())
        self.assertNotIn("ReadWritePaths", {key for key, _v in self.directives})

    def test_shares_the_same_hardening_table(self) -> None:
        directives = dict(self.directives)
        for key, value, _why in permgen._HARDENING:
            with self.subTest(key):
                self.assertEqual(directives.get(key), value)

    def test_shares_the_same_containment_table(self) -> None:
        directives = dict(self.directives)
        for key, value, _why in permgen._JOB_CONTAINMENT:
            with self.subTest(key):
                self.assertEqual(directives.get(key), value)

    def test_does_not_restrict_its_own_egress(self) -> None:
        # 它就是那條出口；收斂手段是白名單，不是位址。
        keys = {key for key, _value in self.directives}
        self.assertNotIn("IPAddressDeny", keys)

    def test_exec_start_matches_the_layout_contract(self) -> None:
        self.assertEqual(
            dict(self.directives)["ExecStart"],
            permgen.DEFAULT_LAYOUT.egress_proxy_exec_start,
        )
        self.assertTrue(
            permgen.DEFAULT_LAYOUT.egress_proxy_exec_start.endswith(
                "/bin/cortex egress-proxy"
            )
        )

    def test_allowlist_is_not_on_the_command_line(self) -> None:
        # 寫進 ExecStart= 會立刻產生第二份真相。
        exec_start = dict(self.directives)["ExecStart"]
        for host in permgen.egress_allowed_hosts():
            self.assertNotIn(host, exec_start)


class UnitReplicaEgressPairTests(unittest.TestCase):
    """半套的出口管制在**產生複本的那一刻**就要紅。"""

    def _job_unit_text(self) -> str:
        return permgen.build_job_unit(
            permgen.SCHEMES["four-way"],
            profile=permgen.HARDENING_PROFILES_BY_ID["jit"],
        ).content

    def test_generated_unit_passes(self) -> None:
        props = permgen.unit_replica_properties(self._job_unit_text(), instance="probe")
        self.assertIn("--property=IPAddressDeny=any", props)
        self.assertIn(
            f"--property=Environment=HTTPS_PROXY={permgen.EGRESS_PROXY.url}", props
        )

    def test_deny_without_proxy_is_drift(self) -> None:
        text = "\n".join(
            line
            for line in self._job_unit_text().splitlines()
            if not line.startswith("Environment=HTTPS_PROXY=")
        )
        with self.assertRaises(permgen.UnitReplicaDriftError):
            permgen.unit_replica_properties(text)

    def test_proxy_without_deny_is_drift(self) -> None:
        text = "\n".join(
            line
            for line in self._job_unit_text().splitlines()
            if line != "IPAddressDeny=any"
        )
        with self.assertRaises(permgen.UnitReplicaDriftError):
            permgen.unit_replica_properties(text)

    def test_a_unit_with_neither_half_is_not_flagged(self) -> None:
        # gate 的兩份 unit 明示不受管制——它們不該因為這條檢查而起不來。
        text = permgen.build_job_unit(
            permgen.SCHEMES["four-way"],
            principal=Principal.GATE,
            profile=permgen.HARDENING_PROFILES_BY_ID["jit"],
        ).content
        permgen.unit_replica_properties(text)


class ShimEgressEnvContractTests(unittest.TestCase):
    """unit 的 `Environment=` 到不了模型——第二層在 shim（與 `PATH` 同一個機制）。"""

    def test_env_name_contract_is_locked_both_ways(self) -> None:
        self.assertEqual(
            tuple(sorted(permgen.EGRESS_PROXY_ENV_NAMES)),
            tuple(sorted(job_shim.EGRESS_PROXY_ENV_NAMES)),
        )
        self.assertEqual(
            set(permgen.EGRESS_PROXY.job_env()), set(job_shim.EGRESS_PROXY_ENV_NAMES)
        )

    def test_unit_declaration_reaches_the_job_env(self) -> None:
        spec = {"env": {"PATH": "/opt/cortex/toolchain/bin"}}
        environ = dict(permgen.EGRESS_PROXY.job_env())
        environ["PATH"] = "/unit/path"
        resolved = job_shim.resolve_job_env(spec, environ)
        for name, value in permgen.EGRESS_PROXY.job_env().items():
            with self.subTest(name):
                self.assertEqual(resolved[name], value)

    def test_empty_no_proxy_is_carried_not_dropped(self) -> None:
        # 空字串是**有意義的值**（明示清空）；用 truthiness 判斷會把它吃掉。
        spec = {"env": {"PATH": "/p"}}
        resolved = job_shim.resolve_job_env(spec, {"NO_PROXY": "", "PATH": "/p"})
        self.assertIn("NO_PROXY", resolved)
        self.assertEqual(resolved["NO_PROXY"], "")

    def test_spec_wins_over_unit(self) -> None:
        spec = {"env": {"PATH": "/p", "HTTPS_PROXY": "http://spec.example:1"}}
        resolved = job_shim.resolve_job_env(
            spec, {"HTTPS_PROXY": "http://unit.example:2", "PATH": "/p"}
        )
        self.assertEqual(resolved["HTTPS_PROXY"], "http://spec.example:1")

    def test_absent_declaration_adds_nothing(self) -> None:
        spec = {"env": {"PATH": "/p"}}
        resolved = job_shim.resolve_job_env(spec, {"PATH": "/p"})
        self.assertEqual(set(resolved), {"PATH"})


class ProxyRequestParsingTests(unittest.TestCase):
    def test_parses_a_well_formed_connect(self) -> None:
        self.assertEqual(
            egress_proxy.parse_connect_target("CONNECT chatgpt.com:443 HTTP/1.1"),
            ("chatgpt.com", 443),
        )

    def test_rejects_non_connect(self) -> None:
        self.assertIsNone(
            egress_proxy.parse_connect_target("GET http://chatgpt.com/ HTTP/1.1")
        )

    def test_only_443_is_allowed(self) -> None:
        self.assertEqual(egress_proxy.ALLOWED_PORTS, frozenset({443}))

    def test_local_names_have_no_global_address(self) -> None:
        # DNS rebinding 防線：白名單是主機名級的，解析結果落在 loopback 一律拒絕。
        self.assertIsNone(egress_proxy.allowed_upstream_address("localhost"))


class ProxyServerDenyTests(unittest.TestCase):
    """拒絕路徑的整合測試——**不需要對外網路**（全部在拒絕那一側就結束）。"""

    def setUp(self) -> None:
        self.proxy_cfg = permgen.EgressProxy(
            account=permgen.EGRESS_PROXY.account,
            bind_address="127.0.9.201",
            port=_free_port("127.0.9.201"),
        )
        try:
            self.server = egress_proxy.EgressProxyServer(
                self.proxy_cfg, allowed_hosts=("chatgpt.com",), stream=_Sink()
            )
        except OSError as exc:  # pragma: no cover - 受限環境
            self.skipTest(f"無法在 {self.proxy_cfg.bind_address} 上 bind: {exc}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _first_line(self, request: bytes) -> str:
        with socket.create_connection(
            (self.proxy_cfg.bind_address, self.proxy_cfg.port), timeout=5
        ) as sock:
            sock.sendall(request)
            return sock.recv(256).split(b"\r\n")[0].decode("latin-1")

    def test_host_not_on_allowlist_is_403(self) -> None:
        self.assertIn(
            "403", self._first_line(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        )

    def test_allowed_host_on_wrong_port_is_403(self) -> None:
        self.assertIn(
            "403", self._first_line(b"CONNECT chatgpt.com:22 HTTP/1.1\r\n\r\n")
        )

    def test_ip_literal_is_403(self) -> None:
        self.assertIn(
            "403", self._first_line(b"CONNECT 1.1.1.1:443 HTTP/1.1\r\n\r\n")
        )

    def test_plain_http_forward_is_405(self) -> None:
        self.assertIn(
            "405", self._first_line(b"GET http://chatgpt.com/ HTTP/1.1\r\n\r\n")
        )


class _Sink:
    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        return None


def _free_port(address: str) -> int:
    with socket.socket() as sock:
        try:
            sock.bind((address, 0))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
