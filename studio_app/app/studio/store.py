"""Versioned strategy storage (STUDIO_SPEC 1.2). Append-only SQLite.

INTEGRATION POINT: replace `_connect` with your app's existing SQLite
session/engine if you prefer a single database file.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import StrategyDefinition

DB_PATH = Path(__file__).resolve().parents[2] / "studio.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    json        TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS strategy_meta (
    strategy_id    TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    latest_version INTEGER NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_drafts (
    strategy_id TEXT PRIMARY KEY,
    json        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS optimize_runs (
    run_id      TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    is_draft    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,            -- running | done | failed
    results     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_suggestions (
    id            TEXT PRIMARY KEY,
    strategy_id   TEXT NOT NULL,
    block         TEXT NOT NULL,
    suggestion    TEXT NOT NULL,
    trial_metrics TEXT,
    baseline      TEXT,
    status        TEXT NOT NULL,   -- review | accepted | rejected | failed
    note          TEXT,
    source        TEXT NOT NULL DEFAULT 'manual',  -- manual | loop
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_loops (
    loop_id     TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    config      TEXT NOT NULL,
    status      TEXT NOT NULL,     -- running | done | failed
    note        TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_iterations (
    loop_id       TEXT NOT NULL,
    n             INTEGER NOT NULL,
    suggestion_id TEXT NOT NULL,
    decision      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (loop_id, n)
);
CREATE TABLE IF NOT EXISTS deployments (
    deploy_id   TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    environment TEXT NOT NULL,        -- paper | live
    config      TEXT NOT NULL,
    status      TEXT NOT NULL,        -- pending | running | paused | stopped
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studio_runs (
    run_id      TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version     INTEGER NOT NULL,
    is_draft    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,            -- running | done | failed
    metrics     TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);
"""


@dataclass
class StrategyMeta:
    strategy_id: str
    name: str
    latest_version: int
    updated_at: str


