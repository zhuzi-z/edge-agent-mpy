#!/usr/bin/env python3
"""Upload skill(s) to the ESP32 agent.

Scans skills/<name>/ for skill.json + code.py and POSTs them to the device's
POST /skills route.

Usage:
  python3 skills/upload.py --base-url http://127.0.0.1                   # all skills
  python3 skills/upload.py --base-url http://127.0.0.1:8080 mijia        # one skill
"""

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def discover_skills():
    """Return list of skill names (directories containing skill.json + code.py)."""
    names = []
    for entry in sorted(os.listdir(HERE)):
        path = os.path.join(HERE, entry)
        if (
            os.path.isdir(path)
            and os.path.isfile(os.path.join(path, "skill.json"))
            and os.path.isfile(os.path.join(path, "code.py"))
        ):
            names.append(entry)
    return names


def build_payload(skill_dir):
    with open(os.path.join(skill_dir, "skill.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(skill_dir, "code.py"), "r", encoding="utf-8") as f:
        code = f.read()
    return {
        "name": meta["name"],
        "description": meta["description"],
        "parameters": meta["parameters"],
        "code": code,
    }


def upload_one(name, base_url):
    skill_dir = os.path.join(HERE, name)
    if not os.path.isdir(skill_dir):
        sys.exit("error: skill '{}' not found in {}".format(name, HERE))
    payload = build_payload(skill_dir)
    body = json.dumps(payload).encode("utf-8")
    url = base_url + "/skills"
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("[{}] HTTP {} {}".format(name, resp.status, url))
            print("  " + resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        sys.exit(
            "[{}] upload failed: HTTP {} {}\n  {}".format(
                name, e.code, e.reason, e.read().decode("utf-8", "replace")
            )
        )
    except OSError as e:
        sys.exit("[{}] upload failed: cannot reach {}: {}".format(name, base_url, e))


def main():
    ap = argparse.ArgumentParser(description="Upload skill(s) to ESP32 agent.")
    ap.add_argument("skills", nargs="*", help="Skill name(s) to upload (default: all)")
    ap.add_argument(
        "--base-url",
        required=True,
        help="Agent base URL, e.g. http://127.0.0.1:8080",
    )
    args = ap.parse_args()

    names = args.skills or discover_skills()
    if not names:
        sys.exit("no skills found in {}".format(HERE))
    base = args.base_url.rstrip("/")
    for name in names:
        upload_one(name, base)


if __name__ == "__main__":
    main()
