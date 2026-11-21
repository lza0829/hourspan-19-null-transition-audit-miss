"""空值翻转开段链路测试（null_to_value_open）。

覆盖：
1. 空开店时刻 → 补齐钟点：新开一段（非原地填洞），原因码 null_fill，三方同源。
2. 非空 → 空：新开一段，原因码 null_clear。
3. 半写失败注入：整笔回滚，历史维 / 当前镜 / 检测三方不撕裂。
4. 只读核证接口不写库，字段齐全。
"""

import json
import os
import shutil
import tempfile
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

    for mod in (db, seed, detect, ingest, scd, nvo):
        importlib.reload(mod)

    import app.routers.core as core
    importlib.reload(core)
    import app.main as main_mod
    importlib.reload(main_mod)

    seed.init_db()
    with TestClient(main_mod.app) as c:
        yield c


def _run_flow(client, fixture="null_flip_batch.json", batch=None):
    if batch is None:
        r = client.post("/api/ingest/run", json={"fixture": fixture})
        assert r.status_code == 200, r.text
        batch = r.json()["batch_id"]
    r = client.post("/api/detect/run", json={"batch_id": batch})
    assert r.status_code == 200, r.text
    det = r.json()
    r = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"]})
    return batch, det, r


def _hist(client, store_id):
    return [h for h in client.get("/api/history", params={"store_id": store_id}).json()]


def _mirror(client, store_id):
    for m in client.get("/api/current").json():
        if m["store_id"] == store_id:
            return m
    return None


def test_null_open_filled_opens_new_segment(client):
    # 种子里 ST-NULL-01 open_time 为 NULL。
    before = _hist(client, "ST-NULL-01")
    assert len(before) == 1
    assert before[0]["open_time"] is None
    assert before[0]["is_current"] == 1
    old_hash = before[0]["row_hash"]

    batch, det, applied = _run_flow(client)
    assert det["changed_count"] == 2
    assert det["null_flip_count"] == 2, det
    body = applied.json()
    assert body["ok"] is True, body
    assert body["count"] == 2

    after = _hist(client, "ST-NULL-01")
    assert len(after) == 2, "必须新开一段，禁止原地填洞"
    old = [h for h in after if h["id"] == before[0]["id"]][0]
    new = [h for h in after if h["is_current"] == 1][0]
    assert old["is_current"] == 0
    assert old["open_time"] is None, "旧段不得被原地 UPDATE"
    assert old["row_hash"] == old_hash
    assert old["valid_to"] == "2026-11-15T00:00:00Z"
    assert new["open_time"] == "09:00"
    assert new["change_reason"] == "null_fill"
    assert new["prev_hist_id"] == old["id"]

    mirror = _mirror(client, "ST-NULL-01")
    assert mirror["open_time"] == "09:00"
    assert mirror["hist_id"] == new["id"]
    assert mirror["row_hash"] == new["row_hash"]

    # to_hash 同源：检测成员 to_hash == 新段 hash == 镜 hash。
    members = client.get(f"/api/detect-runs/{det['detect_run_id']}/members").json()
    m = [x for x in members if x["store_id"] == "ST-NULL-01"][0]
    assert m["kind"] == "null_fill"
    assert m["to_hash"] == new["row_hash"] == mirror["row_hash"]
    assert m["from_hash"] == old_hash
    assert m["status"] == "applied"


def test_value_cleared_opens_new_segment(client):
    batch, det, applied = _run_flow(client)
    body = applied.json()
    assert body["ok"] is True

    rows = _hist(client, "ST-WEST-02")
    assert len(rows) == 2
    new = [h for h in rows if h["is_current"] == 1][0]
    old = [h for h in rows if h["is_current"] == 0][0]
    assert old["open_time"] == "09:30", "旧段保留原值"
    assert new["open_time"] is None
    assert new["change_reason"] == "null_clear"
    mirror = _mirror(client, "ST-WEST-02")
    assert mirror["open_time"] is None
    assert mirror["hist_id"] == new["id"]


def test_half_write_failure_rolls_back_all(client):
    before_null = _hist(client, "ST-NULL-01")
    before_west = _hist(client, "ST-WEST-02")
    before_mirror = {m["store_id"]: m for m in client.get("/api/current").json()}

    r = client.post("/api/ingest/run", json={"fixture": "null_flip_batch.json"})
    batch = r.json()["batch_id"]
    det = client.post("/api/detect/run", json={"batch_id": batch}).json()

    # 注入：处理 1 个成员后失败。
    r = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"], "fault_after": 1})
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["rolled_back"] is True
    assert body["count"] == 0

    # 历史维整笔未动。
    assert _hist(client, "ST-NULL-01") == before_null
    assert _hist(client, "ST-WEST-02") == before_west
    # 当前镜整笔未动。
    after_mirror = {m["store_id"]: m for m in client.get("/api/current").json()}
    assert after_mirror == before_mirror

    # 成员全部回退 pending，且未挂 hist_id。
    members = client.get(f"/api/detect-runs/{det['detect_run_id']}/members").json()
    assert all(m["status"] == "pending" for m in members)
    assert all(m["hist_id"] is None for m in members)

    # 失败可观测。
    run = [x for x in client.get("/api/detect-runs").json() if x["id"] == det["detect_run_id"]][0]
    assert run["apply_rolled_back"] == 1
    assert run["apply_error"]

    # 核证页：无撕裂，检测已标 changed 但尚未开段 → pending，但三方一致。
    proof = client.get("/api/null-flip/proof", params={"detect_run_id": det["detect_run_id"]}).json()
    assert proof["read_only"] is True
    assert proof["tear_count"] == 0
    assert proof["pending_count"] == 2
    assert all(i["three_way"]["consistent"] for i in proof["items"])

    # 重试成功。
    r = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert _mirror(client, "ST-NULL-01")["open_time"] == "09:00"


