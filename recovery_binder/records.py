"""Append-safe bind result writers under Results/."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterable

_lock = threading.Lock()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(value) -> str:
    s = "" if value is None else str(value)
    if any(ch in s for ch in [",", '"', "\n", "\r"]):
        return '"' + s.replace('"', '""') + '"'
    return s


def append_csv(path: str, headers: list[str], row: Iterable) -> None:
    parent = os.path.dirname(path)
    if parent:
        _ensure_dir(parent)
    with _lock:
        exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8", newline="") as f:
            if not exists:
                f.write(",".join(headers) + "\n")
            f.write(",".join(_esc(v) for v in row) + "\n")


def write_bind_status(
    results_dir: str,
    *,
    account: str,
    status: str,
    emails: list[str] | None = None,
    note: str = "",
    error: str | None = None,
) -> None:
    emails = [e.lower().strip() for e in (emails or []) if e]
    path = os.path.join(results_dir, "bind-status.csv")
    append_csv(
        path,
        ["time", "account", "status", "email1", "email2", "emails", "note", "error"],
        [
            _utc_now(),
            account,
            status,
            emails[0] if len(emails) > 0 else "",
            emails[1] if len(emails) > 1 else "",
            "|".join(emails),
            note,
            error or "",
        ],
    )


def merge_recovery_map(
    results_dir: str,
    *,
    account: str,
    status: str,
    emails: list[str] | None = None,
    note: str = "",
    error: str | None = None,
) -> dict:
    emails = [e.lower().strip() for e in (emails or []) if e]
    path = os.path.join(results_dir, "bind-recovery-map.json")
    _ensure_dir(results_dir)
    key = account.lower().strip()
    data: dict = {}
    with _lock:
        for attempt in range(5):
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f) or {}
                break
            except Exception:
                time.sleep(0.05 * (attempt + 1))
                data = {}
        prev = data.get(key) or {}
        merged = list(dict.fromkeys([*(prev.get("emails") or []), *emails]))
        data[key] = {
            "account": account,
            "status": status,
            "emails": merged,
            "note": note or prev.get("note") or "",
            "error": error,
            "updatedAt": _utc_now(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return data[key]


def append_recovery_email(
    results_dir: str,
    *,
    account: str,
    alt_email: str,
    code: str = "",
    step: int | str = "",
) -> None:
    path = os.path.join(results_dir, "all-recovery-emails.csv")
    append_csv(
        path,
        ["account", "altEmail", "code", "step", "time"],
        [account, alt_email, code, step, _utc_now()],
    )


def dump_debug(
    results_dir: str,
    label: str,
    *,
    text: str = "",
    html: str = "",
    meta: dict | None = None,
) -> str | None:
    debug_dir = os.path.join(results_dir, "bind-debug")
    _ensure_dir(debug_dir)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in label)[:80]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    base = os.path.join(debug_dir, f"{stamp}-{safe}")
    if text:
        with open(base + ".txt", "w", encoding="utf-8") as f:
            f.write(text)
    if html:
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(html)
    if meta is not None:
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    return base


def record_bind_outcome(
    results_dir: str | None,
    *,
    account: str,
    status: str,
    emails: list[str] | None = None,
    note: str = "",
    error: str | None = None,
) -> None:
    if not results_dir:
        return
    emails = emails or []
    write_bind_status(
        results_dir,
        account=account,
        status=status,
        emails=emails,
        note=note,
        error=error,
    )
    merge_recovery_map(
        results_dir,
        account=account,
        status=status,
        emails=emails,
        note=note,
        error=error,
    )
