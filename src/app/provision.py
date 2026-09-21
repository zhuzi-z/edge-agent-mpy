"""AP mode WiFi provisioning with captive portal (DNS hijack + HTTP)."""

import json
import struct
import time
import socket
import network
import app.config as config
import app.log as log
from app.util import (
    ensure_parent_dir,
    latin1_decode,
    parse_http_request_head,
    safe_decode,
    send_all,
)

PROVISION_HTML = """\
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>EdgeAgent WiFi</title>
<style>
:root{--bg:#fafaf9;--card:#fff;--field:#fafaf9;--fg:#1f1f23;--muted:#78716c;--border:#e7e4de;--input:#e0dcd4;
--primary:#27272a;--primary-fg:#fafafa;--focus:rgba(39,39,42,.14);
--ok-bg:rgba(34,197,94,.12);--ok-fg:#15803d;--err-bg:rgba(239,68,68,.1);--err-fg:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#232323;--card:#2e2e2e;--field:#262626;--fg:#f4f4f5;--muted:#a3a3a3;
--border:#424242;--input:#4a4a4a;--primary:#fafafa;--primary-fg:#1c1c1e;--focus:rgba(250,250,250,.16);
--ok-bg:rgba(34,197,94,.14);--ok-fg:#4ade80;--err-bg:rgba(239,68,68,.14);--err-fg:#f87171}}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:360px;margin:0 auto;
padding:44px 16px;background:var(--bg);color:var(--fg)}
.brand{display:flex;flex-direction:column;align-items:center;gap:8px;margin-bottom:20px}
.logo{width:46px;height:46px;border-radius:14px;background:var(--primary);color:var(--primary-fg);
display:flex;align-items:center;justify-content:center;margin-bottom:4px}
.logo svg{width:23px;height:23px}
h1{margin:0;font-size:1.18em;font-weight:650;letter-spacing:-.01em}
.sub{margin:0;font-size:.8em;color:var(--muted);text-align:center}
.card{background:var(--card);border:1px solid var(--border);padding:22px;border-radius:16px;
box-shadow:0 1px 2px rgba(0,0,0,.05),0 6px 18px rgba(0,0,0,.06)}
label{display:block;margin:12px 0 6px;font-size:.78em;color:var(--muted);font-weight:600}
input{width:100%;padding:10px 12px;background:var(--field);border:1px solid var(--input);border-radius:10px;
font-size:1em;color:var(--fg);outline:none;transition:border-color .12s,box-shadow .12s}
input:focus{border-color:var(--muted);box-shadow:0 0 0 3px var(--focus)}
button{width:100%;margin-top:22px;padding:12px;background:var(--primary);color:var(--primary-fg);border:none;
border-radius:999px;font-size:1em;font-weight:550;cursor:pointer;transition:opacity .12s}
button:active{opacity:.85}
button:disabled{opacity:.5;cursor:default}
.msg{margin-top:16px;padding:12px;border-radius:10px;font-size:.88em;text-align:center;display:none}
.ok{background:var(--ok-bg);color:var(--ok-fg)}
.err{background:var(--err-bg);color:var(--err-fg)}
.msg a{color:inherit;font-weight:650}
.net{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 12px;margin-top:6px;
background:var(--field);border:1px solid var(--input);border-radius:10px;font-size:.88em;cursor:pointer}
.net:active{opacity:.7}
.netmeta{color:var(--muted);font-size:.85em;white-space:nowrap}
.netnote{margin:8px 0 0;font-size:.8em;color:var(--muted);text-align:center}
.netnote a{color:inherit;font-weight:650}
</style>
</head>
<body>
<div class="brand">
<div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg></div>
<h1>Edge Agent</h1>
<p class="sub">Connect your device to a WiFi network to finish setup</p>
</div>
<div class="card">
<form id="wform" onsubmit="save();return false">
<label>WiFi Name (SSID)</label>
<input id="ssid" autofocus autocapitalize="none" autocorrect="off">
<div id="netlist" style="margin-top:8px"></div>
<p class="netnote" id="netnote" style="display:none"></p>
<label>Password</label>
<input id="pass" type="password">
<button id="btn" type="submit" onclick="save()">Save</button>
</form>
<div id="msg" class="msg"></div>
<div id="find" style="display:none">
<p class="sub" id="fhint" style="margin:16px 0 0"></p>
<div id="fmsg" class="msg"></div>
<p class="sub" id="apcount" style="display:none;margin:12px 0 0"></p>
</div>
</div>
<script>
var saving=false;
var lastSubmit=0;
function save(){
if(saving)return;
var s=document.getElementById("ssid").value.trim();
var p=document.getElementById("pass").value;
var m=document.getElementById("msg");
var b=document.getElementById("btn");
if(!s){m.className="msg err";m.style.display="block";m.textContent="SSID is required";return}
saving=true;lastSubmit=Date.now();b.disabled=true;b.textContent="Saving...";
m.className="msg";m.style.display="none";
var x=new XMLHttpRequest();
x.open("POST","/wifi",true);
x.setRequestHeader("Content-Type","application/x-www-form-urlencoded");
x.timeout=8000;
var finished=false;
function proceed(){
if(finished)return;finished=true;saving=false;
m.className="msg ok";m.style.display="block";m.textContent="Saved! Device is joining '"+s+"'.";
document.getElementById("wform").style.display="none";
document.getElementById("fhint").textContent="This setup WiFi stays on for a few minutes. Waiting for the device to get an IP on '"+s+"'...";
document.getElementById("find").style.display="block";
startStatusPoll();
}
x.onload=function(){
if(x.status===200){proceed();}
else{saving=false;b.disabled=false;b.textContent="Save";m.className="msg err";m.style.display="block";m.textContent="Failed, please try again";}
};
x.onerror=function(){proceed();};
x.ontimeout=function(){proceed();};
x.send("ssid="+encodeURIComponent(s)+"&password="+encodeURIComponent(p));
}

var foundDone=false,pollFails=0,pollWaiting=0,pollTimer=null;
function showLink(ip,port,apOffIn){
if(foundDone)return;foundDone=true;
if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
document.getElementById("wform").style.display="none";
document.getElementById("find").style.display="block";
var host=ip+((port&&port!==80)?":"+port:"");
var m=document.getElementById("fmsg");
m.className="msg ok";m.style.display="block";m.innerHTML="Device is online: ";
var a=document.createElement("a");a.href="http://"+host+"/";a.target="_blank";a.textContent="http://"+host+"/";
m.appendChild(a);
document.getElementById("fhint").textContent="Open the link in your browser. If it fails, wait a few seconds and retry.";
if(apOffIn>0){startApCountdown(apOffIn);}
}
var apTimer=null;
function startApCountdown(secs){
if(apTimer){clearInterval(apTimer);apTimer=null;}
var el=document.getElementById("apcount");
el.style.display="block";
function tick(){
if(secs<=0){clearInterval(apTimer);apTimer=null;
el.textContent="This setup WiFi is off now. Join your home WiFi and open the link above.";return;}
el.textContent="This setup WiFi turns off automatically in "+secs+"s";
secs--;
}
tick();
apTimer=setInterval(tick,1000);
}
function pollStatus(){
if(foundDone)return;
var x=new XMLHttpRequest();
x.open("GET","/status",true);
x.timeout=2500;
x.onload=function(){
if(foundDone)return;
pollFails=0;
var m=document.getElementById("fmsg");
try{
var d=JSON.parse(x.responseText);
if(d.state==="connected"&&d.ip){showLink(d.ip,d.port,d.ap_off_in);return;}
if(d.state==="failed"){
if(Date.now()-lastSubmit<3000){pollTimer=setTimeout(pollStatus,500);return;}
showFailedError(d.reason);pollTimer=setTimeout(pollStatus,2000);return;}
if(d.state==="waiting"){if(++pollWaiting>=5){showWaitingError();return;}}
else{pollWaiting=0;}
m.className="msg ok";m.style.display="block";
m.textContent="Device is connecting, waiting for an IP...";
}catch(e){}
pollTimer=setTimeout(pollStatus,1500);
};
x.onerror=x.ontimeout=function(){
if(foundDone)return;
pollFails++;
if(pollFails===4){
var m=document.getElementById("fmsg");m.className="msg";m.style.display="none";
document.getElementById("fhint").textContent="If this phone has already rejoined your home WiFi, use the button below to find the device.";
}
if(pollFails<90)pollTimer=setTimeout(pollStatus,2000);
};
x.send();
}
var FAIL_TEXT={
"wrong_password":"Connection failed: wrong WiFi password. Check the password and try again.",
"ssid_not_found":"Connection failed: WiFi network not found. Check the WiFi name and try again.",
"timeout":"Connection timed out: the device could not join the WiFi. Check the password and try again.",
"fail":"Connection failed. Please try again."};
function showFormAgain(text){
if(foundDone)return;
document.getElementById("find").style.display="none";
document.getElementById("wform").style.display="block";
var b=document.getElementById("btn");b.disabled=false;b.textContent="Save";
var m=document.getElementById("msg");
m.className="msg err";m.style.display="block";
m.textContent=text;
var fm=document.getElementById("fmsg");fm.className="msg";fm.style.display="none";
}
function showWaitingError(){
showFormAgain("The device did not receive your WiFi settings. Please try again.");
}
function showFailedError(reason){
var key=(reason&&FAIL_TEXT[reason])?reason:"fail";
showFormAgain(FAIL_TEXT[key]);
if(key==="wrong_password"||key==="timeout"){document.getElementById("pass").value="";}
}
function startStatusPoll(){
pollWaiting=0;pollFails=0;
if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
var m=document.getElementById("fmsg");
m.className="msg ok";m.style.display="block";
m.textContent="Device is connecting, waiting for an IP...";
pollStatus();
}
function loadNets(){
var nl=document.getElementById("netlist");
document.getElementById("netnote").style.display="none";
nl.style.display="block";
nl.innerHTML='<p class="netnote">Scanning for nearby WiFi networks...</p>';
var x=new XMLHttpRequest();
x.open("GET","/scan",true);
x.timeout=15000;
x.onload=function(){
var nets=[];
try{nets=JSON.parse(x.responseText).networks||[];}catch(e){}
renderNets(nets);
};
x.onerror=x.ontimeout=function(){renderNets(null);};
x.send();
}
function renderNets(nets){
var nl=document.getElementById("netlist");
nl.innerHTML="";
if(nets===null){nl.innerHTML='<p class="netnote">Scan failed. Please type the WiFi name manually.</p>';return;}
if(!nets.length){nl.innerHTML='<p class="netnote">No WiFi found. Please type the name manually.</p>';return;}
for(var i=0;i<nets.length;i++){
(function(n){
var d=document.createElement("div");
d.className="net";
var s=document.createElement("span");
s.textContent=n.ssid;
var meta=document.createElement("span");
meta.className="netmeta";
meta.textContent=n.rssi+" dBm"+(n.secure?" 🔒":" · open");
d.appendChild(s);d.appendChild(meta);
d.onclick=function(){pickNet(n.ssid);};
nl.appendChild(d);
})(nets[i]);
}
var note=document.createElement("p");
note.className="netnote";
var link=document.createElement("a");
link.href="#";link.textContent="Scan again";
link.onclick=function(){loadNets();return false;};
note.appendChild(link);
nl.appendChild(note);
}
function pickNet(name){
document.getElementById("ssid").value=name;
document.getElementById("netlist").style.display="none";
var note=document.getElementById("netnote");
note.style.display="block";
note.textContent="Selected: "+name+" ";
var a=document.createElement("a");
a.href="#";a.textContent="change";
a.onclick=function(){note.style.display="none";loadNets();return false;};
note.appendChild(a);
try{document.getElementById("pass").focus();}catch(e){}
}
loadNets();
</script>
</body>
</html>
"""


