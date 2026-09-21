"""WiFi management and GPIO helpers."""

import time
import asyncio
import machine
import network
import app.config as config
import app.log as log
from app.provision import load_wifi_config


class WiFiManager:
    """WiFi connection and reconnection."""

    def __init__(self, ssid=None, password=None):
        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)
        if ssid is None:
            ssid, password = self._resolve_credentials()
        self._ssid = ssid or ""
        self._password = password or ""

    def _resolve_credentials(self):
        """Load from flash first, fall back to config defaults."""
        ssid, password = load_wifi_config()
        if ssid:
            log.info("WiFi", "Using saved credentials: {}".format(ssid))
            return ssid, password
        if config.WIFI_SSID:
            log.info("WiFi", "Using default credentials: {}".format(config.WIFI_SSID))
            return config.WIFI_SSID, config.WIFI_PASS
        return None, None

    def has_credentials(self):
        return bool(self._ssid)

    def reload_credentials(self):
        """Re-resolve credentials from flash/config in place.

        Used after re-provisioning so existing references to this manager
        (HTTP server, skills) keep working without re-wiring.
        """
        ssid, password = self._resolve_credentials()
        self._ssid = ssid or ""
        self._password = password or ""

    def is_connected(self):
        return self._wlan.isconnected()

    def connect(self):
        """Connect to WiFi. Returns True on success."""
        if self._wlan.isconnected():
            return True
        if not self._ssid:
            return False
        log.info("WiFi", "Connecting to {}...".format(self._ssid))
        self._wlan.connect(self._ssid, self._password)
        for _ in range(config.WIFI_RETRY_COUNT):
            if self._wlan.isconnected():
                self._log_connected()
                return True
            time.sleep(config.WIFI_RETRY_INTERVAL_SEC)
        log.warn("WiFi", "FAIL")
        return False

    async def connect_async(self):
        """Connect to WiFi, yielding to the event loop between polls.

        Reconnect attempts happen while channels keep running (WeChat
        long-poll backoff, HTTP handlers), unlike blocking connect().
        """
        if self._wlan.isconnected():
            return True
        if not self._ssid:
            return False
        log.info("WiFi", "Connecting to {}...".format(self._ssid))
        self._wlan.connect(self._ssid, self._password)
        for _ in range(config.WIFI_RETRY_COUNT):
            if self._wlan.isconnected():
                self._log_connected()
                return True
            await asyncio.sleep(config.WIFI_RETRY_INTERVAL_SEC)
        log.warn("WiFi", "FAIL")
        return False

    def _log_connected(self):
        ip = self._wlan.ifconfig()[0]
        log.info("WiFi", "OK IP: {}".format(ip))
        log.info("WebUI", "http://{}:{}".format(ip, config.HTTP_PORT))

    def ifconfig(self):
        return self._wlan.ifconfig()

    def sync_ntp(self):
        """Best-effort NTP sync (needed for signed API requests)."""
        try:
            import ntptime

            ntptime.host = "ntp.aliyun.com"
            ntptime.settime()
            log.info("WiFi", "NTP sync OK, epoch={}".format(time.time()))
            return True
        except Exception as e:  # noqa: BLE001
            log.warn("WiFi", "NTP sync failed: {}".format(e))
            return False


_gpio_cache = {}


def read_gpio_states(pins=None, max_pin=48):
    """Return {pin: level} for requested readable GPIOs."""
    states = {}
    pin_numbers = pins if pins is not None else range(0, max_pin + 1)
    for n in pin_numbers:
        pin = _gpio_cache.get(n)
        if pin is None:
            try:
                pin = machine.Pin(n)
                _gpio_cache[n] = pin
            except Exception:  # noqa: BLE001
                continue
        try:
            states[n] = pin.value()
        except Exception:  # noqa: BLE001
            continue
    return states
