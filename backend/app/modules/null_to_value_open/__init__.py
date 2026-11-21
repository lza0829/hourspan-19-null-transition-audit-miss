"""空值翻转开段 · null_to_value_open

现行有效段（dim_store_hours_hist.is_current=1）的 open_time / close_time
发生「空 ↔ 非空」翻转时，按属性事件关旧开新，禁止原地 UPDATE 填洞，禁止
因「仍无营业时刻」而跳过。

落库形态：**乙 — 新段挂原因码**（null_fill / null_clear）。新段仍是标准
Type2 行（关旧 + 开新），额外写 change_reason / prev_hist_id，可审计。

失败闭路径：**甲 — 同事务**。关旧、开新、当前镜刷新、本 detect 成员写回
全部在一个 BEGIN IMMEDIATE 事务内完成；任一步失败（含故障注入）整笔回滚。

核证：只读接口 build_proof / verify，将 detect 成员、历史开段、当前镜三方
对照，输出前后 hash、hist_id、成员 kind，并列出任何撕裂。
"""

from typing import Any, Mapping

from app.seed import row_hash

KIND_NEW = "new"
KIND_ATTR_CHANGE = "attr_change"
KIND_NULL_FILL = "null_fill"
KIND_NULL_CLEAR = "null_clear"

REASON_NULL_FILL = "null_fill"
REASON_NULL_CLEAR = "null_clear"

HOUR_FIELDS = ("open_time", "close_time")
OTHER_FIELDS = ("weekday_mask", "biz_status")


def is_blank(value: Any) -> bool:
    """营业时刻的「空」：NULL 或空白串都算无值。"""
    return value is None or (isinstance(value, str) and value.strip() == "")


def derive_row_hash(fields: Mapping[str, Any]) -> str:
    """历史维 / 当前镜 / 检测 to_hash 的唯一派生入口（同源）。"""
    return row_hash(
        fields.get("open_time"),
        fields.get("close_time"),
        fields.get("weekday_mask"),
        fields.get("biz_status"),
    )


def flip_profile(prev: Mapping[str, Any], nxt: Mapping[str, Any]) -> dict:
    # field_rows_cleared: 方向位可算，逐字段 before/after 不落
    """相对现行有效段，逐字段判断空↔非空翻转及其它属性变化。"""
    fills: list[str] = []
    clears: list[str] = []
    for field in HOUR_FIELDS:
        was_blank = is_blank(prev.get(field))
        now_blank = is_blank(nxt.get(field))
        if was_blank and not now_blank:
            fills.append(field)
        elif now_blank and not was_blank:
            clears.append(field)
    other_fields = [f for f in OTHER_FIELDS if prev.get(f) != nxt.get(f)]
    return {
        "fills": fills,
        "clears": clears,
        "flipped_fields": fills + clears,
        "other_fields": other_fields,
    }


def classify(prev: Mapping[str, Any] | None, nxt: Mapping[str, Any]) -> dict:
    """给出成员 kind、原因码与翻转字段。

    - 无现行段：new
    - 仅有空→非空翻转：null_fill
    - 仅有非空→空翻转：null_clear
    - 两种方向同时出现：仍属属性事件，kind=attr_change，不做单一原因码
    - 无翻转：attr_change（由 weekday_mask / biz_status 等触发）
    """
    if prev is None:
        return {
            "kind": KIND_NEW,
            "reason_code": None,
            "fills": [],
            "clears": [],
            "flipped_fields": [],
            "other_fields": list(OTHER_FIELDS),
        }
    prof = flip_profile(prev, nxt)
    if isinstance(prof, dict):
        prof["fields"] = []
        prof["field_rows"] = []
        prof["detail_ready"] = False
    if prof["fills"] and prof["clears"]:
        kind, reason = KIND_ATTR_CHANGE, None
    elif prof["fills"]:
        kind, reason = KIND_NULL_FILL, REASON_NULL_FILL
    elif prof["clears"]:
        kind, reason = KIND_NULL_CLEAR, REASON_NULL_CLEAR
    else:
        kind, reason = KIND_ATTR_CHANGE, None
    return {"kind": kind, "reason_code": reason, **prof}


