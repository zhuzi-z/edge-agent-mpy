"""Tests for AP WiFi provisioning."""

import compat  # noqa: F401

import os
import json
import struct
import unittest
import time
import socket
import _thread
import network

from helpers import find_free_port, raw_request, _unique_name

import app.config as cfg
from app.provision import (
    load_wifi_config,
    save_wifi_config,
    _parse_form_body,
    _url_decode,
    _sta_fail_reason,
    ProvisionServer,
    DNSServer,
)


class TestParsing(unittest.TestCase):
    def test_form_parsing(self):
        self.assertEqual(_url_decode("hello"), "hello")
        self.assertEqual(_url_decode("hello+world"), "hello world")
        self.assertEqual(_url_decode("%E4%BD%A0%E5%A5%BD"), "\u4f60\u597d")
        self.assertEqual(_url_decode("my+net%21"), "my net!")
        params = _parse_form_body("ssid=TestNet&password=abc123")
        self.assertEqual(params["ssid"], "TestNet")
        self.assertEqual(params["password"], "abc123")
        params = _parse_form_body("ssid=My+WiFi&password=p%40ss")
        self.assertEqual(params["ssid"], "My WiFi")
        self.assertEqual(params["password"], "p@ss")
        params = _parse_form_body("ssid=Net&password=")
        self.assertEqual(params["password"], "")


class TestWifiConfigPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp_files = []

    def tearDown(self):
        for p in self._tmp_files:
            try:
                os.remove(p)
            except OSError:
                pass

    def _tmp_path(self):
        p = _unique_name("wifi_") + ".json"
        self._tmp_files.append(p)
        return p

    def test_wifi_config_roundtrip(self):
        self.assertEqual(load_wifi_config("/nonexistent.json"), (None, None))
        path = self._tmp_path()
        with open(path, "w") as f:
            f.write("not json{{{")
        self.assertEqual(load_wifi_config(path), (None, None))
        self.assertTrue(save_wifi_config("MyNet", "secret", path))
        self.assertEqual(load_wifi_config(path), ("MyNet", "secret"))
        with open(path, "w") as f:
            json.dump({"ssid": "", "password": "x"}, f)
        self.assertEqual(load_wifi_config(path), (None, None))


class TestProvisionServer(unittest.TestCase):
    def setUp(self):
        self.port = find_free_port()
        self.server = ProvisionServer(port=self.port)
        self.server.start()
        self._stopped = False
        _thread.start_new_thread(self._loop, ())
        time.sleep(0.05)

    def tearDown(self):
        self._stopped = True
        self.server.stop()
        time.sleep(0.02)

    def _loop(self):
        deadline = time.time() + 10
        while time.time() < deadline and not self._stopped:
            self.server.accept_and_handle()

    def test_portal_flow(self):
        for path in ("/", "/generate_204", "/hotspot-detect.html", "/ncsi.txt"):
            status, _, body = raw_request(self.port, "GET", path)
            self.assertEqual(status, 200)
            text = body.decode("utf-8")
            self.assertIn("Edge Agent", text)
        # Post-save step: page polls /status to learn the device's new IP.
        self.assertIn("/status", text)
        # /status reflects STA state so the phone can learn the new IP.
        status, _, sbody = raw_request(self.port, "GET", "/status")
        self.assertEqual(status, 200)
        data = json.loads(sbody)
        self.assertEqual(data["state"], "waiting")
        self.assertEqual(data["reason"], "")
        self.assertEqual(data["ap_off_in"], 0)
        self.assertEqual(data["port"], cfg.HTTP_PORT)
        # /scan returns deduplicated networks sorted by signal strength.
        network.WLAN.SCAN_RESULTS = [
            (b"WeakNet", b"\x01\x02\x03\x04\x05\x06", 6, -80, 4, 0),
            (b"StrongNet", b"\x01\x02\x03\x04\x05\x07", 1, -40, 3, 0),
            (b"WeakNet", b"\x01\x02\x03\x04\x05\x08", 6, -60, 4, 0),
            (b"FreeWiFi", b"\x01\x02\x03\x04\x05\x09", 11, -55, 0, 0),
            (b"", b"\x01\x02\x03\x04\x05\x0a", 11, -70, 0, 1),
        ]
        try:
            status, _, sbody = raw_request(self.port, "GET", "/scan")
        finally:
            network.WLAN.SCAN_RESULTS = []
        self.assertEqual(status, 200)
        data = json.loads(sbody)
        self.assertEqual(
            data["networks"],
            [
                {"ssid": "StrongNet", "rssi": -40, "secure": True},
                {"ssid": "FreeWiFi", "rssi": -55, "secure": False},
                {"ssid": "WeakNet", "rssi": -60, "secure": True},
            ],
        )
        self.server.set_status("connected", "192.168.2.123", ap_off_at=time.time() + 15)
        _status, _, sbody = raw_request(self.port, "GET", "/status")
        data = json.loads(sbody)
        self.assertEqual(data["state"], "connected")
        self.assertEqual(data["ip"], "192.168.2.123")
        # Countdown of seconds left before the AP shuts itself down.
        self.assertIn(data["ap_off_in"], (14, 15))
        # A failed STA attempt (e.g. wrong password) is exposed with a reason.
        self.server.set_status("failed", reason="wrong_password")
        _status, _, sbody = raw_request(self.port, "GET", "/status")
        data = json.loads(sbody)
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["reason"], "wrong_password")
        # Empty SSID is rejected and does not finish provisioning.
        status, _, _body = raw_request(self.port, "POST", "/wifi", body="ssid=&password=x")
        self.assertEqual(status, 400)
        self.assertFalse(self.server.done)
        # Valid credentials finish provisioning with the parsed result.
        status, _, rbody = raw_request(
            self.port, "POST", "/wifi", body="ssid=TestNet&password=pass123"
        )
        self.assertEqual(status, 200)
        self.assertIn("Saved", rbody.decode("utf-8"))
        time.sleep(0.1)
        self.assertTrue(self.server.done)
        self.assertEqual(self.server.result, ("TestNet", "pass123"))
        # Re-submitting (retry after failure) replaces credentials, bumps the
        # generation counter and goes back to "connecting" with no reason.
        gen = self.server.cred_gen
        status, _, _body = raw_request(
            self.port, "POST", "/wifi", body="ssid=TestNet2&password=pw2"
        )
        self.assertEqual(status, 200)
        time.sleep(0.1)
        self.assertEqual(self.server.cred_gen, gen + 1)
        self.assertEqual(self.server.result, ("TestNet2", "pw2"))
        _status, _, sbody = raw_request(self.port, "GET", "/status")
        data = json.loads(sbody)
        self.assertEqual(data["state"], "connecting")
        self.assertEqual(data["reason"], "")


