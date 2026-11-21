import json
from uuid import uuid4

from app.db import connect
from app.modules import null_to_value_open as nvo
from app.seed import now_iso


class ApplyConflict(Exception):
    """成员与现库状态不匹配（并发/重放/旧跑次）时失败闭。"""


class InjectedFault(Exception):
    """测试用半写故障注入。"""


def _fields(row):
    return {
        "open_time": row["open_time"],
        "close_time": row["close_time"],
        "weekday_mask": row["weekday_mask"],
        "biz_status": row["biz_status"],
    }


def _effective_hist(conn, store_id):
    rows = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1 "
        "ORDER BY valid_from DESC, id DESC",
        (store_id,),
    ).fetchall()
    if len(rows) > 1:
        raise ApplyConflict(f"{store_id} 存在多条现行历史段，拒绝开闭")
    return rows[0] if rows else None


def _load_members(conn, detect_run_id: str):
    return conn.execute(
        "SELECT * FROM detect_members WHERE detect_run_id=? ORDER BY id",
        (detect_run_id,),
    ).fetchall()


def apply_type2(detect_run_id: str, fault_after: int | None = None):
    """关旧开新 + 当前镜刷新 + 本 detect 成员写回，全部同一事务。

    任一步失败（含注入故障）→ 整笔回滚，三方不得撕裂。

    fault_after: 仅测试用。处理 N 个成员后主动抛错，验证回滚。
    """
    conn = connect(isolation_level=None)
    now = now_iso()
    in_tx = False
    try:
        run = conn.execute("SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)).fetchone()
        if not run:
            return {"ok": False, "message": "检测跑次不存在"}
        members = _load_members(conn, detect_run_id)
        if not members:
            return {
                "ok": False,
                "message": "该检测跑次无成员明细（旧跑次），请重新跑检测",
                "detect_run_id": detect_run_id,
            }
        pending = [m for m in members if m["status"] != "applied"]
        if not pending:
            return {"ok": True, "applied": [], "count": 0, "detect_run_id": detect_run_id, "message": "已全部应用"}

        conn.execute("BEGIN IMMEDIATE")
        in_tx = True
        applied = []
        for idx, member in enumerate(pending):
            store_id = member["store_id"]
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (member["snapshot_id"],)
            ).fetchone()
            if not snap:
                raise ApplyConflict(f"{store_id} 快照缺失：{member['snapshot_id']}")

            snap_fields = _fields(snap)
            # 同源派生：新段 hash 必须与检测 to_hash、快照字段三方一致。
            derived = nvo.derive_row_hash(snap_fields)
            if derived != member["to_hash"]:
                raise ApplyConflict(f"{store_id} to_hash 与快照字段不同源")

            cur_hist = _effective_hist(conn, store_id)
            mirror = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
            ).fetchone()

            if member["kind"] == nvo.KIND_NEW:
                if cur_hist is not None:
                    raise ApplyConflict(f"{store_id} 检测为 new 但已有现行段")
                if mirror is not None and mirror["hist_id"] != "":
                    existing = conn.execute(
                        "SELECT 1 FROM dim_store_hours_hist WHERE id=?", (mirror["hist_id"],)
                    ).fetchone()
                    if existing:
                        raise ApplyConflict(f"{store_id} 当前镜指向已存在的历史段")
                from_hist = None
            else:
                if cur_hist is None:
                    raise ApplyConflict(f"{store_id} 检测为变更但无现行段")
                if cur_hist["id"] != member["from_hist_id"]:
                    raise ApplyConflict(f"{store_id} 现行段已漂移，拒绝按旧检测开闭")
                if nvo.derive_row_hash(_fields(cur_hist)) != member["from_hash"]:
                    raise ApplyConflict(f"{store_id} 现行段 hash 与检测前像不符")
                if mirror is None or mirror["hist_id"] != cur_hist["id"]:
                    raise ApplyConflict(f"{store_id} 当前镜未指向现行段，拒绝开闭")
                from_hist = cur_hist

            valid_from = snap["as_of_ts"]
            if from_hist is not None:
                conn.execute(
                    """UPDATE dim_store_hours_hist
                       SET valid_to=?, is_current=0, closed_by_run=?
                       WHERE id=? AND is_current=1""",
                    (valid_from, detect_run_id, from_hist["id"]),
                )
                if conn.execute("SELECT changes() AS c").fetchone()["c"] != 1:
                    raise ApplyConflict(f"{store_id} 关旧段失败")

            new_id = f"HIST-{uuid4().hex[:10]}"
            reason = member["reason_code"]
            conn.execute(
                """INSERT INTO dim_store_hours_hist(
                  id, store_id, open_time, close_time, weekday_mask, biz_status,
                  valid_from, valid_to, is_current, source_snapshot_id, row_hash,
                  closed_by_run, change_reason, prev_hist_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id,
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    valid_from,
                    None,
                    1,
                    snap["id"],
                    derived,
                    None,
                    reason,
                    from_hist["id"] if from_hist else None,
                ),
            )
            conn.execute(
                """INSERT INTO dim_store_hours_current(
                  store_id, open_time, close_time, weekday_mask, biz_status,
                  hist_id, as_of_ts, row_hash
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(store_id) DO UPDATE SET
                  open_time=excluded.open_time,
                  close_time=excluded.close_time,
                  weekday_mask=excluded.weekday_mask,
                  biz_status=excluded.biz_status,
                  hist_id=excluded.hist_id,
                  as_of_ts=excluded.as_of_ts,
                  row_hash=excluded.row_hash
                """,
                (
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    new_id,
                    valid_from,
                    derived,
                ),
            )
            conn.execute(
                """UPDATE detect_members
                   SET status='applied', hist_id=?, applied_at=?, error=NULL
                   WHERE id=?""",
                (new_id, now, member["id"]),
            )
            applied.append(
                {
                    "store_id": store_id,
                    "kind": member["kind"],
                    "reason_code": reason,
                    "hist_id": new_id,
                    "closed": from_hist["id"] if from_hist else None,
                    "row_hash": derived,
                }
            )
            if fault_after is not None and len(applied) >= fault_after:
                raise InjectedFault(f"注入故障：已处理 {len(applied)} 个成员")

        # 事务内自检：受影响门店三方必须一致，否则回滚。
        tears = nvo.scan_tears(conn, sorted({a["store_id"] for a in applied}))
        if tears:
            raise ApplyConflict(f"事务内三方自检失败：{tears}")

        conn.commit()
        in_tx = False
        return {
            "ok": True,
            "applied": applied,
            "count": len(applied),
            "detect_run_id": detect_run_id,
        }
    except (ApplyConflict, InjectedFault) as exc:
        if in_tx:
            conn.rollback()
            in_tx = False
        _mark_error(conn, detect_run_id, str(exc))
        return {
            "ok": False,
            "rolled_back": True,
            "message": str(exc),
            "applied": [],
            "count": 0,
            "detect_run_id": detect_run_id,
        }
    except Exception as exc:  # 任何未预期异常也必须整笔回滚
        if in_tx:
            conn.rollback()
            in_tx = False
        _mark_error(conn, detect_run_id, f"未预期异常：{exc}")
        return {
            "ok": False,
            "rolled_back": True,
            "message": f"未预期异常：{exc}",
            "applied": [],
            "count": 0,
            "detect_run_id": detect_run_id,
        }
    finally:
        conn.close()


def _mark_error(conn, detect_run_id: str, message: str):
    """回滚后单独记录失败原因；不回写成员状态（成员保持 pending）。"""
    try:
        conn.execute(
            "UPDATE detect_runs SET apply_error=?, apply_rolled_back=1 WHERE id=?",
            (message, detect_run_id),
        )
    except Exception:
        pass


def rebuild_proofs():
    """Attest: each current mirror hist_id must point to is_current=1 hist with same hash."""
    conn = connect()
    now = now_iso()
    try:
        mirrors = conn.execute("SELECT * FROM dim_store_hours_current ORDER BY store_id").fetchall()
        results = []
        for m in mirrors:
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
            valid = 1
            detail = {}
            if not hist:
                valid = 0
                detail["error"] = "mirror_hist_missing"
            elif hist["is_current"] != 1:
                valid = 0
                detail["error"] = "mirror_not_current_hist"
            elif hist["row_hash"] != m["row_hash"]:
                valid = 0
                detail["error"] = "hash_mismatch"
            elif hist["store_id"] != m["store_id"]:
                valid = 0
                detail["error"] = "store_mismatch"
            elif hist["row_hash"] != nvo.derive_row_hash(_fields(hist)):
                valid = 0
                detail["error"] = "hist_hash_not_same_source"
            elif m["row_hash"] != nvo.derive_row_hash(_fields(m)):
                valid = 0
                detail["error"] = "mirror_hash_not_same_source"
            else:
                detail["ok"] = True
            conn.execute(
                """INSERT INTO scd_proofs(
                  store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    m["store_id"],
                    hist["id"] if hist else "",
                    m["hist_id"],
                    m["row_hash"],
                    valid,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            results.append({"store_id": m["store_id"], "valid": bool(valid), "detail": detail})
        conn.commit()
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()
