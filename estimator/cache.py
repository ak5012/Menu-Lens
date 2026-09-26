"""Remembered answers, so the same dish at the same restaurant costs one model call ever.

Stored in a small SQLite file next to this module (estimator/cache.db), so it survives
server restarts. Keys include the provider, so mock answers never stand in for real ones.
Delete the file to start fresh.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "cache.db"
MAX_AGE_S = 30 * 24 * 3600  # re-ask after a month, in case the prompt or model improved


class EstimateCache:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS estimates "
                         "(key TEXT PRIMARY KEY, status INTEGER, body TEXT, saved REAL)")
        self._db.commit()
        self._lock = threading.Lock()

    @staticmethod
    def key(provider: str, prompt: str) -> str:
        normalized = " ".join(prompt.lower().split())
        return hashlib.sha256(f"{provider}\n{normalized}".encode()).hexdigest()

    def get(self, key: str) -> tuple[int, dict] | None:
        with self._lock:
            row = self._db.execute("SELECT status, body, saved FROM estimates WHERE key = ?",
                                   (key,)).fetchone()
        if not row or time.time() - row[2] > MAX_AGE_S:
            return None
        return row[0], json.loads(row[1])

    def put(self, key: str, status: int, body: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO estimates VALUES (?, ?, ?, ?)",
                             (key, status, json.dumps(body), time.time()))
            self._db.commit()
