"""竹聘安 AI demo server.

Run:  python -m uvicorn app:app --port 8765     (from demo/server)
Uses the same validated modules as ../../validation/scripts (rules, retrieval, checker).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import shutil
import sqlite3

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
VALID = HERE.parent.parent / "validation" / "scripts"
sys.path.insert(0, str(VALID))

import auth  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import penalty as pen_mod  # noqa: E402
import qa as qa_mod  # noqa: E402
from extract import EXTRACT_SYS  # noqa: E402
from privacy import PROMPT_NOTE, mask, restore  # noqa: E402  (personal data masked before any LLM call)
from rules import CAL_YEARS, compute  # noqa: E402

SERVICE = "zhupinan-demo"
SERVICE_VERSION = "2026.11-finals-s00"
LAWS = HERE.parent.parent / "validation" / "laws"

# DEMO_DATA=true is the single marker that this server runs on synthetic data only.
# This build is not cleared for real documents or real worker data, so any other value refuses to start.
DEMO_DATA = os.environ.get("DEMO_DATA", "true").strip().lower()
if DEMO_DATA != "true":
    raise RuntimeError("此版本僅供合成資料示範：DEMO_DATA 必須為 true（未經法規與資安審查，不得處理真實資料）")

WEB = HERE.parent / "web"
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
UPLOADS = Path(os.environ.get("ZP_UPLOAD_DIR") or HERE / "uploads")
UPLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="竹聘安 AI demo")

# ---------------------------------------------------------------- database, sessions (S01)
db.init_db()
_boot = db.connect()
_boot.isolation_level = None
_generated_codes = auth.load_env_codes(_boot)
_boot.close()
if _generated_codes:  # printed to the server's own console only; never returned by any API
    print("[竹聘安] 未設定 DEMO_LOGIN_CODES，本次啟動的示範登入碼：", file=sys.stderr)
    for _acct, _code in _generated_codes.items():
        print(f"  {_acct}: {_code}", file=sys.stderr)

DB = Depends(db.get_db)
EMPLOYER = Depends(auth.require_employer)
OFFICER = Depends(auth.require_officer)
ANYONE = Depends(auth.current_account)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Reject cross-site writes before any handler runs; never let case or Q&A responses be cached."""
    if request.url.path.startswith("/api/") and auth.cross_site(request):
        return JSONResponse({"detail": "拒絕跨站請求"}, status_code=403)
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif "cache-control" not in response.headers:
        # static UI: always revalidate so a demo machine never runs yesterday's JS/CSS (S09 adds versioned caching)
        response.headers["Cache-Control"] = "no-cache"
    return response


class Login(BaseModel):
    account: str
    code: str


def _cases(con: sqlite3.Connection, owner_id: int) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, display_code, label, created_at, updated_at FROM cases WHERE owner_id=? ORDER BY id", (owner_id,))]


def _me(con: sqlite3.Connection, acct: sqlite3.Row) -> dict:
    return {"account": {"login_name": acct["login_name"], "role": acct["role"], "display_name": acct["display_name"],
                        "is_demo_admin": bool(acct["is_demo_admin"])},
            "cases": _cases(con, acct["id"]) if acct["role"] == "employer" else []}


@app.post("/api/session")
def session_login(body: Login, request: Request, response: Response, con: sqlite3.Connection = DB):
    acct = auth.login(con, request, response, body.account.strip(), body.code.strip())
    return _me(con, acct)


@app.get("/api/session")
def session_me(acct=ANYONE, con: sqlite3.Connection = DB):
    return _me(con, acct)


@app.delete("/api/session")
def session_logout(request: Request, response: Response, con: sqlite3.Connection = DB):
    auth.logout(con, request, response)
    return {"ok": True}


@app.get("/api/cases")
def list_cases(acct=EMPLOYER, con: sqlite3.Connection = DB):
    return {"cases": _cases(con, acct["id"])}


