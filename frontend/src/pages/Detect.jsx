import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Detect() {
  const [runs, setRuns] = useState([]);
  const [members, setMembers] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = async () => {
    try {
      const rs = await api("/api/detect-runs");
      setRuns(rs);
      if (rs.length) {
        setMembers(await api(`/api/detect-runs/${rs[0].id}/members`));
      }
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  useEffect(() => { reload(); }, []);

  const run = async () => {
    setErr("");
    try {
      const body = batchId.trim() ? { batch_id: batchId.trim() } : {};
      const r = await api("/api/detect/run", { method: "POST", body: JSON.stringify(body) });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  return (
    <>
      <div data-plant="19-null-transition-audit-miss" className="plant-mark" style={{display:"none"}} />
      <div className="topbar">
        <div>
          <h1>变更检测</h1>
          <p>以现行有效历史段为基准对照快照字段，产出成员（new / attr_change / null_fill / null_clear）。</p>
        </div>
      </div>
      <div className="panel">
        <div className="actions">
          <input
            value={batchId}
            onChange={(e) => setBatchId(e.target.value)}
            placeholder="可选 batch_id，空=各店最新快照"
            style={{ minWidth: 280, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={run}>跑检测</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="panel">
        <h2>检测成员（最近跑次）</h2>
        <p className="muted">
          空↔非空翻转单列为 null_fill / null_clear；to_hash 与历史维、当前镜同源派生。
        </p>
        <table>
          <thead>
            <tr>
              <th>门店</th><th>kind</th><th>翻转字段</th><th>状态</th>
              <th>before → after</th><th>新段 hist_id</th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => (
              <tr key={m.id}>
                <td>{m.store_id}</td>
                <td>
                  <span className={`badge ${m.kind.startsWith("null") ? "suspended" : ""}`}>{m.kind}</span>
                </td>
                <td>{(JSON.parse(m.flipped_fields || "[]")).join("、") || "—"}</td>
                <td>
                  <span className={`badge ${m.status === "applied" ? "ok" : ""}`}>{m.status}</span>
                </td>
                <td className="mono">{m.from_hash || "∅"} → {m.to_hash}</td>
                <td className="mono">{m.hist_id || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2>检测跑次</h2>
        <table>
          <thead><tr><th>跑次</th><th>批</th><th>时间</th><th>变更数</th><th>报告摘要</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td className="mono">{r.batch_id || "—"}</td>
                <td>{r.created_at}</td>
                <td>{r.changed_count}</td>
                <td className="mono">{(r.report_json || "").slice(0, 120)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
