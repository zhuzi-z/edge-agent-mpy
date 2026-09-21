#!/usr/bin/env python3
"""Host audio bridge with wake word detection via openwakeword.

Runs as a background helper alongside the MicroPython agent.

Flow:
  1. Continuously captures mic audio, openwakeword detects wake word in real-time
  2. On detection: triggers agent via esp_sr trigger server (TCP)
  3. Connects to agent mic port, streams audio (with pre-roll buffer)
  4. Speaker thread plays TTS asynchronously whenever agent produces it
  5. After TTS playback finishes, streams mic again so the agent's
     follow-up listening window gets audio (no wake word needed)
  6. Returns to wake word detection when the follow-up window expires

Wake detection is gated off while TTS is audible: the host has no AEC,
so the mic would pick up the speaker and self-trigger (half-duplex,
mirrors xiaozhi's non-AFE behavior).

Usage:
    python3 tests/launch/audio_bridge.py [--model alexa] [--threshold 0.5]

Requires: pip install sounddevice numpy openwakeword
"""

import argparse
import socket
import sys
import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

MIC_PORT = 19301
SPK_PORT = 19302
WAKE_PORT = 19303
SAMPLE_RATE = 16000
CHUNK_FRAMES = 1280  # 80ms (openwakeword native chunk size)
PRE_ROLL_CHUNKS = 7  # ~560ms pre-roll

stop_event = threading.Event()
shutdown_event = threading.Event()
followup_event = threading.Event()  # set when TTS playback finishes

# Half-duplex echo suppression: while TTS audio is audible the mic would
# pick up the speaker, so wake detection is gated off until playback drains.
PLAYBACK_TAIL_SEC = 0.5
playback_state = {"until": 0.0}


def playback_active():
    return time.time() < playback_state["until"]


def load_wakeword_model(model_name, threshold):
    """Load openwakeword model. Returns (model, model_key)."""
    import openwakeword
    from openwakeword.model import Model

    paths = openwakeword.get_pretrained_model_paths()
    matched = [p for p in paths if model_name in p]
    if not matched:
        available = [p.rsplit("/", 1)[-1].replace(".onnx", "") for p in paths]
        print("[bridge] Model '{}' not found. Available: {}".format(model_name, available))
        sys.exit(1)

    m = Model(wakeword_model_paths=matched)
    key = list(m.models.keys())[0]
    print("[bridge] Wake word model: {} (threshold={})".format(key, threshold))
    return m, key


