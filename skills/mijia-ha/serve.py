"""Run only this skill's HTTP endpoints on the MicroPython unix port.

One command, no fake LLM, no audio bridge -- the point is the login page:

    micropython skills/mijia-ha/serve.py       # open http://127.0.0.1:8899/mijia-ha/
    micropython skills/mijia-ha/serve.py 9001  # another port

It is the same code path the device runs: the real SkillRegistry injects the
helpers (http_get / register_endpoint / data_dir / server_port ...) and the
real app.api.server serves them, so code.py is not modified for the host.
Nothing else is loaded, so every other path 404s.

On hardware this file is not used at all:

    python3 skills/upload.py --base-url http://192.168.2.216 mijia-ha
    # open http://192.168.2.216/mijia-ha/
"""

import sys
import os
import json
import asyncio

ROOT = os.getcwd()
sys.path.insert(0, ROOT + "/src")
sys.path.insert(0, ROOT + "/tests/stubs")
sys.path.insert(0, ROOT + "/tests")

import compat  # noqa: F401,E402

import app.config as config  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
BASE = ROOT + "/tmp/mijia-ha-data"
config.HTTP_PORT = PORT
config.DATA_DIR = BASE + "/"
config.SKILLS_DATA_DIR = BASE + "/skills/"
config.UPLOADED_SKILLS_PATH = BASE + "/uploaded"
for d in ("", "/skills", "/uploaded"):
    try:
        os.mkdir(BASE + d)
    except OSError:
        pass

from app.skills import SkillRegistry  # noqa: E402
from app.api.server import HTTPServer  # noqa: E402

# MicroPython's os has no os.path, and the repo root is the cwd (see `make unix-dev`).
HERE = "skills/mijia-ha"
with open(HERE + "/skill.json", "r") as f:
    META = json.load(f)
with open(HERE + "/code.py", "r") as f:
    CODE = f.read()

REG = SkillRegistry(builtin_dir=BASE + "/no-builtin", upload_dir=config.UPLOADED_SKILLS_PATH)
REG.add(META["name"], META["description"], META["parameters"], CODE)
SRV = HTTPServer(None, PORT, skills=REG)


async def _main():
    await SRV.start()
    print("[serve] data dir {}".format(config.SKILLS_DATA_DIR + META["name"]))
    # The endpoints only exist once the skill has been asked to log in, which on
    # a device is the agent calling the tool -- do that same call here.
    print("[serve] run(action=login) -> {}".format(REG.exec(META["name"], {"action": "login"})))
    print("[serve] open http://127.0.0.1:{}/mijia-ha/".format(PORT))
    while True:
        await asyncio.sleep(3600)


try:
    asyncio.run(_main())
except KeyboardInterrupt:
    print("[serve] stopped")
