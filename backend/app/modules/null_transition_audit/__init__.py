"""空值跃迁字段级审计 · null_transition_audit

架构口径（全局唯一）：**甲 — 以字段级 before/after 空位为准**。
每个发生空↔非空跃迁的可空字段（open_time / close_time）落一行：
store_id、字段名、旧是否空、新是否空、旧 hist、新 hist、as_of。
不采用「方向汇总位 + 字段名列表」的乙口径 —— 逐字段 before/after 不可省略。

核证口径：已应用开段涉及的每个跃迁字段都必须有且仅有一行记录，
缺字段行即核证失败；记录方向（空位）必须与新旧段实字段一致，
记录不得挂到别店/别段（邻键串改）。

写路径：仅 app.services.scd.apply_type2 在关旧开新的同一事务内 append，
半截失败随主事务整笔回滚 —— 禁止「正式已开、跃迁记录空」。
本模块对路由只暴露只读核证，不提供任何写接口。
"""

import json

from app.modules import null_to_value_open as nvo

STATUS = "implemented"

# 可空（会发生空↔非空跃迁）的营业时刻字段
NULLABLE_FIELDS = list(nvo.HOUR_FIELDS)

# 违例码
MISSING_FIELD_ROW = "missing_field_row"  # 应记字段缺行（甲口径核心：核证失败）
EXTRA_FIELD_ROW = "unexpected_field_row"  # 段上多记了未跃迁字段
DUPLICATE_FIELD_ROW = "duplicate_field_row"  # 同段同字段多行
DIRECTION_MISMATCH = "direction_mismatch"  # 记录空位与实字段分叉
ADJACENT_KEY_TAMPERED = "adjacent_key_tampered"  # 记录挂到别店（邻键串改）
TO_HIST_MISSING = "to_hist_missing"
FROM_HIST_MISSING = "from_hist_missing"
AS_OF_MISMATCH = "as_of_mismatch"
UNKNOWN_FIELD = "unknown_field"
NOT_A_TRANSITION = "not_a_transition"


def _json_list(raw) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _hist_fields(row) -> dict:
    return {
        "open_time": row["open_time"],
        "close_time": row["close_time"],
        "weekday_mask": row["weekday_mask"],
        "biz_status": row["biz_status"],
    }


def _hist(conn, hist_id):
    if not hist_id:
        return None
    return conn.execute(
        "SELECT * FROM dim_store_hours_hist WHERE id=?", (hist_id,)
    ).fetchone()


def expected_field_rows(from_fields: dict, to_fields: dict) -> list[dict]:
    """按实字段计算应落的字段级跃迁行：[{field, old_is_null, new_is_null}]。"""
    return nvo.flip_profile(from_fields, to_fields)["field_rows"]


