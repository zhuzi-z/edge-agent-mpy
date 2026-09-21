"""network module shim (instant connect, loopback IP)."""

STA_IF = 0
AP_IF = 1


class WLAN:
    # Tests may inject scan entries: (ssid, bssid, channel, rssi, security, hidden)
    SCAN_RESULTS = []

    def __init__(self, interface_id=None):
        self._interface_id = interface_id
        self._active = False
        self._connected = False
        self._essid = ""
        self._config_dict = {}

    def active(self, val=None):
        if val is None:
            return self._active
        self._active = bool(val)

    def connect(self, ssid=None, password=None):
        if self._active:
            self._connected = True

    def disconnect(self):
        self._connected = False

    def isconnected(self):
        return self._connected

    def ifconfig(self, config=None):
        if config is not None:
            return None
        if self._connected:
            return ("127.0.0.1", "255.255.255.0", "127.0.0.1", "127.0.0.1")
        if self._interface_id == AP_IF and self._active:
            return ("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.1")
        return ("0.0.0.0", "0.0.0.0", "0.0.0.0", "0.0.0.0")

    def status(self, param=None):
        return 3 if self._connected else 0

    def scan(self):
        return list(self.SCAN_RESULTS)

    def config(self, param=None, **kwargs):
        if param is not None:
            return self._config_dict.get(param)
        self._config_dict.update(kwargs)
        if "essid" in kwargs:
            self._essid = kwargs["essid"]
        return None
