"""S02 acceptance: upload checks, no original left behind, masking before the model, synthetic-only images,
explicit confirmation of flagged dates, and no case writes before confirmation.

The model is never called for real: app.llm.call is replaced by a recorder.
Run from the repo root:  .venv\\Scripts\\python -m unittest discover -s demo/tests -v
"""
import importlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "demo" / "server"
sys.path.insert(0, str(SERVER))

SAMPLE_SCAN = ROOT / "demo" / "web" / "samples" / "聘僱許可函_掃描.png"
SYNTHETIC_TEXT = """【合成測試文件・非真實公文】
新竹市政府 受理雇主聘僱外國人入國通報證明書
外國人姓名：SITI DEMO RAHAYU
護照號碼：C1234567
入國日期：一一五年十二月一日
"""
EXTRACT_JSON = {"doc_type": "入國通報受理證明書",
                "workers": [{"name": "PERSON_01", "entry_date": "2026-12-01",
                             "evidence": {"entry_date": "一一五年十二月一日"}}]}


def png_bytes(color=(10, 20, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages=1, text="合成測試 PDF 入國日期 2026-12-01 " * 5) -> bytes:
    import fitz
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), text if text else "")
    out = doc.tobytes()
    doc.close()
    return out


class S02Base(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        os.environ["ZP_DB_PATH"] = str(base / "t.db")
        os.environ["ZP_UPLOAD_DIR"] = str(base / "uploads")
        os.environ["ZP_TMP_DIR"] = str(base / "ingest")
        os.environ["DEMO_LOGIN_CODES"] = "employer_a:ca,employer_b:cb,officer:co"
        for m in ("app", "db", "auth", "documents"):
            sys.modules.pop(m, None)
        self.app = importlib.import_module("app")
        self.calls = []
        self.reply = json.dumps(EXTRACT_JSON, ensure_ascii=False)

        def fake_call(system, user, model="haiku", image=None, timeout=240, cache=True):
            self.calls.append({"system": system, "user": user, "model": model, "image": image,
                               "image_existed": bool(image and Path(image).exists()), "cache": cache})
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        self.app.llm.call = fake_call
        from fastapi.testclient import TestClient
        self.TC = TestClient

    def tearDown(self):
        for m in ("app", "db", "auth", "documents"):
            sys.modules.pop(m, None)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def login(self, acct="employer_a", code="ca"):
        c = self.TC(self.app.app)
        self.assertEqual(c.post("/api/session", json={"account": acct, "code": code}).status_code, 200)
        return c

    def upload(self, c, name, raw, path="/api/documents/extract"):
        return c.post(path, files={"file": (name, raw)})

    def leftovers(self):
        found = []
        for d in (os.environ["ZP_TMP_DIR"], os.environ["ZP_UPLOAD_DIR"]):
            if os.path.isdir(d):
                found += [str(p) for p in Path(d).rglob("*") if p.is_file()]
        return found

    def count(self, table):
        con = sqlite3.connect(os.environ["ZP_DB_PATH"])
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            con.close()


class UploadChecksTest(S02Base):
    def test_wrong_extension_rejected_no_leftovers(self):
        c = self.login()
        for name, raw in (("doc.txt", png_bytes()), ("doc.png", SYNTHETIC_TEXT.encode()),
                          ("doc.exe", b"MZ\x90\x00"), ("doc.pdf", png_bytes())):
            r = self.upload(c, name, raw)
            self.assertEqual(r.status_code, 415, (name, r.text))
            self.assertIn("detail", r.json())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.leftovers(), [])

    def test_too_large(self):
        r = self.upload(self.login(), "big.txt", b"a" * (15 * 1024 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.leftovers(), [])

    def test_broken_pdf(self):
        r = self.upload(self.login(), "bad.pdf", b"%PDF-1.4\n garbage not a pdf")
        self.assertEqual(r.status_code, 422)
        self.assertIn("PDF", r.json()["detail"])
        self.assertEqual(self.leftovers(), [])

    def test_pdf_page_limit(self):
        r = self.upload(self.login(), "long.pdf", pdf_bytes(pages=11))
        self.assertEqual(r.status_code, 413)
        self.assertIn("10", r.json()["detail"])

    def test_binary_text_rejected(self):
        r = self.upload(self.login(), "x.txt", b"abc\x00\x01\x02")
        self.assertEqual(r.status_code, 415)

    def test_anonymous_and_officer(self):
        self.assertEqual(self.upload(self.TC(self.app.app), "a.txt", b"x").status_code, 401)
        self.assertEqual(self.upload(self.login("officer", "co"), "a.txt", b"x").status_code, 403)


class PrivacyTest(S02Base):
    def test_text_masked_before_model_and_preview_returned(self):
        r = self.upload(self.login(), "合成.txt", SYNTHETIC_TEXT.encode("utf-8"))
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.calls[0]["user"]
        self.assertNotIn("SITI DEMO RAHAYU", sent)
        self.assertNotIn("C1234567", sent)
        body = r.json()
        self.assertIn(body["masked_preview"], sent)  # the preview is exactly what went out
        self.assertEqual(body["workers"][0]["name"], "SITI DEMO RAHAYU")  # restored locally
        self.assertFalse(self.calls[0]["cache"])  # user uploads are not written to the model cache
        self.assertEqual(self.leftovers(), [])

    def test_real_image_never_leaves(self):
        r = self.upload(self.login(), "photo.png", png_bytes((200, 1, 1)))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json().get("code"), "real_image_blocked")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.leftovers(), [])

    def test_scanned_pdf_not_on_allowlist_blocked(self):
        r = self.upload(self.login(), "scan.pdf", pdf_bytes(pages=1, text=""))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json().get("code"), "real_image_blocked")
        self.assertEqual(self.calls, [])

    def test_synthetic_sample_image_allowed_and_deleted(self):
        r = self.upload(self.login(), SAMPLE_SCAN.name, SAMPLE_SCAN.read_bytes())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["source"]["synthetic_sample"])
        self.assertTrue(self.calls[0]["image_existed"])          # the model could read it during the call
        self.assertFalse(Path(self.calls[0]["image"]).exists())  # and it is gone afterwards
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(all(f["warning"] for f in r.json()["fields"]))  # scans always need checking

    def test_model_failure_leaves_nothing(self):
        import llm
        self.reply = llm.LLMError("模擬失敗")
        r = self.upload(self.login(), SAMPLE_SCAN.name, SAMPLE_SCAN.read_bytes())
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.leftovers(), [])

    def test_extract_writes_nothing_to_case(self):
        self.upload(self.login(), "合成.txt", SYNTHETIC_TEXT.encode("utf-8"))
        self.assertEqual(self.count("documents"), 0)
        self.assertEqual(self.count("tasks"), 0)


