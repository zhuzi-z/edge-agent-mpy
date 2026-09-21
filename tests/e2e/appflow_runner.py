#!/usr/bin/env python3
"""App-flow integration test orchestrator (CPython).

Starts the full Edge Agent (MicroPython unix port) with socket-mocked
audio hardware, plus a fake LLM/ASR/TTS TLS server. Then:
  - WebUI channel: HTTP requests to the agent
  - Voice channel: trigger wake word ("Hi ESP") → stream PCM into the
    mock mic socket → read TTS from the mock speaker socket

Usage:
    python3 tests/e2e/appflow_runner.py
    # or: make e2e
"""

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_RATE = 16000
TEST_QUESTION = "who are you?"
TEST_ANSWER = "I'm an edge agent running with MicroPython"
CHUNK_BYTES = 3200  # 100ms at 16kHz 16-bit mono

AGENT_PORT = 18080
FAKE_PORT = 19100
MIC_PORT = 19301
SPK_PORT = 19302
WAKE_PORT = 19303

_passed = 0
_failed = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_free_port(start):
    for p in range(start, start + 200):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    raise RuntimeError("no free port")


def wait_http_ready(port, timeout=15, use_tls=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if use_tls:
                import ssl

                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = ctx.wrap_socket(
                    socket.create_connection(("127.0.0.1", port), timeout=2),
                    server_hostname="127.0.0.1",
                )
            else:
                conn = socket.create_connection(("127.0.0.1", port), timeout=2)
            conn.close()
            return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.2)
    return False


def http_post_json(port, path, obj):
    body = json.dumps(obj).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    status = resp.status
    conn.close()
    return status, data


def https_post_json(port, path, obj):
    """POST JSON to the fake server over TLS (self-signed cert)."""
    import ssl

    body = json.dumps(obj).encode("utf-8")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    conn = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
    req = (
        "POST {} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: {}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).format(path, len(body))
    conn.sendall(req.encode("utf-8") + body)
    resp = b""
    conn.settimeout(10)
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            resp += chunk
    except OSError:
        pass
    conn.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(payload.decode("utf-8"))


def http_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode("utf-8"))
    status = resp.status
    conn.close()
    return status, data


