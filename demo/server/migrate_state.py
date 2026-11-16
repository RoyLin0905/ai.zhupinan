"""Move pre-S01 synthetic demo data from data/state.json into one employer's case in SQLite (S01).

    .venv\\Scripts\\python demo\\server\\migrate_state.py --account employer_a           # dry run
    .venv\\Scripts\\python demo\\server\\migrate_state.py --account employer_a --apply   # write

Before writing, the database file and state.json are copied to data/backup-<timestamp>/.
Only synthetic data (DEMO_DATA=true) may be migrated. state.json is left in place; delete it yourself afterwards.
Proof results are not migrated: re-upload proofs after migration (S02/S03 change how proofs are stored).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, help="employer login name, e.g. employer_a")
    ap.add_argument("--state", default=str(HERE / "data" / "state.json"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    state_path = Path(a.state)
    if not state_path.exists():
        print(f"沒有 {state_path}，不需要遷移。")
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    docs = state.get("docs", [])
    n_tasks = sum(len(d.get("obligations", [])) for d in docs)
    print(f"state.json：{len(docs)} 份文件、{n_tasks} 項待辦（含可能的重複）")

    db.init_db()
    con = db.connect()
    con.isolation_level = None
    try:
        acct = con.execute("SELECT id, role FROM accounts WHERE login_name=?", (a.account,)).fetchone()
        if not acct or acct["role"] != "employer":
            print(f"找不到雇主帳號 {a.account}")
            return 2
        case = con.execute("SELECT id, display_code FROM cases WHERE owner_id=? ORDER BY id LIMIT 1", (acct["id"],)).fetchone()
        print(f"目標：{a.account} 的案件 {case['display_code']}")
        if not a.apply:
            print("（試跑，未寫入。加上 --apply 才會寫入。）")
            return 0

        backup = HERE / "data" / f"backup-{datetime.now():%Y%m%d-%H%M%S-%f}"
        backup.mkdir(parents=True)
        shutil.copy2(state_path, backup / "state.json")
        if db.db_path().exists():
            shutil.copy2(db.db_path(), backup / db.db_path().name)
        print(f"已備份到 {backup}")

        added = 0
        with db.tx(con):
            for d in docs:
                fields = json.dumps({"doc_type": d.get("doc_type"), "workers": d.get("workers", [])},
                                    ensure_ascii=False, sort_keys=True)
                doc_id = con.execute(
                    "INSERT INTO documents(case_id, kind, source_filename, confirmed_fields_json, confirmed_at, source_digest) "
                    "VALUES (?,?,?,?,?,?)", (case["id"], d.get("doc_type") or "未知", "migrated:state.json", fields,
                                             db.now(), hashlib.sha256(fields.encode("utf-8")).hexdigest())).lastrowid
                for o in d.get("obligations", []):
                    added += con.execute(
                        "INSERT OR IGNORE INTO tasks(case_id, document_id, rule_key, worker, worker_norm, title, start_date, "
                        "due_date, law_ref, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (case["id"], doc_id, o["key"], o["worker"], o["worker"].strip().upper(), o["title"],
                         o.get("start"), o["due"], o.get("basis"), o.get("note"), db.now())).rowcount
            db.audit(con, None, case["id"], "migration", None, f"state.json:{added}_tasks")
        print(f"完成：新增 {added} 項待辦（重複的已略過）。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
