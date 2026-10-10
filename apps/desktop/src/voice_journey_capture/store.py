"""Local sessions: ``<data dir>/sessions/<sessionId>/`` with the WAVs, ``session.json`` and ``upload.json``.

A completed session stays here until the server accepts it; every launch
retries the ones still pending. An aborted session is kept (its reason in
``session.json``) and never uploaded. Accepted sessions are kept too until
``--prune-accepted`` removes them (the server holds them from then on).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .package import PackageError, read_session_json
from .pin import now_iso

UPLOAD_FILE = "upload.json"


@dataclass(frozen=True)
class LocalSession:
    session_id: str
    path: Path
    status: str  # completed | aborted | recording (interrupted)
    upload: dict

    @property
    def upload_state(self) -> str:
        """pending | accepted | failed | not-uploadable"""
        if self.status != "completed":
            return "not-uploadable"
        return str(self.upload.get("state", "pending"))


class SessionStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "sessions"

    def new_dir(self, session_id: str) -> Path:
        path = self.root / session_id
        path.mkdir(parents=True, exist_ok=False)
        return path

    def sessions(self) -> list[LocalSession]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(p for p in self.root.iterdir() if p.is_dir()):
            try:
                status = str(read_session_json(path).get("status", "recording"))
            except PackageError:
                status = "recording"  # interrupted before session.json was written
            out.append(LocalSession(path.name, path, status, self._upload(path)))
        return out

    def pending(self) -> list[LocalSession]:
        return [s for s in self.sessions() if s.upload_state == "pending"]

    def _upload(self, path: Path) -> dict:
        try:
            data = json.loads((path / UPLOAD_FILE).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_upload(self, path: Path, data: dict) -> None:
        tmp = path / (UPLOAD_FILE + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path / UPLOAD_FILE)

    def record_attempt(self, path: Path, error: str | None) -> None:
        data = self._upload(path)
        data["state"] = "pending"
        data["attempts"] = int(data.get("attempts", 0)) + 1
        data["lastAttemptAt"] = now_iso()
        data["lastError"] = error
        self._write_upload(path, data)

    def mark_accepted(self, path: Path, server: str) -> None:
        data = self._upload(path)
        data.update({"state": "accepted", "acceptedAt": now_iso(), "server": server, "lastError": None})
        self._write_upload(path, data)

    def mark_failed(self, path: Path, error: str) -> None:
        """A permanent refusal (the server will never take this package as it is): not retried."""
        data = self._upload(path)
        data.update({"state": "failed", "failedAt": now_iso(), "lastError": error})
        self._write_upload(path, data)

    def retry_failed(self, path: Path) -> None:
        data = self._upload(path)
        data.update({"state": "pending", "lastError": None})
        self._write_upload(path, data)

    def prune_accepted(self) -> int:
        count = 0
        for session in self.sessions():
            if session.upload_state == "accepted":
                shutil.rmtree(session.path)
                count += 1
        return count
