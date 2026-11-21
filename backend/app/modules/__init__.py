"""邻题空桩：同仓换主链路时实现其一，勿同构换皮。"""
IMPLEMENTED = {"null_to_value_open": "空值翻转开段（null_fill/null_clear）"}
STUBS = [
    s
    for s in [
        "same_day_multi_change",
        "late_snapshot_apply",
        "null_to_value_open",
        "logical_delete_close",
        "current_mirror_resync",
        "as_of_query_proof",
        "business_key_rebind",
        "weekday_mask_split",
        "open_close_order_fence",
        "hist_row_hash_proof",
        "detect_coverage_member_proof",
        "draft_then_commit_switch",
        "concurrent_version_hold",
        "snapshot_field_allowlist",
        "region_cut_audit",
        "batch_detect_hist_reconcile",
    ]
    if s not in IMPLEMENTED
]