def record_transitions(
    conn,
    *,
    store_id: str,
    field_rows: list[dict],
    from_hist_id: str,
    to_hist_id: str,
    detect_run_id: str,
    member_id: int,
    as_of: str,
    created_at: str,
) -> int:
    """开段同事务内追加字段级跃迁记录（append-only，随主事务同成败）。

    任何一行插入失败（约束/外键/唯一索引）都抛给主事务整笔回滚。
    """
    for r in field_rows:
        conn.execute(
            """INSERT INTO null_transition_audit(
              store_id, field_name, old_is_null, new_is_null,
              from_hist_id, to_hist_id, detect_run_id, member_id, as_of, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                store_id,
                r["field"],
                1 if r["old_is_null"] else 0,
                1 if r["new_is_null"] else 0,
                from_hist_id,
                to_hist_id,
                detect_run_id,
                member_id,
                as_of,
                created_at,
            ),
        )
    return len(field_rows)


def check_row(conn, row) -> list[dict]:
    """单行核验：方向不得与实字段分叉，邻键不得串改，as_of 与新段一致。"""
    errors = []
    field = row["field_name"]
    if field not in NULLABLE_FIELDS:
        return [{"code": UNKNOWN_FIELD, "detail": f"非可空跃迁字段：{field}"}]
    if bool(row["old_is_null"]) == bool(row["new_is_null"]):
        errors.append({"code": NOT_A_TRANSITION, "detail": "新旧空位相同，非跃迁"})

    to_hist = _hist(conn, row["to_hist_id"])
    if to_hist is None:
        errors.append({"code": TO_HIST_MISSING, "detail": row["to_hist_id"]})
    else:
        if to_hist["store_id"] != row["store_id"]:
            errors.append(
                {
                    "code": ADJACENT_KEY_TAMPERED,
                    "detail": f"记录属 {row['store_id']}，新段 {row['to_hist_id']} 属 {to_hist['store_id']}",
                }
            )
        elif bool(row["new_is_null"]) != nvo.is_blank(to_hist[field]):
            errors.append(
                {
                    "code": DIRECTION_MISMATCH,
                    "detail": f"新空位记录={row['new_is_null']}，新段实字段空={nvo.is_blank(to_hist[field])}",
                }
            )
        if row["as_of"] != to_hist["valid_from"]:
            errors.append(
                {
                    "code": AS_OF_MISMATCH,
                    "detail": f"记录 as_of={row['as_of']}，新段 valid_from={to_hist['valid_from']}",
                }
            )

    from_hist = _hist(conn, row["from_hist_id"])
    if from_hist is None:
        errors.append({"code": FROM_HIST_MISSING, "detail": row["from_hist_id"] or "空"})
    else:
        if from_hist["store_id"] != row["store_id"]:
            errors.append(
                {
                    "code": ADJACENT_KEY_TAMPERED,
                    "detail": f"记录属 {row['store_id']}，旧段 {row['from_hist_id']} 属 {from_hist['store_id']}",
                }
            )
        elif bool(row["old_is_null"]) != nvo.is_blank(from_hist[field]):
            errors.append(
                {
                    "code": DIRECTION_MISMATCH,
                    "detail": f"旧空位记录={row['old_is_null']}，旧段实字段空={nvo.is_blank(from_hist[field])}",
                }
            )
    return errors


def verify_member(conn, member, include_row_checks: bool = True):
    """核验单个 detect 成员的字段级跃迁记录（甲口径）。

    仅 status='applied' 且 flipped_fields 非空的成员需要记录；其余返回 ([], [])。
    返回 (rows, errors)：rows 为该成员新段上的跃迁记录。
    """
    flips = _json_list(member["flipped_fields"])
    if member["status"] != "applied" or not flips:
        return [], []

    rows = conn.execute(
        "SELECT * FROM null_transition_audit WHERE to_hist_id=? ORDER BY field_name, id",
        (member["hist_id"],),
    ).fetchall()
    errors: list[dict] = []
    base = {"store_id": member["store_id"], "hist_id": member["hist_id"]}

    by_field: dict[str, object] = {}
    for r in rows:
        if r["field_name"] in by_field:
            errors.append({**base, "code": DUPLICATE_FIELD_ROW, "field": r["field_name"]})
        else:
            by_field[r["field_name"]] = r

    # 期望字段：以库内新旧段实字段重算为准；段缺失时退回成员自报名单并记违例。
    new_hist = _hist(conn, member["hist_id"])
    from_hist = _hist(conn, member["from_hist_id"])
    if new_hist is not None and from_hist is not None:
        expected = [r["field"] for r in expected_field_rows(_hist_fields(from_hist), _hist_fields(new_hist))]
    else:
        expected = list(flips)
        code = TO_HIST_MISSING if new_hist is None else FROM_HIST_MISSING
        errors.append({**base, "code": code, "detail": "成员已应用但段缺失"})

    for fname in expected:
        if fname not in by_field:
            # 甲口径：每个跃迁字段一行，缺字段行即核证失败。
            errors.append({**base, "code": MISSING_FIELD_ROW, "field": fname})
    for fname in by_field:
        if fname not in expected:
            errors.append({**base, "code": EXTRA_FIELD_ROW, "field": fname})

    if include_row_checks:
        for r in rows:
            for e in check_row(conn, r):
                errors.append(
                    {**e, "store_id": r["store_id"], "field": r["field_name"], "hist_id": r["to_hist_id"]}
                )
    return rows, errors


def field_rows(conn, store_id: str | None = None) -> dict:
    """只读核证：字段级跃迁记录 × 新旧段实字段 × detect 成员 四方对照（甲口径）。

    不写任何表。缺字段行 / 方向分叉 / 邻键串改 / as_of 不符，任一违例 valid=False。
    """
    q = "SELECT * FROM null_transition_audit"
    args: list = []
    if store_id:
        q += " WHERE store_id=?"
        args.append(store_id)
    q += " ORDER BY store_id, id"
    rows = conn.execute(q, args).fetchall()

    transitions = []
    violations: list[dict] = []
    for r in rows:
        errs = check_row(conn, r)
        transitions.append(
            {**dict(r), "valid": not errs, "errors": [e["code"] for e in errs]}
        )
        for e in errs:
            violations.append(
                {
                    **e,
                    "store_id": r["store_id"],
                    "field": r["field_name"],
                    "to_hist_id": r["to_hist_id"],
                }
            )

    # 覆盖核验：已应用且含跃迁字段的成员，逐字段行缺一不可（甲口径）。
    mq = "SELECT * FROM detect_members WHERE status='applied' AND flipped_fields != '[]'"
    margs: list = []
    if store_id:
        mq += " AND store_id=?"
        margs.append(store_id)
    members = conn.execute(mq, margs).fetchall()
    checked = 0
    for m in members:
        if not _json_list(m["flipped_fields"]):
            continue
        checked += 1
        _, errs = verify_member(conn, m, include_row_checks=False)
        for e in errs:
            violations.append(
                {**e, "member_id": m["id"], "detect_run_id": m["detect_run_id"]}
            )

    current_n = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE is_current=1"
    ).fetchone()["c"]
    return {
        "read_only": True,
        "detail_ready": True,
        "granularity": "field_before_after",
        "fields_tracked": NULLABLE_FIELDS,
        "current_n": current_n,
        "transition_count": len(transitions),
        "transitions": transitions,
        "checked_members": checked,
        "violation_count": len(violations),
        "violations": violations,
        "valid": not violations,
    }