def test_proof_endpoint_is_read_only_and_complete(client):
    batch, det, applied = _run_flow(client)
    assert applied.json()["ok"] is True

    # 记录核证前后库计数，确认只读。
    def counts():
        ov = client.get("/api/overview").json()
        return (ov["历史行"], ov["当前镜"], ov["核证条"], ov["检测跑次"])

    before = counts()
    proof = client.get("/api/null-flip/proof", params={"detect_run_id": det["detect_run_id"]}).json()
    after = counts()
    assert before == after, "核证接口必须只读"

    assert proof["null_flip_count"] == 2
    assert proof["inconsistent_count"] == 0
    assert proof["tear_count"] == 0
    for item in proof["items"]:
        assert set(
            ["before_hash", "after_hash", "hist_id", "kind", "from_hist_id", "member_status", "three_way"]
        ) <= set(item)
        assert item["member_status"] == "applied"
        assert item["hist_id"]
        assert item["from_hist_id"]
        assert item["kind"] in ("null_fill", "null_clear")
        assert item["three_way"]["to_hash_same_source"] == item["after_hash"]


def test_tears_endpoint_clean_after_apply(client):
    _run_flow(client)
    tears = client.get("/api/null-flip/tears").json()
    assert tears["tear_count"] == 0


def test_plain_attr_change_still_works(client):
    """原变更夹具（非空→非空 / 状态变化）不应被空值逻辑误伤。"""
    batch, det, applied = _run_flow(client, fixture="ingest_change_batch.json")
    assert det["null_flip_count"] == 0
    kinds = {c["store_id"]: c["kind"] for c in det["changes"]}
    assert kinds["ST-EAST-01"] == "attr_change"
    assert kinds["ST-SOUTH-01"] == "attr_change"
    assert applied.json()["ok"] is True

    rows = _hist(client, "ST-EAST-01")
    assert len(rows) == 2
    new = [h for h in rows if h["is_current"] == 1][0]
    assert new["open_time"] == "09:30"
    assert new["change_reason"] is None
    assert _mirror(client, "ST-EAST-01")["hist_id"] == new["id"]


def test_apply_is_idempotent(client):
    batch, det, first = _run_flow(client)
    assert first.json()["ok"] is True

    again = client.post("/api/scd/apply", json={"detect_run_id": det["detect_run_id"]})
    assert again.status_code == 200
    assert again.json()["count"] == 0
    # 重复应用不得再开段。
    assert len(_hist(client, "ST-NULL-01")) == 2


def test_stale_run_conflicts_and_rolls_back(client, tmp_path):
    """旧跑次在其后已有新段时应用必须拒绝，且整笔回滚。"""
    fixtures = tmp_path / "fixtures"
    (fixtures / "seq_a.json").write_text(
        json.dumps(
            {
                "snapshots": [
                    {
                        "id": "SNAP-SEQ-A",
                        "store_id": "ST-WEST-01",
                        "as_of_ts": "2026-11-10T00:00:00Z",
                        "open_time": "08:00",
                        "close_time": "20:30",
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    },
                    {
                        "id": "SNAP-SEQ-A-NULL",
                        "store_id": "ST-NULL-01",
                        "as_of_ts": "2026-11-10T00:00:00Z",
                        "open_time": "09:00",
                        "close_time": "20:00",
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (fixtures / "seq_b.json").write_text(
        json.dumps(
            {
                "snapshots": [
                    {
                        "id": "SNAP-SEQ-B",
                        "store_id": "ST-WEST-01",
                        "as_of_ts": "2026-11-12T00:00:00Z",
                        "open_time": "07:30",
                        "close_time": "20:30",
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    b1 = client.post("/api/ingest/run", json={"fixture": "seq_a.json"}).json()["batch_id"]
    d1 = client.post("/api/detect/run", json={"batch_id": b1}).json()
    assert d1["changed_count"] == 2

    b2 = client.post("/api/ingest/run", json={"fixture": "seq_b.json"}).json()["batch_id"]
    d2 = client.post("/api/detect/run", json={"batch_id": b2}).json()
    assert d2["changed_count"] == 1

    # 先应用较新的跑次（WEST-01 → 07:30）。
    assert client.post("/api/scd/apply", json={"detect_run_id": d2["detect_run_id"]}).json()["ok"] is True

    west_before = _hist(client, "ST-WEST-01")
    null_before = _hist(client, "ST-NULL-01")

    # 再应用旧跑次：WEST-01 现行段已漂移 → 整笔回滚，ST-NULL-01 也不得开段。
    r = client.post("/api/scd/apply", json={"detect_run_id": d1["detect_run_id"]})
    assert r.status_code == 409
    assert r.json()["detail"]["rolled_back"] is True

    assert _hist(client, "ST-WEST-01") == west_before
    assert _hist(client, "ST-NULL-01") == null_before
    assert _mirror(client, "ST-NULL-01")["open_time"] is None
    tears = client.get("/api/null-flip/tears").json()
    assert tears["tear_count"] == 0


