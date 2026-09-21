"""Chat session persistence: JSONL-based append-only storage.

Each session is stored as a JSONL file under DATA_DIR/sessions/.
Messages are appended one-per-line for flash efficiency.
"""

import json
import os
import app.log as log
import app.config as config
from app.util import ensure_dir


def _is_safe_char(ch):
    """Check if character is safe for filenames (MicroPython compatible)."""
    o = ord(ch)
    return (
        48 <= o <= 57  # 0-9
        or 65 <= o <= 90  # A-Z
        or 97 <= o <= 122  # a-z
        or ch in "-_."
    )


def _safe_filename(session_key):
    """Convert session key to a safe filename."""
    out = []
    for ch in session_key:
        if _is_safe_char(ch):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


class ChatStore:
    """Append-only JSONL persistence for chat sessions."""

    def __init__(self, data_dir=None):
        self._dir = (data_dir or config.DATA_DIR).rstrip("/") + "/sessions"
        self._counts = {}  # session key -> message count (mirrors the file)
        ensure_dir(self._dir)

    def _path(self, session_key):
        return self._dir + "/" + _safe_filename(session_key) + ".jsonl"

    def append(self, session_key, messages):
        """Append one or more messages to the session file."""
        path = self._path(session_key)
        try:
            with open(path, "a") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")
            prev = self._counts.get(session_key)
            if prev is not None:
                self._counts[session_key] = prev + len(messages)
            return True
        except OSError as e:
            log.error("ChatStore", "append failed: {}".format(e))
            return False

    def load(self, session_key, max_messages=None):
        """Load messages from session file. Returns list of dicts.

        If max_messages is set, returns only the most recent N messages
        (preserving message boundaries for tool calls).
        """
        path = self._path(session_key)
        messages = []
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []

        self._counts[session_key] = len(messages)
        if max_messages and len(messages) > max_messages:
            messages = messages[-max_messages:]
            # Avoid starting with a tool message (orphan tool result)
            while messages and messages[0].get("role") == "tool":
                messages.pop(0)
        return messages

    def clear(self, session_key):
        """Remove session file."""
        path = self._path(session_key)
        self._counts.pop(session_key, None)
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def message_count(self, session_key):
        """Count messages in a session file without loading all into RAM.

        The count is cached once a session has been loaded or appended;
        only the first call after boot actually reads the file.
        """
        cached = self._counts.get(session_key)
        if cached is not None:
            return cached
        path = self._path(session_key)
        count = 0
        try:
            with open(path, "r") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except OSError:
            pass
        self._counts[session_key] = count
        return count

    def compact(self, session_key, keep_messages):
        """Rewrite session file keeping only the given messages.

        Used after consolidation to trim the persisted history.
        """
        path = self._path(session_key)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                for msg in keep_messages:
                    f.write(json.dumps(msg) + "\n")
            os.remove(path)
            os.rename(tmp_path, path)
            self._counts[session_key] = len(keep_messages)
            return True
        except OSError as e:
            log.error("ChatStore", "compact failed: {}".format(e))
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False
