import json
from uuid import uuid4

from app.db import connect
from app.modules import null_to_value_open as nvo
from app.seed import now_iso


def _snap_fields(snap):
    return {
        "open_time": snap["open_time"],
        "close_time": snap["close_time"],
        "weekday_mask": snap["weekday_mask"],
        "biz_status": snap["biz_status"],
    }


def _hist_fields(hist):
    return {
        "open_time": hist["open_time"],
        "close_time": hist["close_time"],
        "weekday_mask": hist["weekday_mask"],
        "biz_status": hist["biz_status"],
    }


def _effective_hist(conn, store_id):
    return conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1 "
        "ORDER BY valid_from DESC, id DESC LIMIT 1",
        (store_id,),
    ).fetchone()


def run_detect(batch_id: str | None = None):
    """Compare latest snapshots (optionally one batch) vs effective hist row.

    以现行有效历史段（is_current=1）为基准，to_hash 与历史维/当前镜同源派生；
    空↔非空翻转单独归为 null_fill / null_clear 成员，绝不跳过。
    """
    conn = connect()
    run_id = f"DET-{uuid4().hex[:8]}"
    now = now_iso()
    try:
        if batch_id:
            snaps = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY store_id, as_of_ts",
                (batch_id,),
            ).fetchall()
        else:
            snaps = conn.execute(
                """
                SELECT s.* FROM source_snapshots s
                JOIN (
                  SELECT store_id, MAX(as_of_ts) AS mx FROM source_snapshots GROUP BY store_id
                ) t ON s.store_id=t.store_id AND s.as_of_ts=t.mx
                ORDER BY s.store_id
                """
            ).fetchall()

        changes = []
        # 同店同批多次快照：以最后一条为准，避免同店重复开段。
        latest_by_store: dict[str, dict] = {}
        for snap in snaps:
            latest_by_store[snap["store_id"]] = snap

        for store_id, snap in sorted(latest_by_store.items()):
            cur_hist = _effective_hist(conn, store_id)
            to_fields = _snap_fields(snap)
            # 同源派生：检测侧不信任贴源 hash，统一从字段重算。
            to_hash = nvo.derive_row_hash(to_fields)
            if cur_hist is None:
                change = {
                    "store_id": store_id,
                    "kind": nvo.KIND_NEW,
                    "reason_code": None,
                    "flipped_fields": [],
                    "snapshot_id": snap["id"],
                    "from_hash": None,
                    "to_hash": to_hash,
                    "from_hist_id": None,
                    "from": None,
                    "to": to_fields,
                }
            else:
                from_fields = _hist_fields(cur_hist)
                from_hash = nvo.derive_row_hash(from_fields)
                if from_hash == to_hash:
                    continue
                cls = nvo.classify(from_fields, to_fields)
                change = {
                    "store_id": store_id,
                    "kind": cls["kind"],
                    "reason_code": cls["reason_code"],
                    "flipped_fields": cls["flipped_fields"],
                    "snapshot_id": snap["id"],
                    "from_hash": from_hash,
                    "to_hash": to_hash,
                    "from_hist_id": cur_hist["id"],
                    "from": from_fields,
                    "to": to_fields,
                }
            changes.append(change)

        report = {"changes": changes, "scanned": len(snaps)}
        conn.execute(
            """INSERT INTO detect_runs(id, batch_id, created_at, changed_count, report_json)
               VALUES (?,?,?,?,?)""",
            (
                run_id,
                batch_id,
                now,
                len(changes),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        for ch in changes:
            conn.execute(
                """INSERT INTO detect_members(
                  detect_run_id, store_id, snapshot_id, kind, reason_code,
                  flipped_fields, from_hash, to_hash, from_hist_id, hist_id,
                  status, applied_at, error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    ch["store_id"],
                    ch["snapshot_id"],
                    ch["kind"],
                    ch["reason_code"],
                    json.dumps(ch["flipped_fields"], ensure_ascii=False),
                    ch["from_hash"],
                    ch["to_hash"],
                    ch["from_hist_id"],
                    None,
                    "pending",
                    None,
                    None,
                ),
            )
        conn.commit()
        return {
            "detect_run_id": run_id,
            "changed_count": len(changes),
            "null_flip_count": sum(
                1 for c in changes if c["kind"] in (nvo.KIND_NULL_FILL, nvo.KIND_NULL_CLEAR)
            ),
            "changes": changes,
        }
    finally:
        conn.close()