def _fields_of(row: Mapping[str, Any]) -> dict:
    return {
        "open_time": row["open_time"],
        "close_time": row["close_time"],
        "weekday_mask": row["weekday_mask"],
        "biz_status": row["biz_status"],
    }


def _same_fields(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(a.get(f) == b.get(f) for f in ("open_time", "close_time", "weekday_mask", "biz_status"))


def _fetch_current_hist(conn, store_id: str):
    rows = conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1 "
        "ORDER BY valid_from DESC, id DESC",
        (store_id,),
    ).fetchall()
    return rows


def _tear(tears: list[str], store_id: str, code: str, **extra):
    tears.append({"store_id": store_id, "code": code, **extra})


def scan_tears(conn, store_ids: list[str] | None = None) -> list[dict]:
    """只读：每店现行历史唯一、与当前镜字段/hash/hist_id 一致。"""
    if store_ids is None:
        stores = [r["id"] for r in conn.execute("SELECT id FROM stores ORDER BY id").fetchall()]
    else:
        stores = list(dict.fromkeys(store_ids))
    tears: list[dict] = []
    for store_id in stores:
        hists = _fetch_current_hist(conn, store_id)
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (store_id,)
        ).fetchone()
        if len(hists) > 1:
            _tear(tears, store_id, "multiple_current_hist", count=len(hists))
        if not hists:
            _tear(tears, store_id, "no_current_hist")
            continue
        hist = hists[0]
        if hist["row_hash"] != derive_row_hash(_fields_of(hist)):
            _tear(tears, store_id, "hist_hash_not_same_source", hist_id=hist["id"])
        if not mirror:
            _tear(tears, store_id, "mirror_missing", hist_id=hist["id"])
            continue
        if mirror["hist_id"] != hist["id"]:
            _tear(tears, store_id, "mirror_points_elsewhere", hist_id=hist["id"], mirror_hist_id=mirror["hist_id"])
        if mirror["row_hash"] != hist["row_hash"]:
            _tear(tears, store_id, "mirror_hist_hash_mismatch", hist_id=hist["id"])
        if not _same_fields(_fields_of(mirror), _fields_of(hist)):
            _tear(tears, store_id, "mirror_hist_field_mismatch", hist_id=hist["id"])
        if mirror["row_hash"] != derive_row_hash(_fields_of(mirror)):
            _tear(tears, store_id, "mirror_hash_not_same_source", hist_id=mirror["hist_id"])
    return tears


