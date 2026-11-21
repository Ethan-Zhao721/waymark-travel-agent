import datetime
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class DestinationJobTests(unittest.TestCase):
    def test_duplicate_request_and_restart_charge_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "CACHE", Path(tmp)), \
             patch.object(server, "pending", set()), patch.object(server.POOL, "submit") as submit, \
             patch.dict(os.environ, OPENAI_API_KEY="test"):
            server.start_workers()
            key = "1850147"
            self.assertEqual(server.response_for(key)[0], 202)
            self.assertEqual(server.response_for(key)[0], 202)
            self.assertEqual(submit.call_count, 1)
            server.pending.clear()
            server.start_workers()
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(submit.call_args.args[1]["id"], key)
            with sqlite3.connect(server.CACHE / "budget.db") as db:
                self.assertEqual(db.execute("SELECT count FROM usage").fetchall(), [(1,)])

    def test_progress_and_completion_without_polling(self):
        key = "1850147"
        city = server.CITIES[key]
        guide = dict(cityID=key, rankingVersion=4, checked=str(datetime.date.today()), places=[], coverage="")
        discovering, verifying = threading.Event(), threading.Event()
        release_search, release_verify = threading.Event(), threading.Event()
        def discover(_):
            discovering.set()
            if not release_search.wait(5):
                raise TimeoutError()
            return {}
        def verify(_, seeds):
            verifying.set()
            if not release_verify.wait(5):
                raise TimeoutError()
            return guide
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "CACHE", Path(tmp)), \
             patch.dict(os.environ, OPENAI_API_KEY="test"), patch.object(server, "pending", set()), \
             patch.object(server, "discover_candidates", side_effect=discover), \
             patch.object(server, "verify_candidates", side_effect=verify), \
             ThreadPoolExecutor(max_workers=1) as worker, patch.object(server, "POOL", worker):
            server.start_workers()
            existing = dict(guide, checked="2020-01-01")
            cache = Path(tmp) / (key + ".json")
            server.write(cache, existing)
            self.assertEqual(server.response_for(key), (202, {"state":"queued"}))
            self.assertTrue(discovering.wait(3))
            self.assertEqual(server.response_for(key), (202, {"state":"discovering"}))
            release_search.set()
            self.assertTrue(verifying.wait(3))
            self.assertEqual(server.response_for(key), (202, {"state":"verifying"}))
            self.assertEqual(json.loads(cache.read_text()), existing)
            release_verify.set()
            worker.shutdown(wait=True)
            self.assertEqual(server.response_for(key), (200, guide))
            self.assertEqual(server.get_cached(city), guide)

    def test_transient_source_failure_retries(self):
        with patch.object(server, "urlopen", side_effect=[TimeoutError(), io.BytesIO(b'{"ok":true}')]), \
             patch.object(server.time, "sleep") as sleep:
            self.assertEqual(server.fetch_json("https://example.org", 5), {"ok": True})
            sleep.assert_called_once()
