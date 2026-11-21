import os

from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import IMPLEMENTED, STUBS
from app.modules import null_to_value_open as nvo
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs/{run_id}/members")
def detect_members(run_id: str):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_members WHERE detect_run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/null-flip/proof")
def null_flip_proof(detect_run_id: str | None = None):
    """空值翻转核证（只读）：前后 hash、hist_id、detect 成员 kind。"""
    conn = connect()
    try:
        return nvo.build_proof(conn, detect_run_id)
    finally:
        conn.close()


@router.get("/null-flip/tears")
def null_flip_tears():
    conn = connect()
    try:
        tears = nvo.scan_tears(conn)
        return {"tear_count": len(tears), "tears": tears}
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    run_id = body.get("detect_run_id")
    if not run_id:
        raise HTTPException(400, "需要 detect_run_id")
    fault_after = body.get("fault_after")
    if fault_after is not None:
        # 故障注入仅测试环境开放，避免线上误用。
        if os.environ.get("HOURSPAN_TEST_HOOKS") != "1":
            raise HTTPException(403, "fault_after 仅在测试钩子开启时可用")
        try:
            fault_after = int(fault_after)
        except (TypeError, ValueError):
            raise HTTPException(400, "fault_after 须为整数")
    result = scd.apply_type2(run_id, fault_after=fault_after)
    if result.get("ok"):
        return result
    raise HTTPException(409, result)


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS, "implemented": IMPLEMENTED}

# --- plant routes ---
from app.modules import null_transition_audit as null_aud

@router.get("/null-transition/audit")
def null_transition_audit(store_id: str | None = None):
    conn = connect()
    try:
        return null_aud.field_rows(conn, store_id)
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

