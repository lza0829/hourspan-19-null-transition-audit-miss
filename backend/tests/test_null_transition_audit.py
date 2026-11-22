"""字段级空值跃迁审计测试（null_transition_audit · 架构甲）。

口径：每个发生空↔非空跃迁的可空字段落一行（store_id、字段名、旧是否空、
新是否空、旧 hist、新 hist、as_of），与 Type2 开段同事务同成败；
核证以字段级 before/after 空位为准 —— 缺字段行即核证失败。

覆盖：
1. 夹具 null→值 一次：跃迁记录随开段落库，字段级内容与新段实字段一致。
2. 半写故障：整笔回滚，跃迁记录与段同生共死（禁止正式已开、记录空）。
3. 再造缺字段行：甲口径核证失败，核证页同步显形。
4. 记录方向与实字段分叉：核证失败。
5. 邻键记录被串改：核证失败。
6. 核证接口只读。
7. 双向同现（attr_change）：逐字段各落一行。
"""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "fixtures"))
    monkeypatch.setenv("HOURSPAN_TEST_HOOKS", "1")
    shutil.copytree(FIXTURES, tmp_path / "fixtures")

    # 重新导入，确保 db 路径按新环境变量解析。
    import importlib
    import app.db as db
    import app.seed as seed
    import app.services.detect as detect
    import app.services.ingest as ingest
    import app.services.scd as scd
    import app.modules.null_to_value_open as nvo
    import app.modules.null_transition_audit as null_aud

    for mod in (db, seed, detect, ingest, scd, nvo, null_aud):
        importlib.reload(mod)

    import app.routers.core as core
    importlib.reload(core)
    import app.main as main_mod
    importlib.reload(main_mod)

    seed.init_db()
    with TestClient(main_mod.app) as c:
        yield c


def _run_flow(client, fixture="null_flip_batch.json"):
    r = client.post("/api/ingest/run", json={"fixture": fixture})
    assert r.status_code == 200, r.text
    batch = r.json()["batch_id"]
    r = client.post("/api/detect/run", json={"batch_id": batch})
    assert r.status_code == 200, r.text
    det = r.json()
    applied = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"]})
    return batch, det, applied