RESULT_OK_HTML = """\
<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>EdgeAgent WiFi</title>
<style>
:root{--bg:#fafaf9;--fg:#15803d;--box:rgba(34,197,94,.12);--border:rgba(34,197,94,.35)}
@media(prefers-color-scheme:dark){:root{--bg:#232323;--fg:#4ade80;--box:rgba(34,197,94,.14);--border:rgba(34,197,94,.4)}}
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:360px;margin:0 auto;
padding:44px 16px;background:var(--bg)}
.box{background:var(--box);color:var(--fg);border:1px solid var(--border);padding:20px;border-radius:16px;
text-align:center;font-size:.95em}
svg{width:30px;height:30px;margin-bottom:8px}
p{margin:0}
</style>
</head>
<body><div class="box"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg><p>Saved! Device is connecting to WIFI_NAME...</p></div>
</body>
</html>
"""

RESULT_ERR_HTML = """\
<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>EdgeAgent WiFi</title>
<style>
:root{--bg:#fafaf9;--fg:#b91c1c;--box:rgba(239,68,68,.1);--border:rgba(239,68,68,.35)}
@media(prefers-color-scheme:dark){:root{--bg:#232323;--fg:#f87171;--box:rgba(239,68,68,.14);--border:rgba(239,68,68,.4)}}
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:360px;margin:0 auto;
padding:44px 16px;background:var(--bg)}
.box{background:var(--box);color:var(--fg);border:1px solid var(--border);padding:20px;border-radius:16px;
text-align:center;font-size:.95em}
svg{width:30px;height:30px;margin-bottom:8px}
p{margin:0}
</style>
</head>
<body><div class="box"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><p>SSID is required</p></div>
</body>
</html>
"""


