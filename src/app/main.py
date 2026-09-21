"""Main entry point."""

import asyncio
import time
import machine
from app.controllers import WiFiManager
from app.api.server import HTTPServer, route_info, route_index
from app.storage.store import Store
from app.storage.chat import ChatStore
from app.storage.memory import MemoryManager
from app.skills import SkillRegistry
from app.control import DeviceControl
from app.agent import Agent
from app.bus import Bus
from app.settings import register_routes as register_settings
from app.channels.web import WebChannel
from app.channels.voice import VoiceChannel
from app.channels.weixin import WeixinChannel
from app.provision import run_provision
from app.util import uptime_seconds
import app.config as config
import app.log as log
import app.ota as ota


async def _main():
    wifi = WiFiManager()
    if not wifi.has_credentials():
        log.info("Boot", "No WiFi credentials found, starting provisioning...")
        run_provision()
        wifi.reload_credentials()

    store = Store()
    control = DeviceControl(store)
    chat_store = ChatStore()
    memory = MemoryManager()
    skills = SkillRegistry()
    skills.load()
    skills.attach_wifi(wifi)
    skills.attach_control(control)
    agent = Agent(control, skills, memory=memory)

    bus = Bus(agent, chat_store=chat_store, memory=memory, session_cfg_fn=control.config)

    server = HTTPServer(wifi, config.HTTP_PORT, skills=skills, control=control)

    server.register_route("GET", "/", route_index)
    server.register_route("GET", "/index.html", route_index)
    server.register_route("GET", "/info", route_info)
    server.register_route("GET", "/ota", ota.route_status)
    server.register_route("POST", "/ota/rollback", ota.route_rollback)
    # An update bundle is the raw request body - binary, and far bigger than
    # anything a JSON route may hold - so it is read straight off the socket.
    server.register_stream_route("POST", "/ota", ota.route_upload)

    voice_ch = VoiceChannel(bus, voice_cfg_fn=control.config)
    weixin_ch = WeixinChannel(bus)

    ch_cfg = control.channel_config()
    all_channels = {
        "web": WebChannel(bus),
        "voice": voice_ch,
        "weixin": weixin_ch,
    }
    channels = []
    for name, ch in all_channels.items():
        # Routes are registered for every channel (even disabled ones) so
        # opt-in channels like weixin stay configurable through the WebUI.
        for method, path, handler in ch.routes():
            server.register_route(method, path, handler)
        if name in config.CHANNELS_FORCED_ON or ch_cfg.get(name, ch.enabled_default):
            channels.append(ch)
        else:
            log.info("Boot", "Channel '{}' disabled by config".format(name))

    register_settings(
        server,
        control,
        skills,
        voice_channel=voice_ch,
        weixin_channel=weixin_ch,
    )
    for ch in channels:
        await ch.start()

    log.info("Boot", "=== ESP32-S3 Start ===")
    # This app came up, so it is the one to keep: an update that gets this far
    # is never rolled back by src/main.py.  Before any network I/O on purpose -
    # see app/ota.py: confirm().
    ota.confirm()
    await wifi.connect_async()

    ntp_synced = False
    connect_failures = 0
    maintenance_at = time.ticks_ms()
    watchdog = None
    if config.WATCHDOG_ENABLED:
        try:
            watchdog = machine.WDT(timeout=config.WATCHDOG_TIMEOUT_MS)
            log.info("Boot", "Hardware watchdog enabled ({}ms)".format(config.WATCHDOG_TIMEOUT_MS))
        except (ValueError, OSError, AttributeError) as e:
            log.warn("Boot", "Hardware watchdog unavailable: {}".format(e))
    while True:
        if watchdog is not None:
            watchdog.feed()
        if wifi.is_connected():
            connect_failures = 0
            if not ntp_synced:
                ntp_synced = wifi.sync_ntp()
            if not server.is_running():
                await server.start()
        else:
            if server.is_running():
                await server.stop()
            if await wifi.connect_async():
                connect_failures = 0
            else:
                connect_failures += 1
                log.warn(
                    "WiFi",
                    "Connect failed ({}/{})".format(
                        connect_failures, config.WIFI_MAX_CONNECT_FAILURES
                    ),
                )
                if connect_failures >= config.WIFI_MAX_CONNECT_FAILURES:
                    log.warn("WiFi", "Too many failures, starting provisioning...")
                    run_provision()
                    wifi.reload_credentials()
                    connect_failures = 0
                    ntp_synced = False
        # Periodic maintenance, kept off the 10ms path: bring the audio thread
        # back if it crashed, reclaim sessions whose user never returns, and
        # keep the wraparound-safe uptime counter fresh.
        now = time.ticks_ms()
        if time.ticks_diff(now, maintenance_at) >= config.MAINTENANCE_INTERVAL_MS:
            maintenance_at = now
            voice_ch.supervise()
            await bus.sweep_idle_async()
            uptime_seconds()
        await asyncio.sleep(config.MAIN_LOOP_SLEEP_MS / 1000.0)


def main():
    try:
        asyncio.run(_main())
    except Exception as e:  # noqa: BLE001 - an unbootable app must recover
        log.error("Main", "fatal error: {}".format(e))
        time.sleep(1)
        machine.reset()


if __name__ == "__main__":
    main()
