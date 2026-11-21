import hashlib
import json
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  region TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_snapshots (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  store_id TEXT NOT NULL REFERENCES stores(id),
  as_of_ts TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL DEFAULT '1111100',
  biz_status TEXT NOT NULL DEFAULT 'open',
  payload_json TEXT NOT NULL DEFAULT '{}',
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detect_runs (
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  created_at TEXT NOT NULL,
  changed_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}',
  apply_error TEXT,
  apply_rolled_back INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS detect_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detect_run_id TEXT NOT NULL REFERENCES detect_runs(id),
  store_id TEXT NOT NULL REFERENCES stores(id),
  snapshot_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  reason_code TEXT,
  flipped_fields TEXT NOT NULL DEFAULT '[]',
  from_hash TEXT,
  to_hash TEXT NOT NULL,
  from_hist_id TEXT,
  hist_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  applied_at TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS dim_store_hours_hist (
  id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  source_snapshot_id TEXT,
  row_hash TEXT NOT NULL,
  closed_by_run TEXT,
  change_reason TEXT,
  prev_hist_id TEXT
);

CREATE TABLE IF NOT EXISTS dim_store_hours_current (
  store_id TEXT PRIMARY KEY REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  as_of_ts TEXT NOT NULL,
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scd_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  mirror_hist_id TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  valid INTEGER NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hist_store_current
  ON dim_store_hours_hist(store_id, is_current);
CREATE INDEX IF NOT EXISTS idx_snap_store_ts
  ON source_snapshots(store_id, as_of_ts);
CREATE INDEX IF NOT EXISTS idx_members_run
  ON detect_members(detect_run_id, store_id);
"""

# 旧库补列：CREATE TABLE IF NOT EXISTS 不会给既有表加列。
_HIST_EXTRA_COLUMNS = (
    ("change_reason", "TEXT"),
    ("prev_hist_id", "TEXT"),
)


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(dim_store_hours_hist)").fetchall()}
    for name, decl in _HIST_EXTRA_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE dim_store_hours_hist ADD COLUMN {name} {decl}")
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(detect_runs)").fetchall()}
    for name, decl in (
        ("apply_error", "TEXT"),
        ("apply_rolled_back", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in run_cols:
            conn.execute(f"ALTER TABLE detect_runs ADD COLUMN {name} {decl}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_hash(open_time, close_time, weekday_mask, biz_status) -> str:
    raw = f"{open_time or ''}|{close_time or ''}|{weekday_mask}|{biz_status}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    if not conn.execute("SELECT 1 FROM settings WHERE key='as_of_mode'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('as_of_mode', 'business_ts')")
    if conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"] == 0:
        _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn):
    path = FIXTURES_DIR / "seed_stores.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    batch_id = "SEED-BATCH"
    created = now_iso()
    conn.execute(
        "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
        (batch_id, created, json.dumps({"seed": True}, ensure_ascii=False)),
    )
    for s in data.get("stores", []):
        conn.execute(
            "INSERT INTO stores(id, name, region) VALUES (?,?,?)",
            (s["id"], s["name"], s["region"]),
        )
        ot, ct = s.get("open_time"), s.get("close_time")
        wm = s.get("weekday_mask", "1111100")
        st = s.get("biz_status", "open")
        rh = row_hash(ot, ct, wm, st)
        snap_id = f"SNAP-SEED-{s['id']}"
        as_of = s.get("as_of_ts", "2026-11-01T00:00:00Z")
        conn.execute(
            """INSERT INTO source_snapshots(
              id, batch_id, store_id, as_of_ts, open_time, close_time,
              weekday_mask, biz_status, payload_json, row_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snap_id,
                batch_id,
                s["id"],
                as_of,
                ot,
                ct,
                wm,
                st,
                json.dumps(s.get("payload", {}), ensure_ascii=False),
                rh,
            ),
        )
        hist_id = f"HIST-SEED-{s['id']}"
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hist_id, s["id"], ot, ct, wm, st, as_of, None, 1, snap_id, rh, None),
        )
        conn.execute(
            """INSERT INTO dim_store_hours_current(
              store_id, open_time, close_time, weekday_mask, biz_status,
              hist_id, as_of_ts, row_hash
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (s["id"], ot, ct, wm, st, hist_id, as_of, rh),
        )
        conn.execute(
            """INSERT INTO scd_proofs(
              store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                s["id"],
                hist_id,
                hist_id,
                rh,
                1,
                json.dumps({"seed": True}, ensure_ascii=False),
                created,
            ),
        )
