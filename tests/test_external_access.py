from __future__ import annotations

import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src.server import (
    PROJECT_ROOT,
    SESSION_COOKIE_NAME,
    AppContext,
    allowed_local_host,
    build_server,
    load_access_config,
    local_ip_addresses,
    mutation_request_error,
    server_host_names,
)


TEST_PASSWORD = "test-0857"


class AccessConfigTests(unittest.TestCase):
    def test_missing_file_keeps_local_only_behaviour(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            access = load_access_config(Path(temp) / "access.json")
        self.assertFalse(access.allow_external)
        self.assertFalse(access.external_enabled)

    def test_flag_and_password_are_both_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "access.json"
            path.write_text(
                json.dumps({"password": "x", "allow_external": False}), encoding="utf-8"
            )
            self.assertFalse(load_access_config(path).external_enabled)
            path.write_text(
                json.dumps({"password": "", "allow_external": True}), encoding="utf-8"
            )
            self.assertFalse(load_access_config(path).external_enabled)
            path.write_text(
                json.dumps({"password": "x", "allow_external": True}), encoding="utf-8"
            )
            self.assertTrue(load_access_config(path).external_enabled)


class ServerHostNameTests(unittest.TestCase):
    def test_listening_addresses_are_accepted_besides_the_local_list(self) -> None:
        hosts = server_host_names(8765, ["192.168.1.20", "fe80::1"])
        self.assertIn("192.168.1.20:8765", hosts)
        self.assertIn("[fe80::1]:8765", hosts)
        self.assertTrue(allowed_local_host("192.168.1.20:8765", 8765, hosts))
        self.assertTrue(allowed_local_host("127.0.0.1:8765", 8765, hosts))
        self.assertFalse(allowed_local_host("evil.example:8765", 8765, hosts))

    def test_origin_check_accepts_the_listening_address(self) -> None:
        hosts = server_host_names(8765, ["192.168.1.20"])
        headers = {
            "Content-Type": "application/json",
            "Origin": "http://192.168.1.20:8765",
        }
        self.assertIsNone(mutation_request_error(headers, 8765, hosts))
        headers["Origin"] = "http://evil.example:8765"
        issue = mutation_request_error(headers, 8765, hosts)
        self.assertIsNotNone(issue)

    def test_local_addresses_always_contain_loopback(self) -> None:
        self.assertIn("127.0.0.1", local_ip_addresses(refresh=True))


class ExternalAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        catalog_root = self.root / "catalog"
        catalog_root.mkdir(parents=True)
        (catalog_root / "INDEX.yaml").write_text(
            "updated: '2026-08-13'\nactive_assets:\n"
            "  style_cards: []\n  templates: []\n  editors: []\n",
            encoding="utf-8",
        )
        self.catalog_root = catalog_root
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        self.temp.cleanup()

    def start(self, allow_external: bool = True, password: str = TEST_PASSWORD) -> None:
        access_path = self.root / "access.json"
        access_path.write_text(
            json.dumps({"password": password, "allow_external": allow_external}),
            encoding="utf-8",
        )
        self.context = AppContext(
            self.catalog_root,
            self.root / "studio.db",
            self.root,
            self.root / "missing-engine.json",
            access_path,
        )
        self.server = build_server("127.0.0.1", 0, self.context)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        cookie: str = "",
        host: str = "",
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json", "Origin": self.base}
        if cookie:
            headers["Cookie"] = cookie
        if host:
            headers["Host"] = host
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self, password: str = TEST_PASSWORD) -> str:
        status, headers, _ = self.call("/api/login", "POST", {"password": password})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        return cookie.split(";", 1)[0]

    def test_local_client_still_needs_no_login(self) -> None:
        self.start()
        status, _, raw = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "ok")
        status, _, _ = self.call(
            "/api/documents", "POST", {"title": "本機", "content": "本機來源免登入。"}
        )
        self.assertEqual(status, 201)

    def test_remote_client_without_session_is_refused(self) -> None:
        self.start()
        with mock.patch("src.server.client_is_local", return_value=False):
            status, _, _ = self.call("/api/health")
            self.assertEqual(status, 401)

            status, headers, _ = self.call("/")
            self.assertEqual(status, 302)
            self.assertEqual(headers["Location"], "/login")

            status, _, _ = self.call(
                "/api/documents", "POST", {"title": "外部", "content": "未登入不得寫入。"}
            )
            self.assertEqual(status, 401)

            status, _, page = self.call("/login")
            self.assertEqual(status, 200)
            self.assertIn("通行密碼", page.decode("utf-8"))
            self.assertEqual(self.call("/login.css")[0], 200)
            self.assertEqual(self.call("/login.js")[0], 200)

    def test_remote_client_works_after_login(self) -> None:
        self.start()
        with mock.patch("src.server.client_is_local", return_value=False):
            status, _, raw = self.call("/api/login", "POST", {"password": "wrong"})
            self.assertEqual(status, 401)
            self.assertNotIn("Set-Cookie", self.call("/api/login", "POST", {"password": "x"})[1])

            cookie = self.login()

            status, _, raw = self.call("/api/health", cookie=cookie)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["status"], "ok")

            status, _, _ = self.call("/", cookie=cookie)
            self.assertEqual(status, 200)

            status, _, _ = self.call(
                "/api/documents",
                "POST",
                {"title": "外部", "content": "登入後可寫入。"},
                cookie=cookie,
            )
            self.assertEqual(status, 201)

            status, _, _ = self.call(
                "/api/health", cookie=f"{SESSION_COOKIE_NAME}=forged-token"
            )
            self.assertEqual(status, 401)

    def test_five_wrong_passwords_lock_the_client(self) -> None:
        self.start()
        with mock.patch("src.server.client_is_local", return_value=False):
            for _ in range(5):
                status, _, _ = self.call("/api/login", "POST", {"password": "wrong"})
                self.assertEqual(status, 401)

            status, _, raw = self.call("/api/login", "POST", {"password": "wrong"})
            self.assertEqual(status, 429)
            self.assertIn("鎖定", json.loads(raw)["error"])

            status, headers, _ = self.call("/api/login", "POST", {"password": TEST_PASSWORD})
            self.assertEqual(status, 429)
            self.assertNotIn("Set-Cookie", headers)

            self.context.login_guard.clear("127.0.0.1")
            self.assertEqual(
                self.call("/api/login", "POST", {"password": TEST_PASSWORD})[0], 200
            )

    def test_external_access_disabled_keeps_remote_clients_out(self) -> None:
        self.start(allow_external=False)
        with mock.patch("src.server.client_is_local", return_value=False):
            self.assertEqual(self.call("/api/health")[0], 403)
            self.assertEqual(self.call("/")[0], 403)
            self.assertEqual(
                self.call("/api/login", "POST", {"password": TEST_PASSWORD})[0], 403
            )
        self.assertEqual(self.call("/api/health")[0], 200)

    def test_foreign_host_header_is_still_refused(self) -> None:
        self.start()
        status, _, _ = self.call("/api/health", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)

    def test_listening_nic_address_requires_login_even_from_local_peer(self) -> None:
        """Host 帶非 localhost 位址＝視為外部流量，即使來源 IP 是本機也要登入。

        反向隧道（cloudflared）代理的外部訪客在本機端 peer IP 就是 127.0.0.1，
        免登入判定若只看 IP，外人會整條免密碼；故 Host 非 localhost 一律走登入。
        """
        self.start()
        external = sorted(
            item for item in local_ip_addresses(refresh=True)
            if item not in {"127.0.0.1", "::1"} and ":" not in item
        )
        if not external:
            self.skipTest("這台機器沒有非回送網卡位址可比對")
        status, _, _ = self.call("/api/health", host=f"{external[0]}:{self.port}")
        self.assertEqual(status, 401)


class ExternalBindGateTests(unittest.TestCase):
    def test_external_bind_is_refused_when_access_config_disables_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            access_path = Path(temp) / "access.json"
            access_path.write_text(
                json.dumps({"password": TEST_PASSWORD, "allow_external": False}),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "app.py",
                    "--host",
                    "0.0.0.0",
                    "--access-config",
                    str(access_path),
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("allow_external", result.stderr)


if __name__ == "__main__":
    unittest.main()
