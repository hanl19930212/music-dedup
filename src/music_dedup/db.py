"""SQLite 缓存：指纹缓存 + 元数据刮削缓存。"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()

# 指纹缓存格式版本：1 = fpcalc -raw 的整数序列（可逐项比较）
FINGERPRINT_FORMAT = 1


class CacheDB:
    """线程安全的 SQLite 缓存数据库。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with _lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fingerprints (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    fingerprint TEXT,
                    duration REAL,
                    fp_format INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    fingerprint TEXT PRIMARY KEY,
                    recording_id TEXT,
                    title TEXT,
                    artist TEXT,
                    album TEXT,
                    genre TEXT,
                    year TEXT,
                    cover_url TEXT,
                    confidence REAL
                );

                CREATE INDEX IF NOT EXISTS idx_fp_recording
                    ON metadata(recording_id);
                """
            )
            # 老版本缓存里存的是 fpcalc 的压缩格式指纹，没法逐项比较，
            # 升级后直接清掉重算（压缩格式和 -raw 格式不能互相还原）
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(fingerprints)")
            }
            if "fp_format" not in columns:
                self._conn.execute(
                    "ALTER TABLE fingerprints ADD COLUMN fp_format INTEGER NOT NULL DEFAULT 0"
                )
            self._conn.execute(
                "DELETE FROM fingerprints WHERE fp_format != ?", (FINGERPRINT_FORMAT,)
            )
            self._conn.commit()

    # ---------- 指纹缓存 ----------

    @staticmethod
    def _key(path: Path) -> str:
        """缓存主键用绝对路径（大小写归一）。

        否则同一个文件用相对路径和绝对路径各扫一次就会算成两个文件，
        白白重跑一遍 fpcalc。

        这里刻意不用 Path.resolve()：Windows 上它会逐级访问文件系统，
        UNC 路径实测约 5ms/次，而每扫描一个文件要查一次、写一次缓存。
        abspath + normcase 纯字符串处理，同样能消除 "./" 和大小写差异。
        """
        return os.path.normcase(os.path.abspath(str(path)))

    def get_fingerprint(self, path: Path, size: int, mtime: float) -> Optional[tuple[str, float]]:
        """获取缓存的指纹，文件未变则命中。"""
        with _lock:
            row = self._conn.execute(
                "SELECT fingerprint, duration FROM fingerprints WHERE path=? AND size=? AND mtime=?",
                (self._key(path), size, mtime),
            ).fetchone()
        if row and row["fingerprint"]:
            return (row["fingerprint"], row["duration"])
        return None

    def set_fingerprint(
        self, path: Path, size: int, mtime: float, fingerprint: str, duration: float
    ) -> None:
        with _lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO fingerprints
                   (path, size, mtime, fingerprint, duration, fp_format)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self._key(path), size, mtime, fingerprint, duration, FINGERPRINT_FORMAT),
            )
            self._conn.commit()

    # ---------- 元数据缓存 ----------

    def get_metadata(self, fingerprint: str) -> Optional[dict[str, Any]]:
        with _lock:
            row = self._conn.execute(
                "SELECT * FROM metadata WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def set_metadata(self, fingerprint: str, data: dict[str, Any]) -> None:
        with _lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO metadata
                   (fingerprint, recording_id, title, artist, album, genre, year, cover_url, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fingerprint,
                    data.get("recording_id"),
                    data.get("title"),
                    data.get("artist"),
                    data.get("album"),
                    data.get("genre"),
                    data.get("year"),
                    data.get("cover_url"),
                    data.get("confidence"),
                ),
            )
            self._conn.commit()

    # ---------- 统计 ----------

    def stats(self) -> dict[str, int]:
        with _lock:
            fp_count = self._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
            meta_count = self._conn.execute("SELECT COUNT(*) FROM metadata").fetchone()[0]
        return {"fingerprints": fp_count, "metadata": meta_count}

    def clear(self) -> None:
        with _lock:
            self._conn.executescript("DELETE FROM fingerprints; DELETE FROM metadata;")
            self._conn.commit()

    def close(self) -> None:
        with _lock:
            self._conn.close()