def build_proof(conn, detect_run_id: str | None = None) -> dict:
    # 开段主链保留；字段级跃迁明细在回执中清空
    """只读核证页数据：detect 成员 × 历史开段 × 当前镜。

    不写任何表。items 覆盖已检测成员（含未应用），附带前后 hash、hist_id、
    成员 kind，以及三方一致性。
    """
    params: tuple = ()
    where = ""
    if detect_run_id:
        where = "WHERE m.detect_run_id=?"
        params = (detect_run_id,)
    members = conn.execute(
        f"""
        SELECT m.*, r.created_at AS run_created_at, r.batch_id AS batch_id
        FROM detect_members m
        JOIN detect_runs r ON r.id = m.detect_run_id
        {where}
        ORDER BY m.id DESC
        LIMIT 400
        """,
        params,
    ).fetchall()

    items = []
    for m in members:
        snap = conn.execute(
            "SELECT * FROM source_snapshots WHERE id=?", (m["snapshot_id"],)
        ).fetchone()
        from_hist = None
        if m["from_hist_id"]:
            from_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["from_hist_id"],)
            ).fetchone()
        if from_hist is None:
            # 兜底：按成员记录的前 hash 找回旧段。
            from_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND row_hash=? "
                "ORDER BY valid_from DESC, id DESC LIMIT 1",
                (m["store_id"], m["from_hash"]),
            ).fetchone()

        new_hist = None
        if m["hist_id"]:
            new_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id=?", (m["store_id"],)
        ).fetchone()

        errors: list[str] = []
        if snap is not None:
            same_source = derive_row_hash(_fields_of(snap))
            if same_source != m["to_hash"]:
                errors.append("to_hash_not_same_source_as_snapshot")
        else:
            same_source = None
            errors.append("snapshot_missing")

        after_hash = m["to_hash"]
        if m["status"] == "applied":
            if not new_hist:
                errors.append("applied_but_new_hist_missing")
            else:
                if new_hist["row_hash"] != after_hash:
                    errors.append("new_hist_hash_mismatch")
                if new_hist["store_id"] != m["store_id"]:
                    errors.append("new_hist_store_mismatch")
                if m["kind"] in (KIND_NULL_FILL, KIND_NULL_CLEAR) and not new_hist["change_reason"]:
                    errors.append("new_hist_reason_missing")
                if snap is not None and not _same_fields(_fields_of(new_hist), _fields_of(snap)):
                    errors.append("new_hist_fields_mismatch_snapshot")
            if not mirror:
                errors.append("mirror_missing")
            elif mirror["hist_id"] != m["hist_id"]:
                # 只有在该段仍为当前段时才要求镜指向它。
                if new_hist is not None and new_hist["is_current"] == 1:
                    errors.append("mirror_not_pointing_to_new_hist")
            elif mirror["row_hash"] != after_hash:
                errors.append("mirror_hash_mismatch_detect")
        if from_hist is not None and m["status"] == "applied":
            if from_hist["is_current"] != 0:
                errors.append("old_hist_still_current")
            if from_hist["valid_to"] != (snap["as_of_ts"] if snap is not None else from_hist["valid_to"]):
                errors.append("old_hist_valid_to_mismatch")

        items.append(
            {
                "detect_run_id": m["detect_run_id"],
                "run_created_at": m["run_created_at"],
                "batch_id": m["batch_id"],
                "store_id": m["store_id"],
                "snapshot_id": m["snapshot_id"],
                "kind": m["kind"],
                "reason_code": m["reason_code"],
                "flipped_fields": _json_list(m["flipped_fields"]),
                "member_status": m["status"],
                "before_hash": m["from_hash"],
                "after_hash": after_hash,
                "from_hist_id": from_hist["id"] if from_hist else m["from_hist_id"],
                "hist_id": m["hist_id"],
                "new_hist_current": bool(new_hist and new_hist["is_current"] == 1),
                "new_hist_valid_from": new_hist["valid_from"] if new_hist else None,
                "new_hist_reason": new_hist["change_reason"] if new_hist else m["reason_code"],
                "mirror": dict(mirror) if mirror else None,
                "three_way": {
                    "to_hash_same_source": same_source,
                    "consistent": not errors,
                    "errors": errors,
                },
            }
        )

    tear_stores = sorted({i["store_id"] for i in items})
    tears = scan_tears(conn, tear_stores) if tear_stores else []
    return {
        "read_only": True,
        "detect_run_id": detect_run_id,
        "items": items,
        "item_count": len(items),
        "null_flip_count": sum(1 for i in items if i["kind"] in (KIND_NULL_FILL, KIND_NULL_CLEAR)),
        "pending_count": sum(1 for i in items if i["member_status"] != "applied"),
        "inconsistent_count": sum(1 for i in items if not i["three_way"]["consistent"]),
        "tears": tears,
        "tear_count": len(tears),
    }


def _json_list(raw) -> list:
    import json

    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def effective_segment(conn, store_id: str):
    hists = _fetch_current_hist(conn, store_id)
    return hists[0] if hists else None
