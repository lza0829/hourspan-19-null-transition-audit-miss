import React, { useEffect, useState } from "react";
import { api } from "../api.js";

function fieldLabel(f) {
  return f === "open_time" ? "开店" : f === "close_time" ? "闭店" : f;
}

export default function NullFlip() {
  const [storeId, setStoreId] = useState("");
  const [runId, setRunId] = useState("");
  const [proof, setProof] = useState(null);
  const [tears, setTears] = useState(null);
  const [audit, setAudit] = useState(null);
  const [err, setErr] = useState("");

  const loadProof = async () => {
    setErr("");
    try {
      const q = runId.trim() ? `?detect_run_id=${encodeURIComponent(runId.trim())}` : "";
      const aq = storeId.trim() ? `?store_id=${encodeURIComponent(storeId.trim())}` : "";
      setProof(await api(`/api/null-flip/proof${q}`));
      setTears(await api("/api/null-flip/tears"));
      setAudit(await api(`/api/null-transition/audit${aq}`));
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  useEffect(() => { loadProof(); }, []);

  const items = (proof?.items || []).filter(
    (i) => !storeId.trim() || i.store_id === storeId.trim()
  );

  return (
    <>
      <div data-plant="19-null-transition-audit-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>空值翻转核证</h1>
          <p>
            空↔非空翻转必须关旧开新；本页只读，对照 detect 成员 / 历史段 / 当前镜 /
            字段级跃迁记录四方，输出前后 hash、hist_id 与成员 kind。
          </p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="可选 detect_run_id，空=最近 400 条成员"
            style={{ minWidth: 300, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <input
            value={storeId}
            onChange={(e) => setStoreId(e.target.value)}
            placeholder="按门店过滤，如 ST-NULL-01"
            style={{ minWidth: 200, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={loadProof}>刷新（只读）</button>
        </div>
        {err && <p className="error">{err}</p>}
        {proof && (
          <div className="grid-cards">
            <div className="card"><div className="label">成员</div><div className="value">{proof.item_count}</div></div>
            <div className="card"><div className="label">空值翻转</div><div className="value">{proof.null_flip_count}</div></div>
            <div className="card"><div className="label">待应用</div><div className="value">{proof.pending_count}</div></div>
            <div className="card"><div className="label">三方不一致</div><div className="value">{proof.inconsistent_count}</div></div>
            <div className="card"><div className="label">撕裂</div><div className="value">{proof.tear_count}</div></div>
          </div>
        )}
        {tears?.tears?.length > 0 && (
          <pre className="msg">{JSON.stringify(tears.tears, null, 2)}</pre>
        )}
      </div>
      <div className="panel">
        <h2>翻转成员</h2>
        <table>
          <thead>
            <tr>
              <th>跑次</th><th>门店</th><th>kind</th><th>翻转字段</th><th>状态</th>
              <th>before → after hash</th><th>旧段 hist_id</th><th>新段 hist_id</th>
              <th>段当前</th><th>原因</th><th>三方</th>
            </tr>
          </thead>
          <tbody>
            {items.map((i) => (
              <tr key={`${i.detect_run_id}-${i.store_id}-${i.after_hash}`}>
                <td className="mono">{i.detect_run_id}</td>
                <td>{i.store_id}</td>
                <td>
                  <span className={`badge ${i.kind.startsWith("null") ? "suspended" : ""}`}>{i.kind}</span>
                </td>
                <td>{i.flipped_fields.map(fieldLabel).join("、") || "—"}</td>
                <td>
                  <span className={`badge ${i.member_status === "applied" ? "ok" : ""}`}>{i.member_status}</span>
                </td>
                <td className="mono">{i.before_hash || "∅"} → {i.after_hash}</td>
                <td className="mono">{i.from_hist_id || "—"}</td>
                <td className="mono">{i.hist_id || "—"}</td>
                <td>{i.new_hist_current ? "是" : "否"}</td>
                <td className="mono">{i.new_hist_reason || "—"}</td>
                <td>
                  <span className={`badge ${i.three_way.consistent ? "ok" : "bad"}`}>
                    {i.three_way.consistent ? "一致" : "撕裂"}
                  </span>
                  {!i.three_way.consistent && (
                    <div className="mono muted">{i.three_way.errors.join(", ")}</div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>字段级跃迁记录（甲 · before/after 空位）</h2>
        <p className="muted">
          每个发生空↔非空跃迁的可空字段一行，与开段同事务同成败；缺字段行 / 方向分叉 / 邻键串改即核证失败。只读。
        </p>
        {audit && (
          <>
            <div className="grid-cards">
              <div className="card"><div className="label">跃迁记录</div><div className="value">{audit.transition_count}</div></div>
              <div className="card"><div className="label">受核成员</div><div className="value">{audit.checked_members}</div></div>
              <div className="card"><div className="label">违例</div><div className="value">{audit.violation_count}</div></div>
              <div className="card"><div className="label">核证</div><div className="value">{audit.valid ? "通过" : "失败"}</div></div>
            </div>
            <table>
              <thead>
                <tr>
                  <th>门店</th><th>字段</th><th>旧空位</th><th>新空位</th>
                  <th>旧 hist</th><th>新 hist</th><th>as_of</th><th>跑次</th><th>校验</th>
                </tr>
              </thead>
              <tbody>
                {(audit.transitions || []).map((t) => (
                  <tr key={t.id}>
                    <td>{t.store_id}</td>
                    <td>{fieldLabel(t.field_name)}</td>
                    <td>{t.old_is_null ? "空" : "非空"}</td>
                    <td>{t.new_is_null ? "空" : "非空"}</td>
                    <td className="mono">{t.from_hist_id}</td>
                    <td className="mono">{t.to_hist_id}</td>
                    <td className="mono">{t.as_of}</td>
                    <td className="mono">{t.detect_run_id}</td>
                    <td>
                      <span className={`badge ${t.valid ? "ok" : "bad"}`}>{t.valid ? "一致" : "撕裂"}</span>
                      {!t.valid && <div className="mono muted">{t.errors.join(", ")}</div>}
                    </td>
                  </tr>
                ))}
                {audit.transitions?.length === 0 && (
                  <tr><td colSpan={9} className="muted">暂无跃迁记录</td></tr>
                )}
              </tbody>
            </table>
            {audit.violations?.length > 0 && (
              <pre className="msg">{JSON.stringify(audit.violations, null, 2)}</pre>
            )}
          </>
        )}
      </div>
    </>
  );
}
