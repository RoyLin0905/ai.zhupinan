"""S01 acceptance: login, sessions, per-owner isolation, roles, Origin check, reset.

Run from the repo root:  .venv\\Scripts\\python -m unittest discover -s demo/tests -v
No model calls: only endpoints that are fully deterministic (or rejected before any model call) are exercised.
"""
import hashlib
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

DOC = {"doc_type": "入國通報受理證明書", "workers": [{"name": "DEMO WORKER A", "entry_date": "2026-12-01"}], "save": True}


class S01Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ZP_DB_PATH"] = str(Path(self.tmp.name) / "test.db")
        os.environ["ZP_UPLOAD_DIR"] = str(Path(self.tmp.name) / "uploads")
        os.environ["DEMO_LOGIN_CODES"] = "employer_a:code-a-111,employer_b:code-b-222,officer:code-o-333"
        os.environ.pop("DEMO_DATA", None)
        for m in ("app", "db", "auth"):
            sys.modules.pop(m, None)
        self.app_mod = importlib.import_module("app")
        from fastapi.testclient import TestClient
        self.TestClient = TestClient

    def tearDown(self):
        for m in ("app", "db", "auth"):
            sys.modules.pop(m, None)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def client(self):
        return self.TestClient(self.app_mod.app)

    def login(self, account, code):
        c = self.client()
        r = c.post("/api/session", json={"account": account, "code": code})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def emp_a(self):
        return self.login("employer_a", "code-a-111")

    def emp_b(self):
        return self.login("employer_b", "code-b-222")

    def officer(self):
        return self.login("officer", "code-o-333")

    def db_rows(self, sql, *args):
        con = sqlite3.connect(os.environ["ZP_DB_PATH"])
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()