def load_wifi_config(path=None):
    """Load saved WiFi config from flash. Returns (ssid, password) or (None, None)."""
    path = path or config.WIFI_CONFIG_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        if ssid:
            return ssid, password
    except (OSError, ValueError):
        pass
    return None, None


def save_wifi_config(ssid, password, path=None):
    """Save WiFi credentials to flash."""
    path = path or config.WIFI_CONFIG_PATH
    try:
        ensure_parent_dir(path)
        with open(path, "w") as f:
            json.dump({"ssid": ssid, "password": password}, f)
        return True
    except OSError as e:
        log.error("Provision", "save failed: {}".format(e))
        return False


def _parse_form_body(body):
    """Parse application/x-www-form-urlencoded body into dict."""
    params = {}
    for pair in body.split("&"):
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        params[_url_decode(key)] = _url_decode(val)
    return params


def _url_decode(s):
    """Decode percent-encoded string with UTF-8 support."""
    s = s.replace("+", " ")
    buf = bytearray()
    i = 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            try:
                buf.append(int(s[i + 1 : i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        buf.append(ord(s[i]))
        i += 1
    try:
        return buf.decode("utf-8")
    except (UnicodeError, LookupError):
        return latin1_decode(buf)


class DNSServer:
    """Minimal DNS server that resolves ALL queries to a fixed IP (captive portal hijack)."""

    def __init__(self, resolve_ip="192.168.4.1", port=53):
        self._ip = resolve_ip
        self._port = port
        self._socket = None

    def start(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(socket.getaddrinfo("0.0.0.0", self._port)[0][-1])
        self._socket.settimeout(0.5)
        log.info("Provision", "DNS hijack on :{}".format(self._port))

    def stop(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def poll(self):
        """Handle one DNS query if available. Non-blocking via timeout."""
        if self._socket is None:
            return
        try:
            data, addr = self._socket.recvfrom(256)
        except OSError:
            return
        if len(data) < 12:
            return
        resp = self._build_response(data)
        if resp:
            try:
                self._socket.sendto(resp, addr)
            except OSError:
                pass

    def _build_response(self, query):
        """Build a DNS A-record response pointing to self._ip."""
        txn_id = query[:2]
        # Find end of question section (first null byte after header)
        idx = 12
        while idx < len(query) and query[idx] != 0:
            idx += 1
        idx += 1  # skip null terminator
        # question section: name + QTYPE(2) + QCLASS(2)
        question_end = idx + 4
        question = query[12:question_end]

        ip_parts = self._ip.split(".")
        ip_bytes = bytes([int(p) for p in ip_parts])

        # Header: response, authoritative, 1 question, 1 answer
        header = txn_id + b"\x81\x80" + struct.pack(">HHHH", 1, 1, 0, 0)
        # Answer: pointer to question name (0xC00C), type A, class IN, TTL 60, 4-byte IP
        answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + ip_bytes
        return header + question + answer


class ProvisionServer:
    """Captive portal HTTP server for WiFi provisioning in AP mode."""

    def __init__(self, port=80):
        self._port = port
        self._socket = None
        self._done = False
        self._ssid = None
        self._password = None
        self._state = "waiting"
        self._sta_ip = None
        self._reason = ""
        self._cred_gen = 0
        self._ap_off_at = None

    def set_status(self, state, ip=None, reason="", ap_off_at=None):
        """Update STA state shown to the phone via GET /status."""
        self._state = state
        self._sta_ip = ip
        self._reason = reason
        self._ap_off_at = ap_off_at

    @property
    def done(self):
        return self._done

    @property
    def result(self):
        return self._ssid, self._password

    @property
    def cred_gen(self):
        """Incremented on every valid /wifi submit so callers can spot retries."""
        return self._cred_gen

    def start(self):
        addr = socket.getaddrinfo("0.0.0.0", self._port)[0][-1]
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(addr)
        # Backlog > 1: a blocking /scan may hold the loop while the phone's
        # captive-portal probes and /status polls keep arriving.
        self._socket.listen(4)
        self._socket.settimeout(0.5)
        log.info("Provision", "Portal listening on {}".format(addr))

    def stop(self):
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def accept_and_handle(self):
        if self._socket is None:
            return
        try:
            client_sock, addr = self._socket.accept()
        except OSError:
            return
        try:
            self._handle_client(client_sock)
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def _handle_client(self, sock):
        sock.settimeout(config.HTTP_CLIENT_TIMEOUT_SEC)
        request = bytearray()
        while b"\r\n\r\n" not in request:
            try:
                chunk = sock.recv(config.HTTP_RECV_BUF_SIZE)
            except OSError:
                return
            if not chunk:
                return
            request.extend(chunk)
            if len(request) > config.HTTP_MAX_HEADER_SIZE:
                return

        hend = request.find(b"\r\n\r\n")
        parsed = parse_http_request_head(bytes(request[:hend]))
        if parsed is None:
            return
        method, path, content_length = parsed

        body = ""
        if content_length > 0:
            body_start = hend + 4
            remaining = content_length - (len(request) - body_start)
            while remaining > 0:
                try:
                    chunk = sock.recv(min(config.HTTP_RECV_BUF_SIZE, remaining))
                except OSError:
                    break
                if not chunk:
                    break
                request.extend(chunk)
                remaining -= len(chunk)
            body = safe_decode(request[body_start : body_start + content_length])

        if method == "GET" and path == "/status":
            left = 0
            if self._ap_off_at is not None and self._ap_off_at > time.time():
                left = self._ap_off_at - time.time()
            ap_off_in = int(left)
            if left > ap_off_in:
                ap_off_in += 1
            data = {
                "state": self._state,
                "ssid": self._ssid or "",
                "ip": self._sta_ip or "",
                "reason": self._reason,
                "ap_off_in": ap_off_in,
                "port": config.HTTP_PORT,
            }
            self._send(sock, 200, "OK", "application/json", json.dumps(data))
        elif method == "GET" and path == "/scan":
            self._handle_scan(sock)
        elif method == "POST" and path == "/wifi":
            self._handle_wifi_post(sock, body)
        else:
            self._send(sock, 200, "OK", "text/html; charset=utf-8", PROVISION_HTML)

    def _handle_wifi_post(self, sock, body):
        params = _parse_form_body(body)
        ssid = params.get("ssid", "").strip()
        password = params.get("password", "")
        if not ssid:
            self._send(sock, 400, "Bad Request", "text/html; charset=utf-8", RESULT_ERR_HTML)
            return
        self._ssid = ssid
        self._password = password
        self._cred_gen += 1
        self._done = True
        self._state = "connecting"
        self._reason = ""
        html = RESULT_OK_HTML.replace("WIFI_NAME", ssid)
        self._send(sock, 200, "OK", "text/html; charset=utf-8", html)
        time.sleep(0.3)  # let the stack flush before the socket is closed

    def _handle_scan(self, sock):
        """Scan visible WiFi networks so the user can pick one by tapping."""
        nets = []
        try:
            sta = network.WLAN(network.STA_IF)
            if not sta.active():
                sta.active(True)
            best = {}
            for entry in sta.scan():
                ssid = entry[0]
                if isinstance(ssid, (bytes, bytearray)):
                    try:
                        ssid = ssid.decode("utf-8")
                    except UnicodeError:
                        continue
                ssid = ssid.strip()
                if not ssid:
                    continue
                rssi = entry[3]
                prev = best.get(ssid)
                if prev is None or rssi > prev[0]:
                    best[ssid] = (rssi, entry[4] != 0)
            nets = [{"ssid": k, "rssi": v[0], "secure": v[1]} for k, v in best.items()]
            nets.sort(key=lambda item: item["rssi"], reverse=True)
            nets = nets[: config.AP_SCAN_MAX_RESULTS]
        except OSError as e:
            log.warn("Provision", "scan failed: {}".format(e))
        self._send(sock, 200, "OK", "application/json", json.dumps({"networks": nets}))

    def _send(self, sock, code, text, content_type, body):
        body_bytes = body.encode("utf-8")
        header = "HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".format(
            code, text, content_type, len(body_bytes)
        )
        try:
            send_all(sock, header.encode("utf-8") + body_bytes)
        except OSError:
            pass


def start_ap(ssid=None, password=None):
    """Start AP interface. Returns the AP WLAN object."""
    ssid = ssid or config.AP_SSID
    password = password or config.AP_PASSWORD
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    if password:
        ap.config(essid=ssid, password=password, authmode=3)
    else:
        ap.config(essid=ssid)
    log.info("Provision", "AP started: {}".format(ssid))
    return ap


# ESP32 disconnect reasons (esp_wifi_types.h) surfaced by WLAN.status() that
# mean the STA connect attempt terminally failed. MicroPython maps
# AUTH_FAIL/CONNECTION_FAIL to STAT_WRONG_PASSWORD (202) and passes unknown
# reasons through unchanged.
_STA_FAIL_REASONS = {
    getattr(network, "STAT_WRONG_PASSWORD", 202): "wrong_password",
    getattr(network, "STAT_HANDSHAKE_TIMEOUT", 204): "wrong_password",
    15: "wrong_password",  # WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT
    getattr(network, "STAT_NO_AP_FOUND", 201): "ssid_not_found",
    210: "ssid_not_found",  # WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY
    211: "ssid_not_found",  # WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD
    212: "ssid_not_found",  # WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD
    getattr(network, "STAT_CONNECT_FAIL", 203): "fail",
}


# Statuses that mean "no pending failure": seeing one proves the disconnect
# reason left over from a previous attempt has been overwritten, so any
# failure code observed afterwards belongs to the current attempt.
_STA_PROGRESS_CODES = (
    getattr(network, "STAT_IDLE", 1000),
    getattr(network, "STAT_CONNECTING", 1001),
)


def _sta_fail_reason(status):
    """Map an STA status code to a failure key, or None if not a failure."""
    return _STA_FAIL_REASONS.get(status)


def _sta_connect(sta, ssid, password):
    """(Re)start an STA connect attempt. Returns the attempt start time."""
    try:
        sta.disconnect()
    except OSError:
        pass
    sta.connect(ssid, password)
    return time.time()


def run_provision(timeout_sec=None):
    """Run full provisioning flow: AP + DNS hijack + captive portal.

    Returns (ssid, password) or (None, None) on timeout.
    """
    timeout_sec = timeout_sec if timeout_sec is not None else config.AP_TIMEOUT_SEC
    ap = start_ap()
    ap_ip = ap.ifconfig()[0]

    dns = DNSServer(resolve_ip=ap_ip)
    dns.start()

    portal = ProvisionServer()
    portal.start()

    start_time = time.time()
    log.info("Provision", "Waiting for user config (timeout {}s)...".format(timeout_sec))

    while not portal.done:
        if time.time() - start_time > timeout_sec:
            log.warn("Provision", "Timeout, no config received")
            portal.stop()
            dns.stop()
            ap.active(False)
            log.info("Provision", "AP deactivated")
            return None, None
        dns.poll()
        portal.accept_and_handle()
        time.sleep(0.01)

    ssid, password = portal.result
    log.info("Provision", "Got credentials for: {}".format(ssid))
    save_wifi_config(ssid, password)

    # Keep the AP + portal alive: the user's phone is still connected to it
    # and polls /status to learn the device's new IP. Meanwhile the STA
    # connects in the background. On failure (e.g. wrong password) the reason
    # is pushed to /status so the page can show it and let the user retry;
    # a re-submitted POST /wifi then restarts the STA attempt.
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    cred_gen = portal.cred_gen
    attempt_start = _sta_connect(sta, ssid, password)
    log.info("Provision", "STA connecting to {}; AP kept for /status".format(ssid))

    connected_since = None
    fail_reported = False
    saw_clean = False
    keepalive_end = time.time() + config.AP_POST_KEEPALIVE_SEC
    while time.time() < keepalive_end:
        dns.poll()
        portal.accept_and_handle()
        if portal.cred_gen != cred_gen:
            ssid, password = portal.result
            cred_gen = portal.cred_gen
            save_wifi_config(ssid, password)
            attempt_start = _sta_connect(sta, ssid, password)
            log.info("Provision", "New credentials for {}, retrying".format(ssid))
            connected_since = None
            fail_reported = False
            saw_clean = False
            keepalive_end = time.time() + config.AP_POST_KEEPALIVE_SEC
            continue
        if sta.isconnected():
            if connected_since is None:
                connected_since = time.time()
                sta_ip = sta.ifconfig()[0]
                portal.set_status(
                    "connected",
                    sta_ip,
                    ap_off_at=connected_since + config.AP_POST_GRACE_SEC,
                )
                log.info("Provision", "STA IP: {}".format(sta_ip))
                log.info("WebUI", "http://{}:{}".format(sta_ip, config.HTTP_PORT))
            elif time.time() - connected_since >= config.AP_POST_GRACE_SEC:
                break
        elif not fail_reported:
            elapsed = time.time() - attempt_start
            reason = None
            try:
                status = sta.status()
            except OSError:
                status = None
            mapped = _sta_fail_reason(status)
            if status in _STA_PROGRESS_CODES:
                saw_clean = True
            elif mapped is not None and saw_clean and elapsed >= config.AP_FAIL_DETECT_GRACE_SEC:
                reason = mapped
            if reason is None and elapsed >= config.AP_CONNECT_TIMEOUT_SEC:
                reason = "timeout"
            if reason:
                fail_reported = True
                portal.set_status("failed", reason=reason)
                log.warn(
                    "Provision",
                    "STA failed ({}); reported to portal, waiting for retry".format(reason),
                )
        time.sleep(0.01)

    portal.stop()
    dns.stop()
    ap.active(False)
    log.info("Provision", "AP deactivated")
    return ssid, password