class StrategyStore:
    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = str(db_path)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    # ── API ──────────────────────────────────────────────────────
    def save(self, defn: StrategyDefinition) -> int:
        """Persist as a NEW version; returns the version number written."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            row = con.execute(
                "SELECT latest_version FROM strategy_meta WHERE strategy_id=?",
                (defn.id,),
            ).fetchone()
            version = (row["latest_version"] + 1) if row else 1
            doc = defn.model_copy(update={
                "version": version,
                "parent_version": row["latest_version"] if row else None,
            })
            con.execute(
                "INSERT INTO strategy_versions VALUES (?,?,?,?,?)",
                (defn.id, version, doc.model_dump_json(by_alias=True),
                 defn.origin, now),
            )
            con.execute(
                "INSERT INTO strategy_meta VALUES (?,?,?,?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET "
                "name=excluded.name, latest_version=excluded.latest_version, "
                "updated_at=excluded.updated_at",
                (defn.id, defn.name, version, now),
            )
        return version

    def load(self, strategy_id: str,
             version: int | None = None) -> StrategyDefinition:
        with self._connect() as con:
            if version is None:
                row = con.execute(
                    "SELECT json FROM strategy_versions WHERE strategy_id=? "
                    "ORDER BY version DESC LIMIT 1", (strategy_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT json FROM strategy_versions "
                    "WHERE strategy_id=? AND version=?",
                    (strategy_id, version)).fetchone()
        if row is None:
            raise KeyError(f"strategy '{strategy_id}' v{version} not found")
        return StrategyDefinition.model_validate(json.loads(row["json"]))

    def history(self, strategy_id: str) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT version, origin, created_at FROM strategy_versions "
                "WHERE strategy_id=? ORDER BY version DESC",
                (strategy_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_meta(self) -> list[StrategyMeta]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM strategy_meta ORDER BY updated_at DESC").fetchall()
        return [StrategyMeta(r["strategy_id"], r["name"],
                             r["latest_version"], r["updated_at"]) for r in rows]

    # ── drafts (Phase 2) ─────────────────────────────────────────
    def load_draft(self, strategy_id: str) -> StrategyDefinition | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT json FROM strategy_drafts WHERE strategy_id=?",
                (strategy_id,)).fetchone()
        if row is None:
            return None
        return StrategyDefinition.model_validate(json.loads(row["json"]))

    def save_draft(self, defn: StrategyDefinition) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO strategy_drafts VALUES (?,?,?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET "
                "json=excluded.json, updated_at=excluded.updated_at",
                (defn.id, defn.model_dump_json(by_alias=True), now))

    def delete_draft(self, strategy_id: str) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM strategy_drafts WHERE strategy_id=?",
                        (strategy_id,))

    def working_copy(self, strategy_id: str) -> tuple[StrategyDefinition, bool]:
        """Draft if present, else latest version. -> (defn, is_draft)."""
        draft = self.load_draft(strategy_id)
        if draft is not None:
            return draft, True
        return self.load(strategy_id), False

    def promote_draft(self, strategy_id: str,
                      origin: str = "user") -> int:
        """Persist the draft as a new version and clear it."""
        draft = self.load_draft(strategy_id)
        if draft is None:
            raise KeyError(f"no draft for '{strategy_id}'")
        draft = draft.model_copy(update={"origin": origin})
        version = self.save(draft)
        self.delete_draft(strategy_id)
        return version

    # ── runs (Phase 3) ───────────────────────────────────────────
    def create_run(self, run_id: str, strategy_id: str, version: int,
                   is_draft: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO studio_runs "
                "(run_id, strategy_id, version, is_draft, status, created_at) "
                "VALUES (?,?,?,?,'running',?)",
                (run_id, strategy_id, version, int(is_draft), now))

    def finish_run(self, run_id: str, metrics_json: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE studio_runs SET status='done', metrics=?, finished_at=? "
                "WHERE run_id=?", (metrics_json, now, run_id))

    def fail_run(self, run_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE studio_runs SET status='failed', error=?, finished_at=? "
                "WHERE run_id=?", (error, now, run_id))

    def latest_run(self, strategy_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM studio_runs WHERE strategy_id=? "
                "ORDER BY created_at DESC LIMIT 1", (strategy_id,)).fetchone()
        return dict(row) if row else None

    # ── optimize runs (Phase 4) ──────────────────────────────────
    def create_opt(self, run_id: str, strategy_id: str, version: int,
                   is_draft: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO optimize_runs "
                "(run_id, strategy_id, version, is_draft, status, created_at) "
                "VALUES (?,?,?,?,'running',?)",
                (run_id, strategy_id, version, int(is_draft), now))

    def finish_opt(self, run_id: str, results_json: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE optimize_runs SET status='done', results=?, "
                "finished_at=? WHERE run_id=?", (results_json, now, run_id))

    def fail_opt(self, run_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE optimize_runs SET status='failed', error=?, "
                "finished_at=? WHERE run_id=?", (error, now, run_id))

    def latest_opt(self, strategy_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM optimize_runs WHERE strategy_id=? "
                "ORDER BY created_at DESC LIMIT 1", (strategy_id,)).fetchone()
        return dict(row) if row else None


    # ── AI (Phase 5) ─────────────────────────────────────────────
    def add_suggestion(self, sid: str, strategy_id: str, block: str,
                       suggestion_json: str, status: str,
                       trial_metrics: str | None = None,
                       baseline: str | None = None,
                       note: str | None = None,
                       source: str = "manual") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO ai_suggestions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, strategy_id, block, suggestion_json, trial_metrics,
                 baseline, status, note, source, now))

    def get_suggestion(self, sid: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM ai_suggestions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def set_suggestion_status(self, sid: str, status: str,
                              note: str | None = None) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE ai_suggestions SET status=?, note=COALESCE(?, note) "
                "WHERE id=?", (status, note, sid))

    def pending_suggestions(self, strategy_id: str,
                            block: str | None = None) -> list[dict]:
        q = ("SELECT * FROM ai_suggestions WHERE strategy_id=? "
             "AND status='review'")
        args: tuple = (strategy_id,)
        if block:
            q += " AND block=?"
            args += (block,)
        with self._connect() as con:
            rows = con.execute(q + " ORDER BY created_at", args).fetchall()
        return [dict(r) for r in rows]

    def rejected_rationales(self, strategy_id: str, limit: int = 8) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT suggestion FROM ai_suggestions WHERE strategy_id=? "
                "AND status IN ('rejected','failed') "
                "ORDER BY created_at DESC LIMIT ?",
                (strategy_id, limit)).fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["suggestion"]).get("rationale", ""))
            except json.JSONDecodeError:
                pass
        return out

    def create_loop(self, loop_id: str, strategy_id: str,
                    config_json: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO ai_loops "
                "(loop_id, strategy_id, config, status, created_at) "
                "VALUES (?,?,?,'running',?)",
                (loop_id, strategy_id, config_json, now))

    def finish_loop(self, loop_id: str, status: str,
                    note: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "UPDATE ai_loops SET status=?, note=?, finished_at=? "
                "WHERE loop_id=?", (status, note, now, loop_id))

    def latest_loop(self, strategy_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM ai_loops WHERE strategy_id=? "
                "ORDER BY created_at DESC LIMIT 1", (strategy_id,)).fetchone()
        return dict(row) if row else None

    def add_iteration(self, loop_id: str, n: int, suggestion_id: str,
                      decision: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("INSERT INTO ai_iterations VALUES (?,?,?,?,?)",
                        (loop_id, n, suggestion_id, decision, now))

    def iterations(self, strategy_id: str, limit: int = 20) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT i.*, s.suggestion, s.trial_metrics, s.baseline, "
                "s.status AS s_status, s.note "
                "FROM ai_iterations i JOIN ai_suggestions s "
                "ON s.id = i.suggestion_id "
                "JOIN ai_loops l ON l.loop_id = i.loop_id "
                "WHERE l.strategy_id=? "
                "ORDER BY i.created_at DESC LIMIT ?",
                (strategy_id, limit)).fetchall()
        return [dict(r) for r in rows]


    # ── deployments (Phase 6) ────────────────────────────────────
    def create_deployment(self, deploy_id: str, strategy_id: str,
                          version: int, environment: str,
                          config_json: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                "INSERT INTO deployments VALUES (?,?,?,?,?,'pending',?)",
                (deploy_id, strategy_id, version, environment,
                 config_json, now))

    def latest_deployment(self, strategy_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM deployments WHERE strategy_id=? "
                "ORDER BY created_at DESC LIMIT 1", (strategy_id,)).fetchone()
        return dict(row) if row else None

    def set_deployment_status(self, deploy_id: str, status: str) -> None:
        with self._connect() as con:
            con.execute("UPDATE deployments SET status=? WHERE deploy_id=?",
                        (status, deploy_id))

    def get_deployment(self, deploy_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM deployments WHERE deploy_id=?",
                (deploy_id,)).fetchone()
        return dict(row) if row else None

    def list_deployments(self, strategy_id: str, limit: int = 10) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM deployments WHERE strategy_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (strategy_id, limit)).fetchall()
        return [dict(r) for r in rows]
