"""Timestamp rendering tests for app.log."""

import compat  # noqa: F401

import time
import unittest

import app.config as config
import app.log as log


class TestTimestamp(unittest.TestCase):
    def setUp(self):
        log._wall_base_ms = None
        log._wall_ticks = None

    def test_monotonic_and_bounded(self):
        # The stamp is derived from one ticks_ms() reading, so successive
        # stamps never go backwards across a second boundary (the old
        # localtime()+ticks_ms()%1000 mix produced 16.967 then 16.352).
        stamps = []
        for _ in range(20):
            stamps.append(log._ts())
            time.sleep_ms(3)
        self.assertEqual(stamps, sorted(stamps))
        total_ms = log._wall_ms()
        sec, ms = total_ms // 1000, total_ms % 1000
        self.assertTrue(0 <= ms < 1000)
        self.assertEqual(log._ts()[:4], str(time.localtime(sec)[0]))

    def test_pre_sync_and_resync(self):
        # A pre-NTP wall clock lands below the sync floor and degrades to
        # seconds since boot; the anchor is re-read once it goes stale.
        log._wall_base_ms = 100000
        log._wall_ticks = time.ticks_ms()
        self.assertTrue(log._ts().startswith("+100."))
        log._wall_ticks = time.ticks_add(log._wall_ticks, -(log._WALL_RESYNC_MS + 1))
        self.assertFalse(log._ts().startswith("+100."))
        saved = config.TIMING_LOG
        config.TIMING_LOG = False
        try:
            log.timing("Test", "a=1")  # silenced, must not raise
        finally:
            config.TIMING_LOG = saved


if __name__ == "__main__":
    unittest.main(globals())
