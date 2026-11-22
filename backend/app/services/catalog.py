from app.db import connect


def overview(conn):
    return {
        "门店数": conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"],
        "快照数": conn.execute("SELECT COUNT(*) AS c FROM source_snapshots").fetchone()["c"],
        "历史行": conn.execute("SELECT COUNT(*) AS c FROM dim_store_hours_hist").fetchone()["c"],
        "当前镜": conn.execute("SELECT COUNT(*) AS c FROM dim_store_hours_current").fetchone()["c"],
        "当前有效历史": conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE is_current=1"
        ).fetchone()["c"],
        "入档批": conn.execute("SELECT COUNT(*) AS c FROM ingest_batches").fetchone()["c"],
        "检测跑次": conn.execute("SELECT COUNT(*) AS c FROM detect_runs").fetchone()["c"],
        "核证条": conn.execute("SELECT COUNT(*) AS c FROM scd_proofs").fetchone()["c"],
        "跃迁记录": conn.execute("SELECT COUNT(*) AS c FROM null_transition_audit").fetchone()["c"],
        "搁置": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
    }


def store_board(conn):
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.region,
               c.open_time, c.close_time, c.weekday_mask, c.biz_status,
               c.hist_id, c.as_of_ts, c.row_hash,
               (SELECT COUNT(*) FROM dim_store_hours_hist h WHERE h.store_id=s.id) AS hist_count
        FROM stores s
        LEFT JOIN dim_store_hours_current c ON c.store_id=s.id
        ORDER BY s.region, s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]

