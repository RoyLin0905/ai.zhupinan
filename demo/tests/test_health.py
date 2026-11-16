"""S00 checks: /api/health reports service, rule versions, calendar years and demo mode, and never leaks keys.

Run from the repo root:  .venv\Scripts\python -m unittest discover -s demo/tests -v
"""
import importlib
import json
import os
import sys
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

FAKE_SECRET = "s00-fake-channel-secret-0000"
FAKE_TOKEN = "s00-fake-access-token-1111"


def load_app():
    sys.modules.pop("app", None)
    return importlib.import_module("app")


class HealthTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        sys.modules.pop("app", None)

    def client(self):
        from fastapi.testclient import TestClient
        return TestClient(load_app().app)

    def test_health_fields(self):
        os.environ.pop("DEMO_DATA", None)
        body = self.client().get("/api/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "zhupinan-demo")
        self.assertTrue(body["demo_mode"])
        self.assertEqual(body["calendar_years"], ["2026", "2027"])
        self.assertGreater(body["articles"], 300)
        laws = body["rules"]["law_versions"]
        self.assertIn("就業服務法", laws)
        self.assertIn("雇主聘僱外國人許可及管理辦法", laws)
        self.assertTrue(all(v.startswith("民國") for v in laws.values()), laws)

    def test_health_never_returns_keys(self):
        os.environ["LINE_CHANNEL_SECRET"] = FAKE_SECRET
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = FAKE_TOKEN
        raw = self.client().get("/api/health").text
        self.assertNotIn(FAKE_SECRET, raw)
        self.assertNotIn(FAKE_TOKEN, raw)
        self.assertTrue(json.loads(raw)["line_configured"])

    def test_refuses_non_demo_mode(self):
        os.environ["DEMO_DATA"] = "false"
        with self.assertRaises(RuntimeError):
            load_app()


if __name__ == "__main__":
    unittest.main()
