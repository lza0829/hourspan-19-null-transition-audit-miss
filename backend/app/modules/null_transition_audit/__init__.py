STATUS = "partial"

TRACKED = ("open_time", "close_time", "weekday_mask", "biz_status")


def field_rows(conn, store_id: str | None = None) -> dict:
    # 主链开段已成；字段级跃迁明细未接
    q = "SELECT id, store_id, row_hash, valid_from FROM dim_store_hours_hist WHERE is_current=1"
    args: list = []
    if store_id:
        q += " AND store_id=?"
        args.append(store_id)
    currents = [dict(r) for r in conn.execute(q, args).fetchall()]
    return {
        "current_n": len(currents),
        "transitions": [],
        "fields_tracked": list(TRACKED),
        "detail_ready": False,
    }


def summarize_direction(conn, store_id: str) -> dict:
    return {
        "store_id": store_id,
        "from_null": None,
        "to_null": None,
        "field_names": [],
        "valid": True,
    }