class ConfirmTest(S02Base):
    def extract(self, c):
        r = self.upload(c, "合成.txt", SYNTHETIC_TEXT.encode("utf-8"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def confirm(self, c, ex, entry="2026-12-01", checked=()):
        return c.post("/api/documents/confirm", json={
            "extract_id": ex["extract_id"], "document_kind": ex["document_kind"],
            "workers": [{"name": ex["workers"][0]["name"], "entry_date": entry}], "checked": list(checked)})

    def test_fields_contract(self):
        ex = self.extract(self.login())
        f = ex["fields"][0]
        self.assertEqual({"worker", "field", "value", "source_excerpt", "warning"} - set(f), set())
        self.assertEqual(f["source_excerpt"], "一一五年十二月一日")
        self.assertIn("核對", f["warning"])

    def test_unchecked_flagged_date_cannot_create_tasks(self):
        c = self.login()
        ex = self.extract(c)
        r = self.confirm(c, ex)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["unchecked"], ["0:entry_date"])
        self.assertEqual(self.count("tasks"), 0)

    def test_checked_date_creates_tasks_once(self):
        c = self.login()
        ex = self.extract(c)
        r = self.confirm(c, ex, checked=["0:entry_date"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["task_ids"]), 5)
        self.assertEqual(self.count("documents"), 1)
        # the extraction is consumed: replaying it is refused
        self.assertEqual(self.confirm(c, ex, checked=["0:entry_date"]).status_code, 404)

    def test_edited_date_counts_as_confirmed_and_recomputes(self):
        c = self.login()
        ex = self.extract(c)
        r = self.confirm(c, ex, entry="2026-12-15")
        self.assertEqual(r.status_code, 200, r.text)
        dues = {o["key"]: o["due"] for o in r.json()["obligations"]}
        before = {o["key"]: o["due"] for o in c.post("/api/obligations", json={
            "doc_type": "入國通報受理證明書", "workers": [{"name": "X", "entry_date": "2026-12-01"}], "save": False}).json()["obligations"]}
        self.assertNotEqual(dues["apply_permit"], before["apply_permit"])

    def test_other_employer_cannot_use_extraction(self):
        ex = self.extract(self.login())
        r = self.confirm(self.login("employer_b", "cb"), ex, checked=["0:entry_date"])
        self.assertEqual(r.status_code, 404)

    def test_cancel_discards_extraction(self):
        c = self.login()
        ex = self.extract(c)
        self.assertEqual(c.delete(f"/api/documents/extract/{ex['extract_id']}").status_code, 200)
        self.assertEqual(self.confirm(c, ex, checked=["0:entry_date"]).status_code, 404)

    def test_manual_entry_without_extraction(self):
        c = self.login()
        r = c.post("/api/documents/confirm", json={"document_kind": "移工失聯",
                                                   "workers": [{"name": "DEMO", "event_date": "2026-12-01", "job": "製造"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(len(r.json()["task_ids"]), 0)

    def test_unknown_kind_warns(self):
        c = self.login()
        r = c.post("/api/documents/confirm", json={"document_kind": "不存在的文件",
                                                   "workers": [{"name": "DEMO", "event_date": "2026-12-01"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["task_ids"], [])
        self.assertTrue(any("人工" in w for w in r.json()["warnings"]))


class ProofTest(S02Base):
    def make_task(self, c):
        c.post("/api/documents/confirm", json={"document_kind": "入國通報受理證明書",
                                               "workers": [{"name": "SITI DEMO RAHAYU", "entry_date": "2026-12-01"}]})
        return c.get("/api/todos").json()["items"][0]["id"]

    def test_proof_original_not_kept(self):
        c = self.login()
        tid = self.make_task(c)
        self.reply = json.dumps({"doc_kind": "健康檢查報告", "completed_date": "2026-12-02", "relevant": True,
                                 "worker_name": "PERSON_01", "evidence": "檢查日期 2026-12-02"})
        r = c.post(f"/api/todos/{tid}/proof", files={"file": ("proof.txt", "合成證明 SITI DEMO RAHAYU 檢查日期 2026-12-02".encode())})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("SITI DEMO RAHAYU", self.calls[-1]["user"])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(c.get(f"/api/todos/{tid}/proof/file").status_code, 404)

    def test_real_image_proof_blocked(self):
        c = self.login()
        tid = self.make_task(c)
        r = c.post(f"/api/todos/{tid}/proof", files={"file": ("p.png", png_bytes((1, 2, 3)))})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