class LoginTest(S01Base):
    def test_wrong_code_401(self):
        r = self.client().post("/api/session", json={"account": "employer_a", "code": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_account_401_same_message(self):
        c = self.client()
        a = c.post("/api/session", json={"account": "nobody", "code": "x"})
        b = c.post("/api/session", json={"account": "employer_a", "code": "x"})
        self.assertEqual(a.status_code, 401)
        self.assertEqual(a.json(), b.json())

    def test_rate_limit_429(self):
        c = self.client()
        codes = [c.post("/api/session", json={"account": "employer_a", "code": f"bad{i}"}).status_code for i in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        # even the right code is refused while locked
        self.assertEqual(c.post("/api/session", json={"account": "employer_a", "code": "code-a-111"}).status_code, 429)

    def test_cookie_flags_and_hash_only(self):
        c = self.client()
        r = c.post("/api/session", json={"account": "employer_a", "code": "code-a-111"})
        sc = r.headers["set-cookie"].lower()
        self.assertIn("httponly", sc)
        self.assertIn("samesite=lax", sc)
        token = c.cookies.get("zp_session")
        self.assertTrue(token)
        stored = [row[0] for row in self.db_rows("SELECT id_hash FROM sessions")]
        self.assertNotIn(token, stored)
        self.assertIn(hashlib.sha256(token.encode()).hexdigest(), stored)

    def test_me(self):
        me = self.emp_a().get("/api/session").json()
        self.assertEqual(me["account"]["role"], "employer")
        self.assertEqual(len(me["cases"]), 1)
        self.assertEqual(self.client().get("/api/session").status_code, 401)

    def test_logout_invalidates(self):
        c = self.emp_a()
        token = c.cookies.get("zp_session")
        self.assertEqual(c.delete("/api/session").status_code, 200)
        c2 = self.client()
        c2.cookies.set("zp_session", token)
        self.assertEqual(c2.get("/api/todos").status_code, 401)

    def test_expired_session_401(self):
        c = self.emp_a()
        con = sqlite3.connect(os.environ["ZP_DB_PATH"])
        con.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
        con.commit()
        con.close()
        self.assertEqual(c.get("/api/todos").status_code, 401)


class AnonymousTest(S01Base):
    def test_protected_endpoints_401(self):
        c = self.client()
        checks = [
            c.get("/api/todos"),
            c.delete("/api/todos"),
            c.post("/api/obligations", json=DOC),
            c.get("/api/cases"),
            c.post("/api/todos/abc/assign", json={"assignee": None}),
            c.delete("/api/todos/abc/proof"),
            c.get("/api/todos/abc/proof/file"),
            c.post("/api/line/send", json={"days": 30}),
            c.get("/api/line/preview"),
            c.post("/api/ask", json={"question": "x"}),
            c.post("/api/check-penalty", json={"text": "x"}),
        ]
        self.assertEqual([r.status_code for r in checks], [401] * len(checks))

    def test_health_stays_public(self):
        self.assertEqual(self.client().get("/api/health").status_code, 200)


class IsolationTest(S01Base):
    def test_a_cannot_see_or_touch_b(self):
        a, b = self.emp_a(), self.emp_b()
        r = a.post("/api/obligations", json=DOC)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["added"], 5)
        a_items = a.get("/api/todos").json()["items"]
        self.assertEqual(len(a_items), 5)
        tid = a_items[0]["id"]
        a_case = a.get("/api/cases").json()["cases"][0]["id"]

        # B sees nothing of A
        self.assertEqual(b.get("/api/todos").json()["items"], [])
        self.assertEqual(b.get(f"/api/cases/{a_case}").status_code, 404)
        self.assertEqual(b.post(f"/api/todos/{tid}/assign", json={"assignee": "仲介"}).status_code, 404)
        self.assertEqual(b.delete(f"/api/todos/{tid}/proof").status_code, 404)
        self.assertEqual(b.get(f"/api/todos/{tid}/proof/file").status_code, 404)
        self.assertEqual(b.post("/api/obligations", json={**DOC, "case_id": a_case}).status_code, 404)
        # B clearing its own list leaves A untouched
        self.assertEqual(b.delete("/api/todos").status_code, 200)
        self.assertEqual(len(a.get("/api/todos").json()["items"]), 5)
        # B's LINE preview is built only from B's tasks
        self.assertEqual(b.get("/api/line/preview?days=3650").json()["count"], 0)
        # A can act on its own task
        self.assertEqual(a.post(f"/api/todos/{tid}/assign", json={"assignee": "仲介"}).status_code, 200)
        self.assertEqual(a.get("/api/todos").json()["items"][0]["assigned"], "仲介")

    def test_unknown_case_is_404_not_403(self):
        self.assertEqual(self.emp_a().get("/api/cases/999999").status_code, 404)

    def test_duplicate_document_no_duplicate_tasks(self):
        a = self.emp_a()
        a.post("/api/obligations", json=DOC)
        r = a.post("/api/obligations", json=DOC)
        self.assertEqual(r.json()["added"], 0)
        self.assertEqual(len(a.get("/api/todos").json()["items"]), 5)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM tasks")[0][0], 5)

    def test_preview_without_save_writes_nothing(self):
        a = self.emp_a()
        r = a.post("/api/obligations", json={**DOC, "save": False})
        self.assertEqual(len(r.json()["obligations"]), 5)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM tasks")[0][0], 0)


class RoleTest(S01Base):
    def test_employer_cannot_use_officer_api(self):
        self.assertEqual(self.emp_a().post("/api/check-penalty", json={"text": "x" * 100}).status_code, 403)

    def test_officer_cannot_read_employer_cases(self):
        o = self.officer()
        self.assertEqual(o.get("/api/todos").status_code, 403)
        self.assertEqual(o.get("/api/cases").status_code, 403)
        self.assertEqual(o.post("/api/obligations", json=DOC).status_code, 403)

    def test_officer_reaches_penalty_check(self):
        # too-short draft is rejected by validation (400), i.e. after auth and before any model call
        self.assertEqual(self.officer().post("/api/check-penalty", json={"text": "短"}).status_code, 400)


class OriginTest(S01Base):
    def test_cross_site_write_rejected(self):
        a = self.emp_a()
        r = a.post("/api/obligations", json=DOC, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = a.post("/api/obligations", json=DOC, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM tasks")[0][0], 0)

    def test_same_origin_write_allowed(self):
        a = self.emp_a()
        r = a.post("/api/obligations", json=DOC, headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 200)

    def test_cross_site_login_rejected(self):
        r = self.client().post("/api/session", json={"account": "employer_a", "code": "code-a-111"},
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


class ResetTest(S01Base):
    def test_reset_by_admin_clears_everything(self):
        a = self.emp_a()
        a.post("/api/obligations", json=DOC)
        con = sqlite3.connect(os.environ["ZP_DB_PATH"])
        acct = con.execute("SELECT id FROM accounts WHERE login_name='employer_a'").fetchone()[0]
        con.execute("INSERT INTO line_bindings(account_id, line_user_id, bound_at) VALUES (?, 'Udemo', '2026-11-16')", (acct,))
        con.commit()
        con.close()
        o = self.officer()   # the demo officer account is the demo admin
        self.assertEqual(o.post("/api/demo/reset").status_code, 200)
        for table in ("documents", "tasks", "proofs", "line_bindings", "notification_log"):
            self.assertEqual(self.db_rows(f"SELECT COUNT(*) FROM {table}")[0][0], 0, table)
        # seed cases are rebuilt, empty
        self.assertEqual(self.db_rows("SELECT COUNT(*) FROM cases")[0][0], 2)
        self.assertEqual(self.emp_a().get("/api/todos").json()["items"], [])

    def test_reset_requires_admin(self):
        self.assertEqual(self.emp_a().post("/api/demo/reset").status_code, 403)
        self.assertEqual(self.client().post("/api/demo/reset").status_code, 401)


class LineIsolationTest(S01Base):
    SECRET = "s01-test-secret"

    def setUp(self):
        os.environ["LINE_CHANNEL_SECRET"] = self.SECRET
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "s01-test-token"
        super().setUp()
        self.sent = []
        self.app_mod.line_api = lambda path, payload: (self.sent.append((path, payload)) or (200, "{}"))

    def webhook(self, payload: dict, secret: str | None = None):
        import base64, hmac, json
        body = json.dumps(payload).encode()
        sig = base64.b64encode(hmac.new((secret or self.SECRET).encode(), body, hashlib.sha256).digest()).decode()
        return self.client().post("/line/webhook", content=body, headers={"x-line-signature": sig,
                                                                          "content-type": "application/json"})

    def test_unbound_line_user_gets_no_tasks(self):
        self.emp_a().post("/api/obligations", json=DOC)
        ev = {"type": "message", "replyToken": "r1", "source": {"userId": "Ustranger"},
              "message": {"type": "text", "text": "待辦"}}
        self.assertEqual(self.webhook({"events": [ev]}).status_code, 200)
        text = self.sent[0][1]["messages"][0]["text"]
        self.assertIn("尚未綁定", text)
        self.assertNotIn("DEMO WORKER A", text)

    def test_bound_user_sees_only_own_tasks(self):
        self.emp_a().post("/api/obligations", json=DOC)
        b = self.emp_b()
        b.post("/api/obligations", json={**DOC, "workers": [{"name": "DEMO WORKER B", "entry_date": "2026-12-01"}]})
        con = sqlite3.connect(os.environ["ZP_DB_PATH"])
        bid = con.execute("SELECT id FROM accounts WHERE login_name='employer_b'").fetchone()[0]
        con.execute("INSERT INTO line_bindings(account_id, line_user_id, bound_at) VALUES (?, 'Ub', '2026-11-16')", (bid,))
        con.commit()
        con.close()
        ev = {"type": "message", "replyToken": "r2", "source": {"userId": "Ub"}, "message": {"type": "text", "text": "待辦"}}
        self.webhook({"events": [ev]})
        text = self.sent[0][1]["messages"][0]["text"]
        self.assertIn("DEMO WORKER B", text)
        self.assertNotIn("DEMO WORKER A", text)

    def test_bad_signature_rejected(self):
        ev = {"type": "message", "replyToken": "r3", "source": {"userId": "U"}, "message": {"type": "text", "text": "待辦"}}
        self.assertEqual(self.webhook({"events": [ev]}, secret="wrong").status_code, 403)
        self.assertEqual(self.sent, [])

    def test_send_without_binding_sends_nothing(self):
        a = self.emp_a()
        a.post("/api/obligations", json={**DOC, "workers": [{"name": "DEMO WORKER A", "entry_date": "2026-11-15"}]})
        r = a.post("/api/line/send", json={"days": 3650}).json()
        self.assertEqual(r["sent"], 0)
        self.assertEqual(r["mode"], "preview")
        self.assertEqual([s for s in self.sent if s[0] == "push"], [])


if __name__ == "__main__":
    unittest.main()