def owned_case(con: sqlite3.Connection, acct: sqlite3.Row, case_id: int | None) -> sqlite3.Row:
    """The caller's case, or 404 (never 403) so other owners' case IDs are not revealed."""
    if case_id is None:
        row = con.execute("SELECT * FROM cases WHERE owner_id=? ORDER BY id LIMIT 1", (acct["id"],)).fetchone()
    else:
        row = con.execute("SELECT * FROM cases WHERE id=? AND owner_id=?", (case_id, acct["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "找不到這個案件")
    return row


@app.get("/api/cases/{case_id}")
def get_case(case_id: int, acct=EMPLOYER, con: sqlite3.Connection = DB):
    case = owned_case(con, acct, case_id)
    items = _task_items(con, acct["id"], case_id)
    return {"case": {k: case[k] for k in ("id", "display_code", "label", "created_at", "updated_at")},
            "tasks": items}


@app.post("/api/demo/reset")
def demo_reset(acct=Depends(auth.require_demo_admin), con: sqlite3.Connection = DB):
    db.reset_demo(con)
    for folder in (UPLOADS, llm.TMP):
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(exist_ok=True)
    with db.tx(con):
        db.audit(con, acct["id"], None, "demo", None, "reset")
    return {"ok": True}

# ---------------------------------------------------------------- extraction
DEMO_EXTRACT_SYS = EXTRACT_SYS + """

另外，請在每位 worker 物件中加入 "evidence" 欄位：{"entry_date": "<該日期在文件中的原文片段>", ...}，
只列出非 null 的欄位，原文片段照抄文件（例如「一一五年三月十日」），不要改寫。"""


def ingest(raw: bytes, filename: str, folder: Path, prefix: str = ""):
    """Save an upload; return (path, image_path_or_None, text_or_None)."""
    suffix = Path(filename or "upload").suffix.lower()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "檔案太大（上限 15MB）")
    fid = prefix + uuid.uuid4().hex[:10]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fid}{suffix}"
    path.write_bytes(raw)
    image, text_doc = None, None
    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
        image = path
    elif suffix == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        page_text = "\n".join(p.get_text() for p in doc)
        if len(page_text.strip()) > 50:
            text_doc = page_text
        else:
            image = folder / f"{fid}.png"
            doc[0].get_pixmap(dpi=150).save(image)
    elif suffix in (".txt", ".md"):
        text_doc = raw.decode("utf-8", errors="replace")
    else:
        raise HTTPException(415, "請上傳圖片（JPG/PNG）、PDF 或文字檔")
    return path, image, text_doc


def read_with_llm(system: str, image: Path | None, text_doc: str | None, intro: str):
    """Images go to Sonnet (validation: Haiku 0% on scans, Sonnet 78.6%); text goes to Haiku."""
    try:
        if image is not None:
            # pixels cannot be masked locally; the UI tells the user (government version: on-prem model)
            out, model = llm.call(system, "請只輸出 JSON。", model="sonnet", image=image), "sonnet"
            return llm.parse_json(out), model
        masked, mapping = mask(text_doc)
        out, model = llm.call(system + PROMPT_NOTE, intro + "\n\n" + masked, model="haiku"), "haiku"
        return restore(llm.parse_json(out), mapping), model
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/extract")
async def extract(file: UploadFile = File(...), acct=EMPLOYER):
    raw = await file.read()
    path, image, text_doc = ingest(raw, file.filename, UPLOADS)
    fid = path.stem
    data, model = read_with_llm(DEMO_EXTRACT_SYS, image, text_doc, "以下是雇主上傳的文件內容：")
    # flag fields that need human confirmation
    for w in data.get("workers", []):
        flags = {}
        for k, src in (w.get("evidence") or {}).items():
            if any(c in (src or "") for c in "〇一二三四五六七八九十"):
                flags[k] = "國字日期，請再確認"
        if image is not None:
            for k in ("entry_date", "permit_start", "permit_end", "event_date"):
                if w.get(k) and k not in flags:
                    flags[k] = "掃描文件，請核對原件"
        w["flags"] = flags
    return {"file_id": fid, "filename": file.filename, "is_image": image is not None, "model": model, **data}


# ---------------------------------------------------------------- obligations
class Confirmed(BaseModel):
    doc_type: str
    workers: list[dict]
    save: bool = True
    case_id: int | None = None          # default: the caller's first case
    source_filename: str | None = None


# 辦理窗口（許願卡 1：「知道要做什麼、去哪裡辦」）。
# 機關依各條文所定之受理機關；線上連結只放官方新聞稿或問答集寫明可線上辦理者。
FWAPPLY = ("勞動部線上申辦系統", "https://fwapply.wda.gov.tw")   # 勞動部 2016-06-16 新聞稿：聘僱許可、行蹤不明通報、遞補等可線上申辦
HSINCHU_LABOR = "新竹市政府勞工及青年處（跨國勞動力服務中心）"
HOSPITAL = "衛生福利部指定醫院（受聘僱外國人健康檢查指定醫院）"
OFFICE = {
    "onestop_register": {"office": "移工一站式服務網（勞動部）", "online": ("移工一站式服務網", "https://fwots.wda.gov.tw/")},
    "apply_permit": {"office": "勞動部勞動力發展署", "online": FWAPPLY},
    "health_entry": {"office": HOSPITAL},
    "health_6m": {"office": HOSPITAL},
    "health_18m": {"office": HOSPITAL},
    "health_30m": {"office": HOSPITAL},
    "recheck_report": {"office": "新竹市政府衛生局（定期健檢再檢查證明備查）"},
    "dorm_change": {"office": HSINCHU_LABOR},
    "renewal_window": {"office": "勞動部勞動力發展署"},
    "permit_expiry": {"office": "勞動部勞動力發展署（續聘），或安排移工出國"},
    "termination_notice": {"office": f"{HSINCHU_LABOR}、內政部移民署、轄區警察機關"},
    "abscond_notice": {"office": f"{HSINCHU_LABOR}、內政部移民署、轄區警察機關，並副知勞動部", "online": FWAPPLY},
    "replacement_window": {"office": "勞動部勞動力發展署", "online": FWAPPLY},
}
HELPLINES = "有疑問：1955 勞工諮詢申訴專線（多國語言）、勞動力發展署 0800-035-688"


def with_office(o: dict) -> dict:
    info = OFFICE.get(o.get("key"), {})
    o["office"] = info.get("office")
    online = info.get("online")
    o["online"] = {"label": online[0], "url": online[1]} if online else None
    return o


@app.post("/api/obligations")
def obligations(body: Confirmed, acct=EMPLOYER, con: sqlite3.Connection = DB):
    """Compute deadlines with the deterministic rules; with save=true, store them in the caller's own case."""
    case = owned_case(con, acct, body.case_id) if body.save else None
    items = []
    for w in body.workers:
        for o in compute(body.doc_type, w):
            d = o.to_dict()
            d["days_left"] = (date.fromisoformat(d["due"]) - date.today()).days
            items.append(with_office(d))
    items.sort(key=lambda x: x["start"] or x["due"])
    added = len(items)
    if body.save:
        added = 0
        fields = json.dumps({"doc_type": body.doc_type, "workers": body.workers}, ensure_ascii=False, sort_keys=True)
        with db.tx(con):
            doc_id = con.execute(
                "INSERT INTO documents(case_id, kind, source_filename, confirmed_fields_json, confirmed_at, source_digest) "
                "VALUES (?,?,?,?,?,?)",
                (case["id"], body.doc_type, body.source_filename, fields, db.now(),
                 hashlib.sha256(fields.encode("utf-8")).hexdigest())).lastrowid
            for o in items:
                cur = con.execute(
                    "INSERT OR IGNORE INTO tasks(case_id, document_id, rule_key, worker, worker_norm, title, start_date, "
                    "due_date, law_ref, note, office, official_url, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (case["id"], doc_id, o["key"], o["worker"], _norm_worker(o["worker"]), o["title"], o.get("start"),
                     o["due"], o.get("basis"), o.get("note"), o.get("office"),
                     (o.get("online") or {}).get("url"), db.now()))
                added += cur.rowcount
            con.execute("UPDATE cases SET updated_at=? WHERE id=?", (db.now(), case["id"]))
            db.audit(con, acct["id"], case["id"], "document", doc_id, f"confirm:{added}_tasks")
    return {"obligations": items, "added": added, "calendar_years": sorted(CAL_YEARS),
            "case_id": case["id"] if case else None}