def trigger_agent(port, word="wake"):
    """Send wake word trigger to the agent's esp_sr trigger server."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(word.encode("utf-8"))
        s.close()
        return True
    except OSError as e:
        print("[bridge] Trigger failed: {}".format(e))
        return False


def connect_mic(port, timeout=60):
    """Connect to agent mic port (retries until agent opens it)."""
    deadline = time.time() + timeout
    while time.time() < deadline and not stop_event.is_set():
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except (OSError, ConnectionRefusedError):
            time.sleep(0.1)
    return None


def stream_mic(sock, pre_roll_buf):
    """Stream mic audio to agent until connection closes."""
    for chunk in pre_roll_buf:
        try:
            sock.sendall(chunk)
        except OSError:
            return

    conn_alive = [True]

    def callback(indata, frames, time_info, status):
        if stop_event.is_set() or not conn_alive[0]:
            raise sd.CallbackStop()
        try:
            sock.sendall(bytes(indata))
        except (OSError, socket.timeout):
            conn_alive[0] = False
            raise sd.CallbackStop()

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_FRAMES,
            callback=callback,
        ):
            while not stop_event.is_set() and conn_alive[0]:
                time.sleep(0.05)
    except Exception:
        pass


def speaker_thread():
    """Background thread: continuously listens for TTS and plays it.

    Plays chunks as they arrive (streaming) instead of buffering the whole
    utterance: playback starts immediately and a slow synthesizer can't
    stall it into a recv timeout.
    """
    while not shutdown_event.is_set():
        try:
            sock = socket.create_connection(("127.0.0.1", SPK_PORT), timeout=2)
        except (OSError, ConnectionRefusedError, socket.timeout):
            time.sleep(0.3)
            continue

        sock.settimeout(600)
        total = 0
        try:
            with sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as out:
                while True:
                    try:
                        data = sock.recv(8192)
                    except socket.timeout:
                        break
                    if not data:
                        break
                    out.write(data)
                    total += len(data)
                    playback_state["until"] = time.time() + PLAYBACK_TAIL_SEC
        except Exception as e:
            print("[bridge] Speaker error: {}".format(e))
        finally:
            try:
                sock.close()
            except OSError:
                pass

        if total:
            print("[bridge] TTS played ({} bytes)".format(total))
            # Agent enters its follow-up listening window after playback;
            # stream mic to it once the wake gate reopens.
            followup_event.set()


def listen_for_wake_word(ww_model, ww_key, threshold):
    """Wait for wake word or the end of TTS playback.

    Returns (kind, pre_roll) where kind is "wake", "followup", or None on stop.
    """
    pre_roll = deque(maxlen=PRE_ROLL_CHUNKS)
    result = [None, None]

    def callback(indata, frames, time_info, status):
        if stop_event.is_set():
            raise sd.CallbackStop()
        if followup_event.is_set():
            result[0] = "followup"
            stop_event.set()
            raise sd.CallbackStop()
        if playback_active():
            # Half-duplex gate: don't wake on TTS echo.
            pre_roll.clear()
            return
        pcm = bytes(indata)
        pre_roll.append(pcm)
        samples = np.frombuffer(pcm, dtype=np.int16)
        scores = ww_model.predict(samples)
        if scores.get(ww_key, 0) > threshold:
            ww_model.reset()
            result[0] = "wake"
            result[1] = list(pre_roll)
            stop_event.set()

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=CHUNK_FRAMES,
            callback=callback,
        ):
            while not stop_event.is_set():
                time.sleep(0.02)
    except Exception as e:
        if not stop_event.is_set():
            print("[bridge] Mic error: {}".format(e))
    return result[0], result[1]


def main():
    parser = argparse.ArgumentParser(description="Host audio bridge (openwakeword)")
    parser.add_argument("--mic-port", type=int, default=19301)
    parser.add_argument("--spk-port", type=int, default=19302)
    parser.add_argument("--wake-port", type=int, default=19303)
    parser.add_argument("--model", default="alexa", help="Wake word model name")
    parser.add_argument("--threshold", type=float, default=0.5, help="Detection threshold")
    args = parser.parse_args()

    global MIC_PORT, SPK_PORT, WAKE_PORT
    MIC_PORT = args.mic_port
    SPK_PORT = args.spk_port
    WAKE_PORT = args.wake_port

    print("[bridge] mic={}, spk={}, wake={}".format(MIC_PORT, SPK_PORT, WAKE_PORT))
    ww_model, ww_key = load_wakeword_model(args.model, args.threshold)

    # Start speaker playback thread
    spk = threading.Thread(target=speaker_thread, daemon=True)
    spk.start()

    print("[bridge] Listening for wake word...")

    while True:
        stop_event.clear()
        ww_model.reset()

        kind, pre_roll = listen_for_wake_word(ww_model, ww_key, args.threshold)
        if kind is None:
            break
        stop_event.clear()

        if kind == "wake":
            print("[bridge] Wake word detected!")
            if not trigger_agent(WAKE_PORT, args.model):
                continue
            time.sleep(0.5)
            sock = connect_mic(MIC_PORT)
            if sock is None:
                print("[bridge] Could not connect to mic port")
                continue
        else:
            # Follow-up: agent is already listening after TTS playback.
            print("[bridge] Playback done, streaming follow-up audio")
            followup_event.clear()
            time.sleep(0.3)
            sock = connect_mic(MIC_PORT, timeout=4)
            if sock is None:
                continue  # agent not in follow-up listening; back to wake word
            pre_roll = []

        print("[bridge] Streaming mic → agent...")
        sock.settimeout(5)
        stop_event.clear()
        stream_mic(sock, pre_roll)
        try:
            sock.close()
        except OSError:
            pass
        print("[bridge] Mic stream ended")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
        shutdown_event.set()
