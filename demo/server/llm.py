"""Thin wrapper around the local Claude CLI (Pro subscription auth) with a disk cache.

The cache makes the finals demo fast and robust: the same input returns the
saved answer instantly, and a cached answer still works if the network drops.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)
TMP = ROOT / "tmp"
TMP.mkdir(exist_ok=True)


class LLMError(RuntimeError):
    pass


def _claude() -> str:
    for c in ("claude.cmd", "claude"):
        p = shutil.which(c)
        if p:
            return p
    raise LLMError("找不到 claude CLI，請先安裝並登入（claude auth login）")


def call(system: str, user: str, model: str = "haiku", image: Path | None = None, timeout: int = 240,
         cache: bool = True) -> str:
    """cache=False for anything a user uploaded: their (masked) content must not be kept on disk."""
    key_src = json.dumps([system, user, model, hashlib.sha256(image.read_bytes()).hexdigest() if image else None])
    key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:24]
    hit = CACHE / f"{key}.json"
    if cache and hit.exists():
        return json.loads(hit.read_text(encoding="utf-8"))["text"]

    exe = _claude()  # fail before writing anything if the CLI is missing
    sp = TMP / f"sys_{key}_{os.getpid()}_{time.monotonic_ns()}.txt"
    sp.write_text(system, encoding="utf-8")
    cmd = [exe, "-p", "--model", model, "--system-prompt-file", str(sp),
           "--strict-mcp-config", "--no-session-persistence", "--output-format", "json"]
    if image is not None:
        cmd += ["--tools", "Read", "--allowedTools", "Read", "--add-dir", str(image.parent)]
        user = f"Use the Read tool to open this uploaded document image, then follow the instructions: {image}\n\n{user}"
    else:
        cmd += ["--tools", ""]
    try:
        p = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                           timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired as e:
        raise LLMError("AI 回應逾時，請再試一次") from e
    finally:
        sp.unlink(missing_ok=True)  # the prompt file is only needed while the CLI runs
    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"AI 回應格式錯誤：{(p.stdout or p.stderr)[:200]}") from e
    if env.get("is_error"):
        msg = env.get("result") or ""
        if "401" in msg or "authenticate" in msg.lower():
            raise LLMError("Claude CLI 登入已過期，請在終端機執行 claude auth login")
        raise LLMError(f"AI 呼叫失敗：{msg[:200]}")
    text = env.get("result") or ""
    if cache:
        hit.write_text(json.dumps({"text": text, "model": model}, ensure_ascii=False), encoding="utf-8")
    return text


def parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError("AI 沒有回傳可解析的資料")
    return json.loads(m.group(0))
