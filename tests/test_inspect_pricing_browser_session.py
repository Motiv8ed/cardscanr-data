"""Offline CDP preflight tests using only an in-process loopback test server."""
from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

MODULE_FILE = Path(__file__).resolve().parents[1] / "scripts" / "inspect_pricing_browser_session.py"
SPEC = importlib.util.spec_from_file_location("inspect_pricing_browser_session", MODULE_FILE)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class CdpHandler(BaseHTTPRequestHandler):
    response_kind = "ready"
    requested_paths: list[str] = []

    def do_GET(self) -> None:
        self.__class__.requested_paths.append(self.path)
        port = self.server.server_port
        if self.response_kind == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/never-fetch")
            self.end_headers()
            return
        if self.response_kind == "invalid_json":
            body = b"{not JSON"
        elif self.response_kind == "remote_debugger":
            body = json.dumps({
                "Browser": "Chrome/136.0.0.0",
                "webSocketDebuggerUrl": "ws://example.com:9232/devtools/browser/secret",
            }).encode()
        elif self.response_kind == "not_chrome":
            body = json.dumps({"Browser": "Other/1.0", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/id"}).encode()
        else:
            body = json.dumps({
                "Browser": "Chrome/136.0.0.0",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/secret-id",
                "User-Agent": "does-not-belong-in-output",
            }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


class BrowserPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        CdpHandler.response_kind = "ready"
        CdpHandler.requested_paths = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CdpHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_healthy_cdp_loopback_only_and_sanitised_output(self) -> None:
        result = mod.inspect_browser_cdp(port=self.server.server_port)
        self.assertEqual(result["status"], "CDP_READY")
        self.assertTrue(result["cdpReady"])
        self.assertEqual(result["browserFamily"], "Chrome")
        self.assertEqual(CdpHandler.requested_paths, ["/json/version"])
        self.assertNotIn("secret-id", json.dumps(result))
        self.assertNotIn("does-not-belong", json.dumps(result))

    def test_redirect_is_rejected_before_leaving_loopback(self) -> None:
        CdpHandler.response_kind = "redirect"
        result = mod.inspect_browser_cdp(port=self.server.server_port)
        self.assertEqual(result["status"], "CDP_UNREACHABLE")
        self.assertEqual(result["errorClass"], "HTTPError")
        self.assertEqual(CdpHandler.requested_paths, ["/json/version"])

    def test_rejects_non_chrome(self) -> None:
        CdpHandler.response_kind = "not_chrome"
        self.assertEqual(mod.inspect_browser_cdp(port=self.server.server_port)["status"], "CDP_NOT_CHROME")

    def test_rejects_remote_websocket_target(self) -> None:
        CdpHandler.response_kind = "remote_debugger"
        self.assertEqual(mod.inspect_browser_cdp(port=self.server.server_port)["status"], "CDP_UNSAFE_DEBUGGER_TARGET")

    def test_invalid_json_has_no_raw_body(self) -> None:
        CdpHandler.response_kind = "invalid_json"
        result = mod.inspect_browser_cdp(port=self.server.server_port)
        self.assertEqual(result["status"], "CDP_INVALID_JSON")
        self.assertNotIn("not JSON", json.dumps(result))

    def test_reserved_bridge_ports_cannot_be_probed(self) -> None:
        for port in (9230, 9231):
            with self.subTest(port=port):
                result = mod.inspect_browser_cdp(port=port)
                self.assertEqual(result["status"], "RESERVED_BRIDGE_RECORDER_PORT")
                self.assertFalse(result["cdpReady"])
        self.assertEqual(CdpHandler.requested_paths, [])

    def test_invalid_ports_rejected_without_network(self) -> None:
        for port in (0, -2, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                mod.inspect_browser_cdp(port=port)
        self.assertEqual(CdpHandler.requested_paths, [])

    def test_profile_metadata_only_no_path_leakage(self) -> None:
        with TemporaryDirectory(prefix="PRIVATE_SECRET_PROFILE_") as d:
            path = Path(d)
            (path / "SingletonLock").touch()
            result = mod.inspect_browser_cdp(port=self.server.server_port, profile=path)
            self.assertTrue(result["profilePresent"])
            self.assertTrue(result["profileLockArtifactPresent"])
            self.assertNotIn(str(path), json.dumps(result))
            self.assertTrue((path / "SingletonLock").exists())

    def test_profile_missing_is_observation_not_failure(self) -> None:
        with TemporaryDirectory() as d:
            path = Path(d) / "nonexistent"
            result = mod.inspect_browser_cdp(port=self.server.server_port, profile=path)
            self.assertFalse(result["profilePresent"])
            self.assertEqual(result["status"], "CDP_READY")

    def test_websocket_must_be_matching_loopback_port(self) -> None:
        self.assertFalse(mod._debug_socket_is_local("ws://127.0.0.1:11/devtools/browser/x", 12))
        self.assertFalse(mod._debug_socket_is_local("ws://attacker.local:12/devtools/browser/x", 12))
        self.assertFalse(mod._debug_socket_is_local("ws://user:password@127.0.0.1:12/devtools/browser/x", 12))
        self.assertFalse(mod._debug_socket_is_local("https://127.0.0.1:12/devtools/browser/x", 12))
        self.assertTrue(mod._debug_socket_is_local("ws://127.0.0.1:12/devtools/browser/x", 12))


if __name__ == "__main__":
    unittest.main()