def _audit(client, store_id=None):
    params = {"store_id": store_id} if store_id else {}
    r = client.get("/api/null-transition/audit", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _db_query(sql, args=()):
    import app.db as db

    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _db_exec(sql, args=()):
    import app.db as db

    conn = db.connect()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def test_field_rows_written_with_segment(client):
    """夹具 null→值 一次（另含 value→null）：字段级跃迁记录随开段同库。"""
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True, applied.text

    audit = _audit(client)
    assert audit["read_only"] is True
    assert audit["detail_ready"] is True
    assert audit["granularity"] == "field_before_after"
    assert audit["valid"] is True, audit["violations"]
    assert audit["violation_count"] == 0
    assert audit["transition_count"] == 2
    assert audit["checked_members"] == 2

    hist = {h["id"]: h for h in client.get("/api/history").json()}
    rows = {t["store_id"]: t for t in audit["transitions"]}

    # ST-NULL-01：open_time 空 → 09:00（null_fill）
    fill = rows["ST-NULL-01"]
    assert fill["field_name"] == "open_time"
    assert fill["old_is_null"] == 1 and fill["new_is_null"] == 0
    assert fill["as_of"] == "2026-11-15T00:00:00Z"
    assert fill["detect_run_id"] == det["detect_run_id"]
    # 记录与新段/旧段实字段一致
    new_hist = hist[fill["to_hist_id"]]
    assert new_hist["is_current"] == 1 and new_hist["open_time"] == "09:00"
    old_hist = hist[fill["from_hist_id"]]
    assert old_hist["is_current"] == 0 and old_hist["open_time"] is None

    # ST-WEST-02：open_time 09:30 → 空（null_clear）
    clear = rows["ST-WEST-02"]
    assert clear["field_name"] == "open_time"
    assert clear["old_is_null"] == 0 and clear["new_is_null"] == 1
    assert hist[clear["to_hist_id"]]["open_time"] is None
    assert hist[clear["from_hist_id"]]["open_time"] == "09:30"

    # 核证页协同：proof items 携带字段级跃迁记录且四方一致
    proof = client.get("/api/null-flip/proof", params={"detect_run_id": det["detect_run_id"]}).json()
    assert proof["inconsistent_count"] == 0
    for item in proof["items"]:
        assert item["member_status"] == "applied"
        assert len(item["transitions"]) == 1
        assert item["transitions"][0]["field_name"] == "open_time"
        assert item["transitions"][0]["to_hist_id"] == item["hist_id"]


def test_half_write_rollback_leaves_no_field_rows(client):
    """半截失败整笔回滚：禁止「正式已开、跃迁记录空」的任一半边。"""
    r = client.post("/api/ingest/run", json={"fixture": "null_flip_batch.json"})
    batch = r.json()["batch_id"]
    det = client.post("/api/detect/run", json={"batch_id": batch}).json()

    r = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"], "fault_after": 1})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["rolled_back"] is True

    # 跃迁记录随主事务回滚，一行不留。
    assert _db_query("SELECT COUNT(*) AS c FROM null_transition_audit")[0]["c"] == 0
    audit = _audit(client)
    assert audit["transition_count"] == 0
    assert audit["valid"] is True

    # 重试成功：记录与段一并补齐。
    r = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    audit = _audit(client)
    assert audit["transition_count"] == 2
    assert audit["valid"] is True


def test_missing_field_row_fails_verification(client):
    """再造缺字段行：甲口径核证失败（乙口径此时仅凭名单可过，此处不采用）。"""
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True

    _db_exec(
        "DELETE FROM null_transition_audit WHERE store_id=? AND field_name=?",
        ("ST-NULL-01", "open_time"),
    )

    audit = _audit(client)
    assert audit["valid"] is False
    miss = [v for v in audit["violations"] if v["code"] == "missing_field_row"]
    assert miss, audit["violations"]
    assert miss[0]["store_id"] == "ST-NULL-01"
    assert miss[0]["field"] == "open_time"

    # 核证页同步显形：开段成功而无字段级记录 → 成员判不一致。
    proof = client.get("/api/null-flip/proof", params={"detect_run_id": det["detect_run_id"]}).json()
    item = [i for i in proof["items"] if i["store_id"] == "ST-NULL-01"][0]
    assert item["three_way"]["consistent"] is False
    assert "transition:missing_field_row" in item["three_way"]["errors"]
    assert proof["inconsistent_count"] == 1


def test_direction_divergence_fails_verification(client):
    """禁止记录方向与实字段分叉：新段指针被拨回旧段后核证失败。"""
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True

    _db_exec(
        "UPDATE null_transition_audit SET to_hist_id=from_hist_id WHERE store_id=?",
        ("ST-NULL-01",),
    )

    audit = _audit(client)
    assert audit["valid"] is False
    codes = {v["code"] for v in audit["violations"]}
    # 记录新空位=非空，但所指段 open_time 实为 NULL → 方向分叉。
    assert "direction_mismatch" in codes


def test_adjacent_key_tamper_fails_verification(client):
    """禁止邻键记录被串改：记录挂到别店名下即核证失败。"""
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True

    _db_exec(
        "UPDATE null_transition_audit SET store_id=? WHERE store_id=?",
        ("ST-EAST-01", "ST-NULL-01"),
    )

    audit = _audit(client)
    assert audit["valid"] is False
    codes = {v["code"] for v in audit["violations"]}
    assert "adjacent_key_tampered" in codes


def test_audit_endpoint_is_read_only(client):
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True

    def counts():
        ov = client.get("/api/overview").json()
        return (
            ov["历史行"],
            ov["当前镜"],
            ov["检测跑次"],
            ov["跃迁记录"],
            _db_query("SELECT COUNT(*) AS c FROM detect_members")[0]["c"],
        )

    before = counts()
    assert before[3] == 2
    _audit(client)
    _audit(client, "ST-NULL-01")
    assert counts() == before, "核证接口必须只读"


def test_mixed_direction_writes_row_per_field(client, tmp_path):
    """双向同现（kind=attr_change）：每个跃迁字段各落一行，方向各自正确。"""
    fixtures = tmp_path / "fixtures"
    (fixtures / "mixed_flip.json").write_text(
        json.dumps(
            {
                "snapshots": [
                    {
                        "id": "SNAP-MIXED-ST-NULL-01",
                        "store_id": "ST-NULL-01",
                        "as_of_ts": "2026-11-16T00:00:00Z",
                        "open_time": "08:30",  # 空 → 非空
                        "close_time": None,  # 非空 → 空（种子为 20:00）
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    batch, det, applied = _run_flow(client, fixture="mixed_flip.json")
    change = [c for c in det["changes"] if c["store_id"] == "ST-NULL-01"][0]
    assert change["kind"] == "attr_change"  # 双向同现不做单一原因码
    assert applied.json()["ok"] is True, applied.text

    audit = _audit(client, "ST-NULL-01")
    assert audit["valid"] is True, audit["violations"]
    rows = {t["field_name"]: t for t in audit["transitions"]}
    assert set(rows) == {"open_time", "close_time"}
    assert (rows["open_time"]["old_is_null"], rows["open_time"]["new_is_null"]) == (1, 0)
    assert (rows["close_time"]["old_is_null"], rows["close_time"]["new_is_null"]) == (0, 1)
    assert rows["open_time"]["to_hist_id"] == rows["close_time"]["to_hist_id"]
