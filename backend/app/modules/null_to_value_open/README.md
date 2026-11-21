# null_to_value_open · 空值翻转开段

现行有效段（`dim_store_hours_hist.is_current=1`）的 `open_time` / `close_time`
发生 **空 ↔ 非空翻转** 时（NULL 或空白串都算空），按属性事件 **关旧开新**。

## 硬约束

| 约束 | 实现 |
| --- | --- |
| 禁止原地填洞 | 旧段保持原值，只写 `valid_to` + `is_current=0`；新开一行 |
| 禁止当无变更跳过 | detect 把翻转单列为 `null_fill` / `null_clear` 成员 |
| 三方同源 | `derive_row_hash` 是历史维 / 当前镜 / 检测 `to_hash` 的唯一派生入口 |
| 落库形态（择一） | **乙：新段挂原因码**，写 `change_reason` + `prev_hist_id` |
| 失败闭（择一） | **甲：同事务**，关旧 + 开新 + 镜刷新 + 成员写回一个 `BEGIN IMMEDIATE` |

## 流程

1. `POST /api/ingest/run` —— 贴源快照入档。
2. `POST /api/detect/run` —— 以现行有效历史段为基准，产出 `detect_members`，
   `to_hash` 由字段重算，不信任贴源 hash。
3. `POST /api/scd/apply` —— 同事务关旧开新 + 刷镜 + 写回成员；事务内三方自检，
   任何冲突/异常整笔回滚（成员保持 `pending`）。
4. `GET /api/null-flip/proof` —— **只读**核证，返回前后 hash、`from_hist_id`、
   `hist_id`、成员 kind、三方一致性；`GET /api/null-flip/tears` 列撕裂。

## 夹具

- `fixtures/seed_stores.json` 中 `ST-NULL-01`：种子 `open_time=NULL`。
- `fixtures/null_flip_batch.json`：
  - `ST-NULL-01` 空开店时刻 → `09:00`（`null_fill`）
  - `ST-WEST-02` `09:30` → 空（`null_clear`）

## 测试

```bash
cd backend
python -m pytest -q        # 8 项：开新段 / 清空 / 半写回滚 / 只读核证 / 旧跑次冲突
```

故障注入：`POST /api/scd/apply {"detect_run_id": "...", "fault_after": 1}`，
仅在 `HOURSPAN_TEST_HOOKS=1` 时开放。