def silence_pcm(nbytes):
    """Generate near-silent PCM (below VAD threshold)."""
    return b"\x01\x00" * (nbytes // 2)


def trigger_wakeword(port, word="Hi ESP"):
    """Send a wake word trigger to the esp_sr stub's TCP server."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(word.encode("utf-8"))
    s.close()
    time.sleep(0.3)


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  ✓ {}".format(name))
    else:
        _failed += 1
        print("  ✗ {} {}".format(name, detail))


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------


def test_webui_channel(port):
    print("\n[WebUI Channel]")

    status, data = http_post_json(port, "/agent", {"message": TEST_QUESTION})
    check("basic chat status", status == 200, "got {}".format(status))
    check(
        "basic chat reply", data.get("reply") == TEST_ANSWER, "got: {}".format(data.get("reply"))
    )

    status, data = http_post_json(port, "/agent", {"message": "hello"})
    check("echo reply", data.get("reply") == "Echo: hello", "got: {}".format(data.get("reply")))

    status, data = http_post_json(port, "/agent", {"message": "/history"})
    check("history endpoint", status == 200)


def test_voice_wakeword_channel(mic_port, spk_port, wake_port):
    """Wake word 'Hi ESP' triggers listening state (esp-sr path only)."""
    print("\n[Voice Channel — Wake Word 'Hi ESP']")

    # Trigger wake word
    print("  (triggering wake word 'Hi ESP'...)")
    trigger_wakeword(wake_port, "Hi ESP")
    check("wake word trigger sent", True)

    # Agent should enter LISTENING and open mic
    time.sleep(0.5)
    try:
        mic = socket.create_connection(("127.0.0.1", mic_port), timeout=10)
        check("mic socket connected after wakeup", True)
        mic.close()
    except (OSError, ConnectionRefusedError) as e:
        check("mic socket connected after wakeup", False, str(e))


def test_voice_wakeword_idle_timeout(mic_port, spk_port, wake_port):
    """Wake word triggers listening, but no speech → timeout → back to idle."""
    print("\n[Voice Channel — Listen Timeout]")

    # Trigger wake word
    trigger_wakeword(wake_port, "Hi ESP")
    time.sleep(0.5)

    # Connect to mic but send only silence (VAD never commits)
    try:
        mic = socket.create_connection(("127.0.0.1", mic_port), timeout=10)
        check("mic connected for timeout test", True)
    except (OSError, ConnectionRefusedError) as e:
        check("mic connected for timeout test", False, str(e))
        return

    # Send silence for a short period then close
    try:
        for _ in range(5):
            mic.sendall(silence_pcm(CHUNK_BYTES))
            time.sleep(0.05)
    except (BrokenPipeError, OSError):
        pass
    try:
        mic.close()
    except OSError:
        pass

    # After timeout, agent should return to idle and restart esp_sr.
    # Verify by triggering wake word again — mic should become available.
    time.sleep(3)  # wait for VOICE_LISTEN_TIMEOUT_MS (2s in appflow) + margin
    trigger_wakeword(wake_port, "Hi ESP")
    time.sleep(0.5)

    try:
        mic2 = socket.create_connection(("127.0.0.1", mic_port), timeout=5)
        check("re-wakeup after timeout", True)
        mic2.close()
    except (OSError, ConnectionRefusedError) as e:
        check("re-wakeup after timeout", False, str(e))


def test_config_endpoint(port):
    print("\n[Config]")
    status, data = http_get(port, "/config")
    check("config accessible", status == 200)
    check("api_key masked", data.get("api_key") == "***")
    test_config_backup(port)
    test_volume_tone(port)


def test_config_backup(port):
    """Export the settings as a backup file, drift away, restore from it."""
    status, backup = http_get(port, "/config/export")
    check("config export accessible", status == 200)
    exported = backup.get("config") or {}
    check("export carries the real api_key", exported.get("api_key") == "sk-fake")
    model = exported.get("model")
    check("export carries the seeded model", model == "fake-model")

    http_post_json(port, "/config", {"model": "drifted"})
    status, data = http_post_json(port, "/config/import", backup)
    check("config import accepted", status == 200 and data.get("status") == "ok")
    status, after = http_get(port, "/config")
    check("import restored the model", after.get("model") == model)
    check("import left the display masked", after.get("api_key") == "***")

    status, data = http_post_json(port, "/config/import", {"kind": "not-a-backup"})
    check("foreign file refused", status == 400 and "error" in data)
    status, after = http_get(port, "/config")
    check("refused file changed nothing", after.get("model") == model)


def test_volume_tone(port):
    """POST /volume/test plays a beep through the (socket) speaker."""
    status, _ = http_post_json(port, "/config", {"volume": 50})
    check("volume saved", status == 200)
    # The speaker socket listens since the first TTS; connect before the
    # POST because the agent's write blocks until a client is accepted.
    spk = connect_with_retry(SPK_PORT, timeout=10)
    if spk is None:
        check("volume test tone played", False, "speaker port not opened")
        return
    status, _ = http_post_json(port, "/volume/test", {})
    total = 0
    spk.settimeout(15)
    try:
        while True:
            chunk = spk.recv(4096)
            if not chunk:
                break
            total += len(chunk)
    except OSError:
        pass
    spk.close()
    check("volume test tone played", status == 200 and total > 0, "{} bytes".format(total))


def synthesize_speech(text):
    """Synthesize real speech (piper, same voice as the fake server)."""
    import io
    import wave

    from piper import PiperVoice

    model_path = os.environ.get(
        "PIPER_MODEL", os.path.expanduser("~/.local/share/piper/en_US-lessac-low.onnx")
    )
    voice = PiperVoice.load(model_path)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    buf.seek(0)
    with wave.open(buf, "rb") as wav_file:
        return wav_file.readframes(wav_file.getnframes())


def wait_for_idle(mic_port, timeout=15):
    """Wait until no listening session holds the mic port (agent idle).

    A LISTENING session always keeps the mic port open, so a refused
    connect means the agent is back in the wake stage. A connect timeout
    (e.g. the accept backlog is full of stale probes) still means the
    session is alive: keep waiting.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", mic_port))
        except ConnectionRefusedError:
            probe.close()
            return True
        except OSError:
            probe.close()
            time.sleep(0.2)
            continue
        probe.close()
        time.sleep(0.2)
    return False


def connect_with_retry(port, timeout=10):
    """Connect with retries (agent may open the port a moment later)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.1)
    return None


def stream_utterance(sock, speech_pcm):
    """Stream lead silence (VAD calibration) + speech + trailing silence.

    The agent's VAD timing is sample-based (each chunk = 100ms of
    audio), so chunks can go out faster than real time. The agent closes
    the mic socket as soon as it commits the utterance, so a reset in
    the trailing loop means speech was consumed.
    """
    for _ in range(6):
        sock.sendall(silence_pcm(CHUNK_BYTES))
        time.sleep(0.05)
    off = 0
    while off < len(speech_pcm):
        sock.sendall(speech_pcm[off : off + CHUNK_BYTES])
        off += CHUNK_BYTES
        time.sleep(0.02)
    for _ in range(20):
        try:
            sock.sendall(silence_pcm(CHUNK_BYTES))
        except OSError:
            return
        time.sleep(0.03)


def read_until_eof(sock, timeout=60):
    sock.settimeout(timeout)
    total = 0
    try:
        while True:
            data = sock.recv(8192)
            if not data:
                break
            total += len(data)
    except socket.timeout:
        pass
    sock.close()
    return total


def test_voice_followup(mic_port, spk_port, wake_port):
    """After TTS the agent keeps listening: a follow-up is processed without
    a wake word, and silence returns the channel to the wake stage."""
    print("\n[Voice Follow-Up]")

    # Previous tests leave listening sessions that run out their timeout;
    # wait for the mic port to close (agent back in the wake stage) so the
    # wake word below starts a fresh round.
    wait_for_idle(mic_port)

    speech_pcm = synthesize_speech(TEST_QUESTION)

    # Round 1: wake word + utterance -> TTS
    trigger_wakeword(wake_port)
    mic = connect_with_retry(mic_port)
    if mic is None:
        check("mic connect for conversation", False, "mic port not opened")
        return
    stream_utterance(mic, speech_pcm)
    mic.close()

    spk = connect_with_retry(spk_port, timeout=90)
    if spk is None:
        check("round 1 tts played", False, "speaker port not opened")
        return
    check("round 1 tts played", read_until_eof(spk) > 0)

    # Follow-up window: agent listens again without a wake word.
    mic2 = connect_with_retry(mic_port, timeout=8)
    if mic2 is None:
        check("follow-up listening opened", False, "mic port not reopened after TTS")
        return
    check("follow-up listening opened", True)
    stream_utterance(mic2, speech_pcm)
    mic2.close()

    spk2 = connect_with_retry(spk_port, timeout=90)
    if spk2 is None:
        check("round 2 tts played (no wake word)", False, "speaker port not reopened")
        return
    check("round 2 tts played (no wake word)", read_until_eof(spk2) > 0)

    # Second follow-up window: stream only silence -> back to wake stage.
    mic3 = connect_with_retry(mic_port, timeout=8)
    if mic3 is None:
        check("silence returns to wake stage", False, "follow-up window not reopened")
        return
    try:
        while True:
            mic3.sendall(silence_pcm(CHUNK_BYTES))
            time.sleep(0.1)
    except OSError:
        pass  # agent closed the mic socket when the window timed out
    mic3.close()

    # Wake word works again after the silent follow-up window expired.
    trigger_wakeword(wake_port)
    mic4 = connect_with_retry(mic_port, timeout=8)
    if mic4 is not None:
        mic4.close()
    check("silence returns to wake stage", mic4 is not None)


def test_voice_exit_phrase(mic_port, spk_port, wake_port):
    """Saying something like "you can stand down" ends the conversation: the LLM calls
    the voice_control skill (action=exit), the agent acknowledges via TTS
    and returns to the wake stage (no follow-up window)."""
    print("\n[Voice Exit Phrase]")

    # Synthesize before waking: piper latency must not eat into the
    # agent's listen timeout window.
    speech_pcm = synthesize_speech(TEST_QUESTION)
    wait_for_idle(mic_port)
    status, _ = https_post_json(FAKE_PORT, "/__asr_next", {"text": "you can stand down"})
    check("fake asr override armed", status == 200)

    trigger_wakeword(wake_port)
    mic = connect_with_retry(mic_port)
    if mic is None:
        check("mic connect for exit round", False, "mic port not opened")
        return
    stream_utterance(mic, speech_pcm)
    mic.close()

    spk = connect_with_retry(spk_port, timeout=90)
    if spk is None:
        check("exit ack tts played", False, "speaker port not opened")
        return
    check("exit ack tts played", read_until_eof(spk) > 0)

    # No follow-up window after the ack: the mic port must stay closed.
    reopened = False
    deadline = time.time() + 2.0
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", mic_port))
            reopened = True
            probe.close()
            break
        except OSError:
            probe.close()
        time.sleep(0.2)
    check("no follow-up listening after exit", not reopened)

    # Wake word starts a fresh round right away.
    time.sleep(0.5)  # playback-tail mute must expire first
    trigger_wakeword(wake_port)
    mic2 = connect_with_retry(mic_port, timeout=8)
    if mic2 is not None:
        mic2.close()
    check("wake stage restored after exit", mic2 is not None)


def test_voice_volume(mic_port, spk_port, wake_port):
    """Asking to change the volume mid-conversation makes the LLM call the
    voice_control skill (action=volume): the new level is persisted to the
    agent config, the confirmation is spoken, and the conversation keeps
    going (follow-up window opens)."""
    print("\n[Voice Volume Control]")

    speech_pcm = synthesize_speech(TEST_QUESTION)
    wait_for_idle(mic_port)
    status, _ = https_post_json(FAKE_PORT, "/__asr_next", {"text": "set the volume to 30"})
    check("fake asr override armed", status == 200)

    trigger_wakeword(wake_port)
    mic = connect_with_retry(mic_port)
    if mic is None:
        check("mic connect for volume round", False, "mic port not opened")
        return
    stream_utterance(mic, speech_pcm)
    mic.close()

    spk = connect_with_retry(spk_port, timeout=90)
    if spk is None:
        check("volume confirmation tts played", False, "speaker port not opened")
        return
    check("volume confirmation tts played", read_until_eof(spk) > 0)

    status, data = http_get(AGENT_PORT, "/config")
    check(
        "volume persisted by skill",
        status == 200 and data.get("volume") == 30,
        "got {}".format(data.get("volume")),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    global AGENT_PORT, FAKE_PORT, MIC_PORT, SPK_PORT, WAKE_PORT
    AGENT_PORT = find_free_port(AGENT_PORT)
    FAKE_PORT = find_free_port(FAKE_PORT)
    MIC_PORT = find_free_port(MIC_PORT)
    # Chain the scans so occupied defaults (e.g. a running unix-dev
    # session) can't map several services onto the same free port.
    SPK_PORT = find_free_port(max(SPK_PORT, MIC_PORT + 1))
    WAKE_PORT = find_free_port(max(WAKE_PORT, SPK_PORT + 1))

    print("=== Edge Agent App-Flow Test ===")
    print("  Agent:  http://127.0.0.1:{}".format(AGENT_PORT))
    print("  Fake:   https://127.0.0.1:{}".format(FAKE_PORT))
    print("  Mic:    port {}".format(MIC_PORT))
    print("  Spk:    port {}".format(SPK_PORT))
    print("  Wake:   port {}".format(WAKE_PORT))

    # -- Prepare config --
    # Start from a clean slate: persisted sessions/memory from previous
    # runs must not leak into this run's conversations.
    cfg_dir = os.path.join(ROOT, "tmp", "unix-dev-data", "appflow")
    shutil.rmtree(cfg_dir, ignore_errors=True)
    os.makedirs(cfg_dir)
    agent_cfg = {
        "base_url": "https://127.0.0.1:{}".format(FAKE_PORT),
        "api_key": "sk-fake",
        "model": "fake-model",
        "asr_ws_host": "ws://127.0.0.1:{}/api-ws/v1/inference".format(FAKE_PORT),
        "asr_ws_api_key": "sk-asr-fake",
        "asr_ws_model": "paraformer-v2",
        "tts_ws_host": "ws://127.0.0.1:{}/api-ws/v1/inference".format(FAKE_PORT),
        "tts_ws_api_key": "sk-tts-fake",
        "tts_ws_model": "sambert-zhiying-v1",
        "tts_ws_sample_rate": 16000,
    }
    with open(os.path.join(cfg_dir, "agent.json"), "w") as f:
        json.dump(agent_cfg, f)

    # -- Start fake LLM server --
    print("\n[1/3] Starting fake LLM/ASR/TTS server...")
    fake_proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(ROOT, "tests", "fake_llm_server.py"),
            str(FAKE_PORT),
            TEST_QUESTION,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if not wait_http_ready(FAKE_PORT, timeout=120, use_tls=True):
        print("FATAL: fake server did not start")
        fake_proc.kill()
        sys.exit(1)
    print("  OK (pid={})".format(fake_proc.pid))

    # -- Start Edge Agent --
    print("[2/3] Starting Edge Agent (MicroPython)...")
    micropy = os.environ.get("MICROPY", "micropython")
    env = os.environ.copy()
    env["MICROPYPATH"] = ":".join(
        [
            # `.frozen` first so the frozen `asyncio`/stdlib is still reachable
            # (MICROPYPATH replaces the default sys.path instead of appending).
            ".frozen",
            os.path.join(ROOT, "src"),
            os.path.join(ROOT, "tests", "stubs"),
            os.path.join(ROOT, "tests"),
            os.path.join(ROOT, "tests", "mpy"),
        ]
    )
    agent_proc = subprocess.Popen(
        [
            micropy,
            "-X",
            "heapsize=16M",
            os.path.join(ROOT, "tests", "launch", "run_agent.py"),
            "--http-port",
            str(AGENT_PORT),
            "--mic-port",
            str(MIC_PORT),
            "--spk-port",
            str(SPK_PORT),
            "--wake-port",
            str(WAKE_PORT),
            "--data-dir",
            os.path.join("tmp", "unix-dev-data", "appflow"),
            "--no-pace",
            "--fast-timers",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=ROOT,
    )
    if not wait_http_ready(AGENT_PORT, timeout=15):
        print("FATAL: Edge Agent did not start")
        out = agent_proc.stdout.read().decode(errors="replace")
        print(out[-2000:])
        agent_proc.kill()
        fake_proc.kill()
        sys.exit(1)
    print("  OK (pid={})".format(agent_proc.pid))

    # -- Run tests --
    print("[3/3] Running channel tests...")
    fatal = False
    try:
        test_webui_channel(AGENT_PORT)
        test_voice_wakeword_channel(MIC_PORT, SPK_PORT, WAKE_PORT)
        test_voice_wakeword_idle_timeout(MIC_PORT, SPK_PORT, WAKE_PORT)
        test_voice_followup(MIC_PORT, SPK_PORT, WAKE_PORT)
        test_voice_exit_phrase(MIC_PORT, SPK_PORT, WAKE_PORT)
        test_voice_volume(MIC_PORT, SPK_PORT, WAKE_PORT)
        test_config_endpoint(AGENT_PORT)
    except Exception as e:
        fatal = True
        print("\nFATAL: test error: {}".format(e))
        import traceback

        traceback.print_exc()

    # -- Collect agent output (printed only on failure or E2E_VERBOSE=1) --
    agent_proc.send_signal(signal.SIGINT)
    try:
        agent_proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        agent_proc.kill()
    agent_out = agent_proc.stdout.read().decode(errors="replace")
    if _failed or fatal or os.environ.get("E2E_VERBOSE"):
        print("\n[Agent output]")
        for line in agent_out.strip().split("\n")[-200:]:
            print("  | " + line)

    # -- Cleanup --
    print("\n[cleanup]")
    try:
        agent_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        agent_proc.kill()
    fake_proc.terminate()
    try:
        fake_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        fake_proc.kill()
    print("  processes stopped")

    # -- Summary --
    print("\n" + "=" * 40)
    total = _passed + _failed
    if _failed == 0:
        print("APP-FLOW PASS: {}/{} checks passed".format(_passed, total))
    else:
        print("APP-FLOW FAIL: {}/{} passed, {} FAILED".format(_passed, total, _failed))
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