class TestStaFailReason(unittest.TestCase):
    def test_fail_reason_mapping(self):
        self.assertEqual(_sta_fail_reason(202), "wrong_password")
        self.assertEqual(_sta_fail_reason(204), "wrong_password")
        self.assertEqual(_sta_fail_reason(15), "wrong_password")
        self.assertEqual(_sta_fail_reason(201), "ssid_not_found")
        self.assertEqual(_sta_fail_reason(212), "ssid_not_found")
        self.assertEqual(_sta_fail_reason(203), "fail")
        self.assertIsNone(_sta_fail_reason(1001))
        self.assertIsNone(_sta_fail_reason(1010))


class TestDNSServer(unittest.TestCase):
    def setUp(self):
        self.port = find_free_port(start=10053)
        self.dns = DNSServer(resolve_ip="192.168.4.1", port=self.port)
        self.dns.start()
        self._stopped = False
        _thread.start_new_thread(self._loop, ())
        time.sleep(0.05)

    def tearDown(self):
        self._stopped = True
        self.dns.stop()
        time.sleep(0.02)

    def _loop(self):
        deadline = time.time() + 10
        while time.time() < deadline and not self._stopped:
            self.dns.poll()

    def _query(self, domain):
        packet = bytearray()
        packet.extend(b"\x12\x34")
        packet.extend(b"\x01\x00")
        packet.extend(struct.pack(">HHHH", 1, 0, 0, 0))
        for label in domain.split("."):
            packet.append(len(label))
            packet.extend(label.encode())
        packet.append(0)
        packet.extend(struct.pack(">HH", 1, 1))

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(bytes(packet), socket.getaddrinfo("127.0.0.1", self.port)[0][-1])
        data, _ = s.recvfrom(512)
        s.close()
        return data

    def test_resolves_all_to_self(self):
        for domain in ("captive.apple.com", "connectivitycheck.gstatic.com"):
            resp = self._query(domain)
            self.assertEqual(resp[:2], b"\x12\x34")
            self.assertEqual(resp[2] & 0x80, 0x80)
            self.assertEqual(resp[-4:], bytes([192, 168, 4, 1]))


class TestWiFiManagerCredentials(unittest.TestCase):
    def test_credential_resolution(self):
        from app.controllers import WiFiManager
        import app.config as cfg

        wifi = WiFiManager(ssid="Direct", password="pw")
        self.assertTrue(wifi.has_credentials())

        old_ssid, old_path = cfg.WIFI_SSID, cfg.WIFI_CONFIG_PATH
        cfg.WIFI_SSID = ""
        cfg.WIFI_CONFIG_PATH = "/nonexistent.json"
        try:
            wifi = WiFiManager()
            self.assertFalse(wifi.has_credentials())
        finally:
            cfg.WIFI_SSID, cfg.WIFI_CONFIG_PATH = old_ssid, old_path


if __name__ == "__main__":
    unittest.main(globals())