def _norm_worker(name: str) -> str:
    """Same worker + same task + same deadline = same to-do (prevents duplicates)."""
    return (name or "").strip().upper()


def _task_items(con: sqlite3.Connection, owner_id: int, case_id: int | None = None) -> list[dict]:
    """All tasks of the owner's cases, shaped like the pre-S01 /api/todos items. Owner filter is in SQL."""
    sql = ("SELECT t.*, c.display_code FROM tasks t JOIN cases c ON c.id = t.case_id WHERE c.owner_id = ?"
           + (" AND c.id = ?" if case_id is not None else "") + " ORDER BY t.due_date, t.id")
    rows = con.execute(sql, (owner_id, case_id) if case_id is not None else (owner_id,)).fetchall()
    items = []
    for t in rows:
        p = con.execute("SELECT * FROM proofs WHERE task_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        proof = json.loads(p["detail_json"]) if p and p["detail_json"] else None
        o = {"id": str(t["id"]), "case_id": t["case_id"], "case_code": t["display_code"], "key": t["rule_key"],
             "worker": t["worker"], "title": t["title"], "start": t["start_date"], "due": t["due_date"],
             "basis": t["law_ref"], "note": t["note"], "status": t["status"], "assigned": t["assigned"],
             "proof": proof, "days_left": (date.fromisoformat(t["due_date"]) - date.today()).days}
        # baseline behaviour kept until S03: an on-time/late proof counts as done (S03 adds manual confirmation)
        o["done"] = bool(proof and proof.get("verdict") in ("on_time", "late"))
        items.append(with_office(o))
    return items


@app.get("/api/todos")
def todos(acct=EMPLOYER, con: sqlite3.Connection = DB):
    items = _task_items(con, acct["id"])
    return {"workers": sorted({o["worker"] for o in items}), "items": items}


@app.delete("/api/todos")
def clear_todos(acct=EMPLOYER, con: sqlite3.Connection = DB):
    """Clear only the caller's own cases (documents, tasks, proofs) and their proof files."""
    files = [r["stored_file"] for r in con.execute(
        "SELECT p.stored_file FROM proofs p JOIN tasks t ON t.id = p.task_id JOIN cases c ON c.id = t.case_id "
        "WHERE c.owner_id = ? AND p.stored_file IS NOT NULL", (acct["id"],))]
    with db.tx(con):
        con.execute("DELETE FROM tasks WHERE case_id IN (SELECT id FROM cases WHERE owner_id=?)", (acct["id"],))
        con.execute("DELETE FROM documents WHERE case_id IN (SELECT id FROM cases WHERE owner_id=?)", (acct["id"],))
        db.audit(con, acct["id"], None, "case", None, "clear")
    for f in files:
        (UPLOADS / "proofs" / f).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------- proof of completion
def _find_todo(con: sqlite3.Connection, acct: sqlite3.Row, tid: str) -> dict:
    """The caller's own task, or 404."""
    if not tid.isdigit():
        raise HTTPException(404, "找不到這項待辦")
    row = con.execute("SELECT t.case_id FROM tasks t JOIN cases c ON c.id = t.case_id WHERE t.id=? AND c.owner_id=?",
                      (int(tid), acct["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "找不到這項待辦")
    return next(o for o in _task_items(con, acct["id"], row["case_id"]) if o["id"] == tid)


class Assign(BaseModel):
    assignee: str | None = None   # "仲介" or None


@app.post("/api/todos/{tid}/assign")
def assign(tid: str, body: Assign, acct=EMPLOYER, con: sqlite3.Connection = DB):
    task = _find_todo(con, acct, tid)
    who = body.assignee or None
    with db.tx(con):
        con.execute("UPDATE tasks SET assigned=?, status=CASE WHEN status IN ('todo','delegated') THEN ? ELSE status END "
                    "WHERE id=?", (who, "delegated" if who else "todo", int(tid)))
        db.audit(con, acct["id"], task["case_id"], "task", tid, "assign" if who else "unassign")
    return {"ok": True, "assigned": who}


PROOF_SYS = """你是「竹聘安 AI」的辦理證明核對模組。雇主或仲介上傳一份文件，用來證明某項移工法定待辦已經辦理。
你的工作只有抽取欄位，輸出 JSON（不要 markdown 程式碼框），不要判斷是否逾期：
{"doc_kind": "<文件種類，例如健康檢查報告、收件回執、許可函>",
 "completed_date": "<辦理完成日期 YYYY-MM-DD 或 null>",
 "date_label": "<該日期在文件上的欄位名稱，例如檢查日期、收件日期>",
 "evidence": "<該日期在文件中的原文片段，照抄>",
 "worker_name": "<文件上的外國人姓名，照抄；沒有則 null>",
 "relevant": <true/false：這份文件能否證明下方待辦已辦理>,
 "reason": "<一句繁體中文說明 relevant 的判斷>"}
規則：日期一律換算成西元；民國年+1911。completed_date 是「辦理完成」的日期（檢查日、收件日、通報日、核准日），
不是列印日、出生日、護照效期或下次應辦日。"""

VERDICT_TEXT = {
    "on_time": "準時完成", "late": "已辦理但逾期", "early": "早於辦理期間，不算完成",
    "mismatch": "文件與待辦不符", "unknown": "讀不到完成日期", "name_mismatch": "移工姓名不符",
}


def _norm_name(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def judge_proof(task: dict, data: dict) -> dict:
    """Deterministic verdict: the LLM only reads the date; this function decides on-time/late."""
    d = data.get("completed_date")
    verdict = "unknown"
    if data.get("relevant") is False:
        verdict = "mismatch"
    elif d:
        try:
            cd = date.fromisoformat(d)
            if task.get("start") and cd < date.fromisoformat(task["start"]):
                verdict = "early"
            elif cd > date.fromisoformat(task["due"]):
                verdict = "late"
            else:
                verdict = "on_time"
        except ValueError:
            verdict = "unknown"
    name_ok = True
    if data.get("worker_name"):
        a, b = _norm_name(data["worker_name"]), _norm_name(task["worker"])
        name_ok = bool(a and b and (a in b or b in a))
    if verdict in ("on_time", "late") and not name_ok:
        verdict = "name_mismatch"
    late_days = (date.fromisoformat(d) - date.fromisoformat(task["due"])).days if verdict == "late" else 0
    return {"verdict": verdict, "verdict_text": VERDICT_TEXT[verdict] + (f" {late_days} 天" if late_days else ""),
            "completed_date": d, "date_label": data.get("date_label"), "evidence": data.get("evidence"),
            "doc_kind": data.get("doc_kind"), "worker_name": data.get("worker_name"), "reason": data.get("reason")}


@app.post("/api/todos/{tid}/proof")
async def upload_proof(tid: str, file: UploadFile = File(...), by: str = Form("雇主"), acct=EMPLOYER,
                       con: sqlite3.Connection = DB):
    task = _find_todo(con, acct, tid)
    raw = await file.read()
    path, image, text_doc = ingest(raw, file.filename, UPLOADS / "proofs", prefix=f"{tid}_")
    sys_prompt = PROOF_SYS + f"\n\n待辦事項：{task['title']}\n移工：{task['worker']}\n法定期限：" + \
        (f"{task['start']} ～ {task['due']}" if task.get("start") else f"{task['due']} 前")
    data, model = read_with_llm(sys_prompt, image, text_doc, "以下是上傳的辦理證明：")
    result = judge_proof(task, data)
    result.update({"file": path.name, "filename": file.filename, "uploaded_by": by,
                   "uploaded_at": datetime.now().isoformat(timespec="minutes"), "model": model})
    with db.tx(con):
        old = [r["stored_file"] for r in con.execute("SELECT stored_file FROM proofs WHERE task_id=?", (int(tid),))]
        con.execute("DELETE FROM proofs WHERE task_id=?", (int(tid),))
        con.execute("INSERT INTO proofs(task_id, submitted_at, observed_date, verdict, detail_json, stored_file) "
                    "VALUES (?,?,?,?,?,?)", (int(tid), db.now(), result.get("completed_date"), result["verdict"],
                                             json.dumps(result, ensure_ascii=False), path.name))
        con.execute("UPDATE tasks SET status='proof_submitted' WHERE id=? AND status IN ('todo','delegated')", (int(tid),))
        db.audit(con, acct["id"], task["case_id"], "proof", tid, f"submit:{result['verdict']}")
    for f in old:
        if f and f != path.name:
            (UPLOADS / "proofs" / f).unlink(missing_ok=True)
    return result


@app.delete("/api/todos/{tid}/proof")
def delete_proof(tid: str, acct=EMPLOYER, con: sqlite3.Connection = DB):
    task = _find_todo(con, acct, tid)
    files = [r["stored_file"] for r in con.execute("SELECT stored_file FROM proofs WHERE task_id=?", (int(tid),))]
    with db.tx(con):
        con.execute("DELETE FROM proofs WHERE task_id=?", (int(tid),))
        con.execute("UPDATE tasks SET status=CASE WHEN assigned IS NULL THEN 'todo' ELSE 'delegated' END "
                    "WHERE id=? AND status='proof_submitted'", (int(tid),))
        db.audit(con, acct["id"], task["case_id"], "proof", tid, "delete")
    for f in files:
        if f:
            (UPLOADS / "proofs" / f).unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/todos/{tid}/proof/file")
def proof_file(tid: str, acct=EMPLOYER, con: sqlite3.Connection = DB):
    task = _find_todo(con, acct, tid)
    proof = task.get("proof") or {}
    f = UPLOADS / "proofs" / (proof.get("file") or "")
    if not proof.get("file") or not f.exists():
        raise HTTPException(404, "沒有證明檔案")
    return FileResponse(f, filename=proof.get("filename") or f.name)


# ---------------------------------------------------------------- legal Q&A
_ARTS = qa_mod.load_articles()
_RETR = qa_mod.Retriever2(_ARTS)


class Ask(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Ask, acct=ANYONE):
    q = body.question.strip()
    if not q:
        raise HTTPException(400, "請輸入問題")
    hits = _RETR.top(q)
    ctx = "\n\n".join(f"【{h['law']} {h['no']}】\n{h['text']}" for h in hits)
    try:
        mq, qmap = mask(q)
        ans = restore(llm.call(qa_mod.SYS_STRICT, f"參考條文：\n{ctx}\n\n問題：{mq}", model="haiku"), qmap)
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    cited = [h for h in hits if h["no"].replace("第", "").replace("條", "") in ans or h["no"] in ans]
    return {"answer": ans, "citations": cited or hits[:2], "retrieved": hits}


# ---------------------------------------------------------------- penalty checker
pen_mod.REF_ARTS[0] = ("就業服務法", ["第 46 條", "第 56 條", "第 57 條", "第 63 條", "第 67 條", "第 68 條"], "N0090001.txt")
PEN_SYS = pen_mod.SYS + """
另外，對每個 ok 為 false 的項目，加入 "fix" 欄位，提供可直接貼入裁處書的建議文字（一至兩句）。
注意：「製造工作」屬就業服務法第46條第1項第10款之工作。"""


class Draft(BaseModel):
    text: str


@app.post("/api/check-penalty")
def check_penalty(body: Draft, acct=OFFICER):
    if len(body.text.strip()) < 30:
        raise HTTPException(400, "請貼上完整的裁處書草稿")
    try:
        masked, mapping = mask(body.text)
        out = llm.call(PEN_SYS + PROMPT_NOTE + "\n\n" + pen_mod.refs(), "請檢核以下裁處書草稿：\n\n" + masked, model="haiku")
        data = restore(llm.parse_json(out), mapping)
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    items = [{"id": c, "name": pen_mod.CHECK_ZH[c], **(data.get(c) or {"ok": None, "reason": "未回傳"})} for c in pen_mod.CHECKS]
    return {"items": items, "masked_preview": masked}


@app.post("/api/mask-preview")
def mask_preview(body: Draft, acct=ANYONE):
    """Show exactly what would be sent to the AI (for the demo and for auditing)."""
    masked, mapping = mask(body.text)
    return {"masked": masked, "pseudonyms": len(mapping)}


# ---------------------------------------------------------------- LINE
LINE_ENV = HERE / "line.env"   # KEY=VALUE lines; written by the team, never committed or shared


def line_cfg() -> tuple[str, str]:
    """Read LINE credentials from env vars or line.env on every call (no restart needed)."""
    vals = {}
    if LINE_ENV.exists():
        for ln in LINE_ENV.read_text(encoding="utf-8-sig").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                vals[k.strip()] = v.strip().strip('"')
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or vals.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    secret = os.environ.get("LINE_CHANNEL_SECRET") or vals.get("LINE_CHANNEL_SECRET", "")
    return token, secret


def line_api(path: str, payload: dict) -> tuple[int, str]:
    token, _ = line_cfg()
    r = httpx.post(f"https://api.line.me/v2/bot/message/{path}", timeout=15,
                   headers={"Authorization": f"Bearer {token}"}, json=payload)
    return r.status_code, r.text[:300]


def bound_line_users(con: sqlite3.Connection, account_id: int) -> list[str]:
    """Active LINE bindings of one employer account. Pairing (S05) is the only way a row gets here;
    there is no global follower list any more, so nothing is ever broadcast."""
    return [r["line_user_id"] for r in con.execute(
        "SELECT line_user_id FROM line_bindings WHERE account_id=? AND revoked_at IS NULL", (account_id,))]


TEMPLATES = {
    "zh": "【竹聘安提醒】{status}\n{worker}：{title}\n期限：{when}\n依據：{basis}",
    "id": "[Pengingat Zhu Pin An] {status}\n{worker}: {title}\nBatas waktu: {when}\nDasar hukum: {basis}",
    "vi": "[Nhắc nhở Zhu Pin An] {status}\n{worker}: {title}\nHạn chót: {when}\nCăn cứ: {basis}",
}
STATUS = {
    "zh": ("已逾期 {n} 天，請儘速辦理", "還剩 {n} 天", "{n} 天後開始辦理"),
    "id": ("Terlambat {n} hari, segera urus", "Sisa {n} hari", "Mulai dalam {n} hari"),
    "vi": ("Đã quá hạn {n} ngày, vui lòng xử lý ngay", "Còn {n} ngày", "Bắt đầu sau {n} ngày"),
}


def reminder_text(o, lang="zh"):
    lang = lang if lang in TEMPLATES else "zh"
    when = f"{o['start']} ～ {o['due']}" if o.get("start") else o["due"]
    over, left, starts = STATUS[lang]
    start_left = (date.fromisoformat(o["start"]) - date.today()).days if o.get("start") else None
    if o["days_left"] < 0:
        status = over.format(n=-o["days_left"])
    elif o["days_left"] == 0:
        status = {"zh": "今天截止，請立即辦理", "id": "Batas waktu hari ini", "vi": "Hạn chót hôm nay"}[lang]
    elif start_left is not None and start_left > 0:
        status = starts.format(n=start_left)
    else:
        status = left.format(n=o["days_left"])
    title = TITLE_I18N.get(lang, {}).get(o["key"], o["title"])
    text = TEMPLATES[lang].format(status=status, worker=o["worker"], title=title, when=when, basis=o["basis"])
    office = o.get("office") or OFFICE.get(o.get("key"), {}).get("office")
    if office:
        text += "\n" + {"zh": "辦理窗口：", "id": "Tempat pengurusan: ", "vi": "Nơi làm thủ tục: "}[lang] + office
    online = OFFICE.get(o.get("key"), {}).get("online")
    if online:
        text += "\n" + online[1]
    return text


# translation drafts (to be reviewed by native speakers before real use)
TITLE_I18N = {
    "id": {"apply_permit": "Ajukan izin kerja (聘僱許可)", "health_entry": "Pemeriksaan kesehatan dalam 3 hari kerja setelah tiba",
           "health_6m": "Pemeriksaan kesehatan berkala bulan ke-6", "health_18m": "Pemeriksaan kesehatan berkala bulan ke-18",
           "health_30m": "Pemeriksaan kesehatan berkala bulan ke-30", "renewal_window": "Ajukan perpanjangan atau pindah majikan",
           "permit_expiry": "Izin kerja berakhir", "recheck_report": "Laporkan hasil pemeriksaan ulang ke dinas kesehatan",
           "dorm_change": "Laporkan perubahan tempat tinggal", "termination_notice": "Laporkan pemutusan hubungan kerja",
           "abscond_notice": "Laporkan pekerja yang hilang kontak (3 hari)", "replacement_window": "Ajukan penggantian pekerja",
           "onestop_register": "Daftar layanan terpadu (一站式) paling lambat 5 hari sebelum tiba"},
    "vi": {"apply_permit": "Nộp đơn xin giấy phép tuyển dụng (聘僱許可)", "health_entry": "Khám sức khỏe trong 3 ngày làm việc sau khi nhập cảnh",
           "health_6m": "Khám sức khỏe định kỳ tháng thứ 6", "health_18m": "Khám sức khỏe định kỳ tháng thứ 18",
           "health_30m": "Khám sức khỏe định kỳ tháng thứ 30", "renewal_window": "Nộp đơn gia hạn hoặc chuyển chủ khi hết hạn",
           "permit_expiry": "Giấy phép tuyển dụng hết hạn", "recheck_report": "Nộp kết quả khám lại cho cơ quan y tế",
           "dorm_change": "Thông báo thay đổi nơi ở", "termination_notice": "Thông báo chấm dứt quan hệ lao động",
           "abscond_notice": "Thông báo lao động mất liên lạc (3 ngày)", "replacement_window": "Nộp đơn xin bổ sung lao động",
           "onestop_register": "Đăng ký dịch vụ một cửa (一站式) chậm nhất 5 ngày trước khi nhập cảnh"},
}


@app.get("/api/line/status")
def line_status(acct=EMPLOYER, con: sqlite3.Connection = DB):
    token, secret = line_cfg()
    n = len(bound_line_users(con, acct["id"]))
    return {"configured": bool(token and secret), "bound": n, "followers": n}


class Push(BaseModel):
    days: int = 30
    lang: str = "zh"


def _due_items(con: sqlite3.Connection, account_id: int, days: int):
    out = []
    for o in _task_items(con, account_id):
        start_left = (date.fromisoformat(o["start"]) - date.today()).days if o.get("start") else None
        in_range = o["days_left"] <= days or (start_left is not None and 0 <= start_left <= days)
        if in_range and not o.get("done"):   # completed (proof verified) items are no longer reminded
            out.append(o)
    return out


BROKER_PENDING = {"zh": "※ 已委任仲介辦理，尚未上傳辦理證明", "id": "※ Diserahkan ke agen, bukti belum diunggah",
                  "vi": "※ Đã giao cho môi giới, chưa tải lên chứng từ"}


def _broker_flag(o, lang):
    return ("\n" + BROKER_PENDING[lang]) if o.get("assigned") == "仲介" and not o.get("proof") else ""


DIGEST_HEAD = {"zh": "【竹聘安提醒】近期待辦 {n} 項", "id": "[Pengingat Zhu Pin An] {n} tugas mendatang",
               "vi": "[Nhắc nhở Zhu Pin An] {n} việc sắp đến hạn"}
DIGEST_MORE = {"zh": "…另有 {n} 項，請到竹聘安網頁查看", "id": "…dan {n} tugas lainnya di situs Zhu Pin An",
               "vi": "…và {n} việc khác trên trang Zhu Pin An"}
SEP = "\n────────\n"


def digest(items, lang="zh", limit=8):
    """One LINE message listing all due items (instead of one message per item)."""
    lang = lang if lang in TEMPLATES else "zh"
    body = SEP.join(f"{i}. " + reminder_text(o, lang).split("\n", 1)[0].split("】")[-1].split("] ")[-1] + "\n"
                    + reminder_text(o, lang).split("\n", 1)[1] + _broker_flag(o, lang)
                    for i, o in enumerate(items[:limit], 1))
    text = DIGEST_HEAD[lang].format(n=len(items)) + SEP + body
    if len(items) > limit:
        text += SEP + DIGEST_MORE[lang].format(n=len(items) - limit)
    return text[:4900]  # LINE text limit is 5000 chars


@app.get("/api/line/preview")
def line_preview(days: int = 30, lang: str = "zh", acct=EMPLOYER, con: sqlite3.Connection = DB):
    items = _due_items(con, acct["id"], days)
    return {"count": len(items), "text": digest(items, lang) if items else ""}


@app.post("/api/line/send")
def line_send(body: Push, acct=EMPLOYER, con: sqlite3.Connection = DB):
    """Push only this employer's own items, only to LINE users bound to this employer."""
    items = _due_items(con, acct["id"], body.days)
    messages = [digest(items, body.lang)] if items else []
    if not messages:
        return {"sent": 0, "preview": [], "mode": "none", "note": "近期沒有需要提醒的事項"}
    token, secret = line_cfg()
    if not (token and secret):
        return {"sent": 0, "preview": messages, "mode": "preview", "note": "尚未設定 LINE Messaging API，僅顯示預覽"}
    users = bound_line_users(con, acct["id"])
    if not users:
        return {"sent": 0, "preview": messages, "mode": "preview",
                "note": "這個帳號還沒有綁定 LINE（配對功能 S05 開發中），僅顯示預覽，不會發送給任何人。"}
    sent, errors = 0, []
    for uid in users:
        code, text = line_api("push", {"to": uid, "messages": [{"type": "text", "text": m} for m in messages]})
        if code == 200:
            sent += 1
        else:
            errors.append(f"{code} {text}")
    note = f"已推播給此帳號綁定的 {sent} 個 LINE（1 則訊息，含 {len(items)} 項待辦）" + (f"；失敗：{errors[0]}" if errors else "")
    return {"sent": sent, "preview": messages, "mode": "line", "note": note}


WELCOME = ("歡迎使用竹聘安 AI！\n我會在移工的法定期限（健檢、通報、續聘）快到時提醒您，每則提醒都附法條依據。\n\n"
           "請先在竹聘安網頁取得配對碼並傳到這裡；綁定後傳「待辦」可查看您自己的近期待辦。\n（決賽展示版本，資料為合成測試資料）")
NOT_BOUND = "這個 LINE 帳號尚未綁定竹聘安雇主帳號，因此不會顯示任何待辦。請先在竹聘安網頁取得配對碼。"


@app.post("/line/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("x-line-signature", "")
    _, secret = line_cfg()
    expect = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    if not secret or not hmac.compare_digest(sig, expect):
        raise HTTPException(403, "bad signature")
    payload = json.loads(body)
    con = db.connect()
    try:
        for ev in payload.get("events", []):
            _line_event(con, ev)
    finally:
        con.close()
    return {"ok": True}


def _line_event(con: sqlite3.Connection, ev: dict) -> None:
    """Reply only with the bound employer's own items; an unbound LINE user gets pairing help, never data."""
    uid = (ev.get("source") or {}).get("userId")
    token = ev.get("replyToken")
    if not token:
        return
    if ev.get("type") == "follow":
        line_api("reply", {"replyToken": token, "messages": [{"type": "text", "text": WELCOME}]})
    elif ev.get("type") == "message" and (ev.get("message") or {}).get("type") == "text":
        text = ev["message"].get("text", "")
        row = con.execute("SELECT account_id FROM line_bindings WHERE line_user_id=? AND revoked_at IS NULL",
                          (uid,)).fetchone() if uid else None
        if ("待辦" in text or "提醒" in text) and row is None:
            msgs = [NOT_BOUND]
        elif "待辦" in text or "提醒" in text:
            items = _due_items(con, row["account_id"], 90)
            msgs = [digest(items)] if items else ["目前 90 天內沒有待辦事項。"]
        else:
            msgs = ["您好！傳「待辦」可查看近期待辦；法規問題請到竹聘安網頁的「法規問答」查詢。"]
        line_api("reply", {"replyToken": token, "messages": [{"type": "text", "text": m} for m in msgs]})


# ---------------------------------------------------------------- static
def law_versions() -> dict[str, str]:
    """Statute name -> amendment date, read from the header of each law text used by rules and retrieval."""
    out = {}
    for f in sorted(LAWS.glob("*.txt")):
        head = f.read_text(encoding="utf-8").splitlines()[:2]
        name = head[0].lstrip("# ").strip() if head else f.stem
        amended = head[1].split("：", 1)[-1].strip() if len(head) > 1 else ""
        out[name] = amended
    return out


_LAW_VERSIONS = law_versions()


@app.get("/api/health")
def health():
    """Service status for the demo machine. Never returns keys, tokens or case data."""
    token, secret = line_cfg()
    return {
        "ok": True,
        "service": SERVICE,
        "version": SERVICE_VERSION,
        "demo_mode": DEMO_DATA == "true",
        "rules": {"engine": "validation/scripts/rules.py", "law_versions": _LAW_VERSIONS},
        "calendar_years": sorted(CAL_YEARS),
        "articles": len(_ARTS),
        "line_configured": bool(token and secret),
    }


if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse({"detail": f"系統錯誤：{type(exc).__name__}"}, status_code=500)
