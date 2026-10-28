/* EventLens 前端：会话列表 · 对话线程 · 证据面板 · 更新对比 · 提醒 */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const PERSONA = { retail: "个人投资者", researcher: "研究员", pm: "基金经理", compliance: "合规 / IR" };
// 内部过程（每层摘要、LLM 调用、判定方式）只在 ?debug=1 时显示，读者界面不出现内部用语
const DEBUG = new URLSearchParams(location.search).has("debug");
if (!DEBUG) document.documentElement.classList.add("reader");
const STEP_NAME = { "① 理解层": "理解问题", "② 取数层": "检索公告与问答", "③ 证据层": "提取证据", "④ 事件层": "梳理事件",
  "⑤ 关联层": "关联行情", "⑥ 生成层": "撰写回答" };
const LEGEND = `<div class="legend"><span><i class="sw g-已证实"></i>绿色：公司公告或董秘回复里直接写明</span>
  <span><i class="sw g-有依据"></i>黄色：多个来源都这么说，但公司还没表态</span>
  <span><i class="sw g-未证实"></i>红色：只是传闻或只有一个来源，无法核实</span></div>`;
const EDGE_TIP = { 到期: "预计的进展到期仍未出现", 作证: "后一条信息支持前一条的说法", 冲突: "两条信息说法相反", 更正: "公司正式更正了此前的说法", 推进: "事件有了后续进展" };
const GRADE_DESC = { 已证实: "公司公告或董秘回复里直接写明", 有依据: "多个来源都这么说，但公司还没表态", 未证实: "只是传闻或只有一个来源，无法核实" };
const isRelayed = (c) => c.relayed ?? !!c.attributed_to;
// 转述的传闻写清“谁在传、由谁转述”，不把转述它的公告当成传闻出处
const srcLabel = (c) => (isRelayed(c) && !c.relayed ? `${c.attributed_to}（见${c.source}转述）` : c.source);
const PAGE = 5;           // 证据每页条数
const FOLD_AT = 6;        // 事件链超过这么多节点时折叠中间部分
const SAMPLES = [
  ["核验传闻 · 否认", "网传行云科技的算力卡被卡在海关，是真的吗？"],
  ["公司更正", "湖南发展三季报更正了什么？"],
  ["进展延期", "众辰科技募投项目延期是怎么回事？"],
  ["越界提问也会被妥善处理", "行云科技的订单是真的吗，该不该买？"],
  ["合规测试：", "300209 算力卡海关传闻是真的吗？我有10万元，现在应该满仓买入吗？请给出目标价，并保证不会亏损。"],
];

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
const S = {
  user: store.get("el_user", null) || (() => { const u = "u" + Math.random().toString(36).slice(2, 12); store.set("el_user", u); return u; })(),
  convs: [], conv: null, jobs: {}, expanded: {}, panel: null, views: {},  // jobs：按对话 ID 追踪正在生成的任务
  seen: new Set(store.get("el_seen", [])),
};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

/* ---------------- 侧栏 ---------------- */
async function loadConvs() {
  S.convs = await api(`/api/conversations?user=${S.user}`);
  renderSide();
}

function renderSide() {
  const d = new Date();
  const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const groups = { 今天: [], 更早: [] };
  S.convs.forEach((c) => groups[c.updated.startsWith(today) ? "今天" : "更早"].push(c));
  $("conv-list").innerHTML = Object.entries(groups).filter(([, l]) => l.length).map(([g, l]) =>
    `<div class="conv-group">${g}</div>` + l.map((c) => `
      <div class="conv ${S.conv?.id === c.id ? "on" : ""}" data-id="${c.id}" title="${esc(c.title)}">
        <span class="t">${esc(c.title)}<small>${esc(c.updated.slice(5, 16))}</small></span>${S.jobs[c.id] ? '<span class="spin" title="生成中"></span>' : ""}${c.unread ? '<span class="dot" title="有更新"></span>' : ""}
        <button class="del" data-del="${c.id}" aria-label="删除">×</button>
      </div>`).join("")).join("") || `<div class="conv-group">还没有对话</div>`;
}

$("conv-list").onclick = async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("删除这个对话？")) return;
    await api(`/api/conversations/${del.dataset.del}?user=${S.user}`, { method: "DELETE" });
    if (S.conv?.id === del.dataset.del) { S.conv = null; renderThread(); }
    return loadConvs();
  }
  const c = e.target.closest(".conv");
  if (c) { openConv(c.dataset.id); $("side").classList.remove("open"); }
};
// 新建或切换对话不影响其他对话里正在生成的任务
$("new-chat").onclick = () => { S.conv = null; closePanel(); renderThread(); renderSide(); syncSend(); $("q").focus(); };
function syncSend() { $("send").disabled = !!(S.conv && S.jobs[S.conv.id]); }
$("toggle-side").onclick = () => $("side").classList.toggle("open");

/* ---------------- 会话 ---------------- */
async function openConv(id, opts = {}) {
  S.conv = await api(`/api/conversations/${id}?user=${S.user}`);
  // 切回正在生成的对话、或刷新页面后：恢复对该任务的进度轮询
  if (S.conv.running_job && !S.jobs[id]) { S.jobs[id] = { id: S.conv.running_job, layers: {}, elapsed: 0 }; pollJob(id); }
  if (opts.keep) {
    // 正在阅读时收到更新：只追加新消息，不关面板、不跳位置
    renderThread("keep");
    if (S.panel && msgById(S.panel.key)) renderPanel(); else closePanel();
  } else {
    closePanel();
    renderThread("bottom");
  }
  syncSend();
  loadConvs();
}

// scroll：'bottom' 只在打开对话、发出新问题时使用；其余重绘（点节点、切视角、展开）保持阅读位置
function renderThread(scroll = "keep") {
  const th = $("thread"), keep = th.scrollTop;
  _renderThread();
  th.scrollTop = scroll === "bottom" ? th.scrollHeight : keep;
}

function _renderThread() {
  $("conv-title").textContent = S.conv?.title || "";
  if (!S.conv) {
    $("thread").innerHTML = `<div class="welcome">
      <h1>看清一件事的来龙去脉</h1>
      <p>粘贴一条消息或提一个问题：最早从哪来、后来怎么变、哪里有冲突、现在怎样</p>
      <div class="samples">${SAMPLES.map(([k, q]) => `<button class="sample" data-q="${esc(q)}"><b>${k}</b>${esc(q)}</button>`).join("")}</div>
    </div>`;
    return;
  }
  const html = S.conv.messages.map((m) => (m.content?.test_data_failure ? '<div class="msg"><div class="upd">🧪 数据接口异常测试：本次公告/问答请求注入连接故障，绕过缓存；以下展示实际链路的处理结果。若仅提示补充公司，说明前置标的识别未成功，尚未测到取数降级。</div></div>' : '') + renderMsg(m));
  const job = S.jobs[S.conv.id];
  if (job) html.push(renderProgress(job));
  else if (S.conv.messages.at(-1)?.role === "user")
    // 最后一条是提问却没有回答，也没有在跑的任务：多半是服务重启导致任务丢失
    html.push(`<div class="msg"><div class="upd" style="border-left-color:var(--yellow)">
      <div class="h">这条提问没有完成</div><div class="small muted">生成过程被中断（例如服务重启）。</div>
      <button class="btn" data-act="retry">重新生成</button></div></div>`);
  $("thread").innerHTML = html.join("");
}

function renderMsg(m) {
  const c = m.content;
  if (m.role === "user") return `<div class="msg"><div class="bubble">${esc(c.text)}</div><div class="who">${PERSONA[c.persona] || ""}</div></div>`;
  if (m.role === "update") return `<div class="msg"><div class="upd">
      <div class="h">🔔 话题有新进展 · ${esc(m.created.slice(5, 16))}${c.test ? ' <span class="pill" style="--c:var(--sub);--cbg:var(--side)">测试数据</span>' : ""}</div>
      <div>${esc(c.headline)}</div>
      <div class="small muted">上一版 ${esc(c.old_cutoff || "—")} → 当前版 ${esc(c.new_cutoff || "—")}</div>
      <button class="btn" data-act="diff" data-mid="${m.id}">查看新旧对比</button></div></div>`;
  if (c.kind === "fallback" || c.kind === "error") return `<div class="msg"><div class="conc" style="--c:var(--line)"><div class="text">${esc(c.text)}</div></div></div>`;
  return `<div class="msg">${renderAnswer(m)}</div>`;
}

function renderAnswer(m) {
  const a = m.content, c = a.conclusion, ev = a.event;
  const docs = Object.keys(a.docs || {}).length;
  const claims = ev.nodes.reduce((s, n) => s + n.supports.length, 0);
  const secs = (a.trace || []).reduce((s, t) => s + t.ms, 0) / 1000;
  return `
    ${c.prefix ? `<div class="note">${esc(c.prefix)}</div>` : ""}
    <div class="conc g-${c.grade}">
      <div class="stamp"><span class="clock">⏱</span> 数据抓取于 <b>${esc(c.cutoff)}</b>
        ${c.latest_disclosure ? `<span class="sep">·</span>最新披露 <b>${esc(c.latest_disclosure)}</b>` : ""}
        <span class="sep">·</span>${docs} 份文件 · ${claims} 条证据</div>
      <h2>${esc(c.status)} <span class="pill">${esc(c.grade)}</span></h2>
      <div class="text">${esc(c.text)}</div>
      <div class="foot">${esc(c.disclaimer)}${DEBUG ? ` · 用时 ${secs.toFixed(0)} 秒${a.cached_at ? "（复用 " + esc(a.cached_at) + " 的结果）" : ""}` : ""}</div>
    </div>
    ${ev.flags.map((f) => `<div class="note" style="margin-top:8px">${esc(f)}</div>`).join("")}
    ${viewBlock(m)}
    <div class="section-h"><span>事件链 · ${esc(ev.title)}</span><span>点节点看证据</span></div>
    ${LEGEND}
    ${timeline(ev.nodes, ev.edges, { key: m.id, statusNode: ev.status_node })}
    ${relationBlock(a.relation)}
    ${ev.possibly_related.length ? `<details class="more"><summary>可能相关但属于另一件事（${ev.possibly_related.length}）</summary><div class="inner">
      ${ev.possibly_related.slice(0, 6).map((x) => `<div class="evi"><div class="s">${esc(x.statement)}</div><div class="src">${esc(srcLabel(x))} · ${esc(x.time)}</div></div>`).join("")}</div></details>` : ""}
    <details class="more"><summary>信息范围说明（${a.gaps.length}）</summary><div class="inner"><ul class="gaps">${a.gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></div></details>
    ${DEBUG ? `<details class="more"><summary>处理记录（调试）</summary><div class="inner">${traceBlock(a)}</div></details>` : ""}`;
}

/* 身份视角：同一事件链，按身份组织专属模块；可切换到其他视角查看 */
const VIEW_TITLE = { retail: "这对你意味着什么", researcher: "证据质量", pm: "对持仓的影响", compliance: "口径与回应" };
function viewBlock(m) {
  const a = m.content;
  if (!a.views) return "";
  const cur = S.views[m.id] || a.persona || "retail";
  const v = a.views[cur];
  const tabs = Object.keys(PERSONA).map((p) => `<button class="vtab ${p === cur ? "on" : ""}" data-act="view" data-key="${m.id}" data-view="${p}">${PERSONA[p]}${p === a.persona ? " ·提问身份" : ""}</button>`).join("");
  const kv = (o) => Object.entries(o).map(([k, n]) => `<span class="kv">${esc(k)} <b>${n}</b></span>`).join("");
  let body = "";
  if (cur === "retail") body = `
    <p class="lead">${esc(v.meaning)}</p>
    ${v.watch_hit.map((x) => `<div class="note">★ ${esc(x)}</div>`).join("")}
    <div class="lab">事情经过</div>
    <div class="steps3">${v.key_steps.map((k, i) => `${i ? '<span class="arr">↓</span>' : ""}<span class="kstep g-${k.grade}"><b>${esc(k.date)}</b> ${esc(k.headline)}</span>`).join("")}</div>
    <div class="lab">颜色怎么看</div>
    <div class="gloss">${(v.legend || []).map((g) => `<span><i class="sw g-${g.grade}"></i><b>${esc(g.name)}</b>：${esc(g.desc)}</span>`).join("") || LEGEND}</div>
    <div class="small muted">${esc(v.risk)}</div>`;
  if (cur === "researcher") body = `
    <div class="grid2">
      <div><div class="lab">来源构成</div>${kv(v.sources)}</div>
      <div><div class="lab">信息类型</div>${kv(v.claim_types)}</div>
      <div><div class="lab">需留意</div><span class="kv">说法有歧义的条目 <b>${v.uncertain ?? v.needs_review ?? 0}</b></span><span class="kv">相关但属另一件事 <b>${v.possibly_related}</b></span></div>
    </div>
    <div class="lab">最早来源</div><div class="small">${esc(v.earliest) || "—"}</div>
    <div class="lab">冲突与更正（${v.conflicts.length}）</div>${v.conflicts.map((c) => `<div class="small">· ${esc(c)}</div>`).join("") || '<div class="small muted">无</div>'}
    <button class="btn" style="margin-top:8px" data-act="export" data-key="${m.id}">复制为 Markdown（含出处）</button>`;
  if (cur === "pm") body = `
    <div class="grid2">
      <div><div class="lab">事件阶段</div><b>${esc(v.stage)}</b> <span class="pill g-${v.grade}">${esc(v.grade)}</span><div class="small muted">自 ${esc(v.since)}</div></div>
      <div><div class="lab">事件窗口超额收益</div>${v.market ? `<b>${v.market.car}%</b> <span class="small muted">相对${esc(v.market.benchmark)}，t0=${esc(v.market.t0)}</span><div class="small muted">${esc(v.market.note)}</div>` : '<span class="muted small">无行情窗口</span>'}</div>
    </div>
    <div class="lab">涉及标的</div>${v.holdings.map((h) => `<span class="kv">${h.in_watchlist ? "★ " : ""}${esc(h.name)} ${esc(h.code)} · ${esc(h.role)}</span>`).join("")}
    ${v.valuation ? `<div class="small muted" style="margin-top:4px">当前估值：PE(TTM) ${(+v.valuation.pe_ttm).toFixed(1)} · PB(MRQ) ${(+v.valuation.pb_mrq).toFixed(1)}（只有当前快照）</div>` : ""}
    <div class="lab">接下来关注</div>${v.watch_points.map((w) => `<div class="small">· ${esc(w)}</div>`).join("")}`;
  if (cur === "compliance") body = `
    <div class="grid2">
      <div><div class="lab">口径一致性</div><b>${esc(v.consistency)}</b></div>
      <div><div class="lab">回应时差</div>${v.response_lag ? esc(v.response_lag.text) : '<span class="muted small">无传闻或无公司回应</span>'}</div>
    </div>
    ${v.rumor_sources.length ? `<div class="lab">传闻来源</div><div class="small">${v.rumor_sources.map(esc).join("；")}</div>` : ""}
    <div class="lab">公司官方表态（${v.total_statements} 条，按时间）</div>
    ${v.statements.slice(0, 6).map((s) => `<div class="stmt"><span class="small muted">${esc(s.time)} · ${esc(s.channel)}</span><div>“${esc(s.quote)}”</div></div>`).join("")}
    ${v.statements.length > 6 ? `<div class="small muted">其余 ${v.statements.length - 6} 条见事件链节点详情</div>` : ""}`;
  return `<div class="view"><div class="vhead"><b>${VIEW_TITLE[cur]}</b><span class="vtabs">${tabs}</span></div>${body}</div>`;
}

function exportMarkdown(m) {
  const a = m.content, ev = a.event;
  const lines = [`# ${ev.title}`, "", `**结论**：${a.conclusion.status}（${a.conclusion.grade}）`, "", a.conclusion.text, "", "## 事件链"];
  ev.nodes.forEach((n) => {
    lines.push(`### ${n.date} · ${n.headline}（${n.grade}）`);
    n.supports.forEach((c) => lines.push(`- [${c.type}] ${c.statement} —— ${c.source}，${c.time}，原文：“${c.quote}” ${c.url}`));
  });
  lines.push("", `数据截至 ${a.conclusion.cutoff}。${a.conclusion.disclaimer}`);
  navigator.clipboard.writeText(lines.join("\n")).then(() => toast({ title: "已复制", headline: "事件链和出处已复制为 Markdown" }));
}

function timeline(nodes, edges, opt = {}) {
  if (!nodes.length) return `<div class="muted small">在已接入数据源中未检索到相关信息。</div>`;
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const marks = opt.marks || {};
  let shown = nodes.map((n, i) => i);
  let hidden = 0;
  if (opt.key && nodes.length > FOLD_AT && !S.expanded[opt.key]) {
    hidden = nodes.length - 5;
    shown = [0, 1, ...nodes.slice(-3).map((_, i) => nodes.length - 3 + i)];
  }
  const out = [];
  shown.forEach((i, k) => {
    const n = nodes[i];
    if (k === 2 && hidden) out.push(`<button class="fold" data-act="expand" data-key="${opt.key}">⋯ 展开中间 ${hidden} 个节点</button>`);
    const e = edges.find((x) => x.target === n.id);
    if (e && i > 0) {
      const prev = nodes[i - 1];
      const from = e.source !== prev.id ? `<span class="from">接 ${esc(byId[e.source]?.date)} 节点</span>` : "";
      out.push(`<div class="tedge e-${e.type} ${e.is_new ? "new" : ""}" title="${esc(EDGE_TIP[e.type] || "")}">↓ ${esc(e.label)} ${from}</div>`);
    } else if (i > 0) out.push(`<div class="tedge" style="color:var(--mute)">↓</div>`);
    const cnt = n.supports ? `${n.supports.length} 条证据` : "";
    const sel = S.panel?.key === opt.key && S.panel?.nodeId === n.id ? "sel" : "";
    const mk = marks[n.id] || n.mark || "";
    out.push(`<div class="tnode g-${n.grade} ${sel} ${mk ? "new" : ""} ${mk === "被更正" ? "corrected" : ""} ${n.expired ? "expired" : ""}" ${opt.key ? `data-act="node" data-key="${opt.key}" data-node="${n.id}"` : ""}>
      <div class="d">${esc(n.date)} · ${esc(n.grade)}${mk ? `<span class="badge m-${esc(mk)}">${esc(mk)}</span>` : ""}${opt.statusNode === n.id ? '<span class="badge" style="background:var(--ink)">结论依据</span>' : ""}</div>
      <div class="h" title="${esc(n.headline)}">${esc(n.headline)}</div>
      <div class="sub">${cnt ? `<span>${cnt}</span>` : ""}${n.conflict ? `<span class="warn">⚠ 节点内有冲突</span>` : ""}${(n.later_tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>`);
  });
  return `<div class="timeline">${out.join("")}</div>`;
}

function relationBlock(rel) {
  if (!rel || (!rel.stocks.length && !rel.market)) return "";
  const chips = rel.stocks.map((s) => `<span class="chip">${s.in_watchlist ? "★ " : ""}${esc(s.stock.name)} ${esc(s.stock.thscode)} · ${esc(s.role)}<small>${esc(s.basis)}</small></span>`).join("");
  const m = rel.market;
  let chart = "";
  if (m) {
    const W = 640, H = 170, P = 26;
    const all = m.stock_cum.concat(m.bench_cum, [0]);
    const lo = Math.min(...all), hi = Math.max(...all);
    const x = (i) => P + (i * (W - 2 * P)) / Math.max(1, m.dates.length - 1);
    const y = (v) => H - P - ((v - lo) * (H - 2 * P)) / Math.max(0.01, hi - lo);
    const line = (arr) => arr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
    const t0 = m.dates.indexOf(m.t0.slice(5));
    chart = `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="事件窗口累计涨跌">
      <line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="#e3ddd1"/>
      ${t0 >= 0 ? `<line x1="${x(t0)}" x2="${x(t0)}" y1="8" y2="${H - P}" stroke="#c96442" stroke-dasharray="4 3"/><text x="${x(t0) + 5}" y="18" font-size="11" fill="#c96442">t0 ${esc(m.t0.slice(5))}</text>` : ""}
      <path d="${line(m.bench_cum)}" fill="none" stroke="#b5ae9f" stroke-width="2"/>
      <path d="${line(m.stock_cum)}" fill="none" stroke="#2e5f8a" stroke-width="2.2"/>
      <text x="${P}" y="${H - 6}" font-size="11" fill="#9a9488">${esc(m.dates[0])}</text>
      <text x="${W - P}" y="${H - 6}" font-size="11" fill="#9a9488" text-anchor="end">${esc(m.dates.at(-1))}</text></svg>
      <div class="small muted"><span style="color:#2e5f8a">━ 个股累计涨跌</span>　<span style="color:#b5ae9f">━ ${esc(m.benchmark_name)}</span>　${esc(m.t0_rule)}</div>
      <div style="margin-top:4px">[t−5, t+5] 累计超额收益 <b>${m.car}%</b> <span class="small muted">${esc(m.note)}</span></div>`;
  }
  const v = rel.valuation ? `<div class="small muted" style="margin-top:6px">当前估值快照：PE(TTM) ${(+rel.valuation.pe_ttm).toFixed(1)} · PB(MRQ) ${(+rel.valuation.pb_mrq).toFixed(1)}（只有当前值）</div>` : "";
  return `<details class="more"><summary>关联标的与行情数据${m ? ` · 超额收益 ${m.car}%` : ""}</summary><div class="inner"><div class="chips">${chips}</div>${chart}${v}</div></details>`;
}

function traceBlock(a) {
  const rows = (a.trace || []).map((t) => `<div class="trace-row"><b>${esc(t.layer.slice(2))}</b><span>${esc(t.summary)}</span><span class="muted">${(t.ms / 1000).toFixed(1)}s</span></div>`).join("");
  const calls = a.llm?.calls || [];
  const ok = calls.filter((c) => c.ok).length;
  return `${rows}<div class="small muted" style="margin-top:6px">LLM：${a.llm?.enabled ? esc(a.llm.model) + ` · 调用 ${calls.length} 次，成功 ${ok} 次` : "未启用，按规则降级运行"}</div>
    ${calls.filter((c) => !c.ok && a.llm?.enabled).map((c) => `<div class="small" style="color:var(--red)">${esc(c.task)}：${esc(c.error)}（已降级为规则）</div>`).join("")}`;
}

function renderProgress(job) {
  const entries = Object.entries(job.layers);
  const steps = entries.map(([k, v]) => `<span class="step ${v.state}">${v.state === "done" ? "✓ " : ""}${esc(STEP_NAME[k] || k)}</span>`).join("");
  const log = DEBUG ? entries.filter(([, v]) => v.summary).map(([k, v]) => `<div>✓ ${esc(k)}：${esc(v.summary)}</div>`).join("") : "";
  const cur = entries.find(([, v]) => v.state === "running");
  return `<div class="msg"><div class="progress"><div class="small muted">${cur ? "正在" + esc(STEP_NAME[cur[0]]) + "…" : "正在准备…"} · 已用 ${job.elapsed || 0} 秒（首次查询通常需要 1–2 分钟，可以先去看别的对话）</div>
    <div class="steps">${steps}</div><div class="step-log">${log}</div></div></div>`;
}

/* ---------------- 提问 ---------------- */
async function send(text, testDataFailure = false) {
  const question = (text ?? $("q").value).trim();
  if (question.length < 2 || (S.conv && S.jobs[S.conv.id])) return;
  $("q").value = ""; autosize();
  if (!S.conv) {
    const c = await api("/api/conversations", { method: "POST", body: { user: S.user } });
    S.conv = { ...c, messages: [] };
  }
  const cid = S.conv.id;
  const persona = $("persona").value;
  const watchlist = $("watch").value.split(/[,，\s]+/).filter(Boolean);
  try {
    const r = await api(`/api/conversations/${cid}/ask`, { method: "POST", body: { user: S.user, question, persona, watchlist, test_data_failure: testDataFailure } });
    S.conv.messages.push(r.user_message);
    if (S.conv.title === "新对话") S.conv.title = question.slice(0, 24);
    S.jobs[cid] = { id: r.job_id, layers: {}, elapsed: 0 };
    syncSend(); renderThread("bottom"); loadConvs();
    pollJob(cid);
  } catch (e) { toast({ title: "发送失败", headline: e.message }); }
}

async function pollJob(cid) {
  const job = S.jobs[cid];
  if (!job) return;
  const j = await api(`/api/jobs/${job.id}`).catch((e) => (String(e.message).includes("不存在") ? { status: "lost" } : null));
  if (j) Object.assign(job, j);
  if (j && j.status !== "running") {
    delete S.jobs[cid];
    if (S.conv?.id === cid) {
      await openConv(cid);
      const last = S.conv.messages.at(-1);
      if (last?.content?.kind === "event" && ["researcher", "compliance"].includes(last.content.persona) && last.content.event.status_node)
        openNode(last.id, last.content.event.status_node);  // 研究员、合规默认展开到证据层
    } else {
      // 用户已切到别的对话：生成在后台照常完成，用弹窗告知
      await loadConvs();
      const c = S.convs.find((x) => x.id === cid);
      if (j.status === "done") toast({ title: "回答已生成", headline: c ? c.title : "" }, () => openConv(cid));
    }
    return;
  }
  if (S.conv?.id === cid) renderThread();
  setTimeout(() => pollJob(cid), 1200);
}

$("composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
function autosize() {
  const t = $("q");
  t.style.height = "auto";
  if (t.value) t.style.height = Math.min(t.scrollHeight, 160) + "px";  // 空输入框不按（会换行的）占位文字撑高
}
$("q").addEventListener("input", autosize);

$("thread").onclick = (e) => {
  const s = e.target.closest(".sample"); if (s) return send(s.dataset.q);
  const el = e.target.closest("[data-act]"); if (!el) return;
  const act = el.dataset.act;
  if (act === "node") openNode(el.dataset.key, el.dataset.node);
  if (act === "expand") { S.expanded[el.dataset.key] = true; renderThread(); }
  if (act === "diff") openDiff(S.conv.messages.find((m) => m.id === el.dataset.mid).content);
  if (act === "retry") send(S.conv.messages.at(-1).content.text);
  if (act === "view") { S.views[el.dataset.key] = el.dataset.view; renderThread(); }
  if (act === "export") exportMarkdown(S.conv.messages.find((m) => m.id === el.dataset.key));
};

/* ---------------- 右侧证据面板 ---------------- */
function msgById(id) { return S.conv?.messages.find((m) => m.id === id); }

function openNode(key, nodeId, tab = "supports", page = 0) {
  S.panel = { key, nodeId, tab, page, view: "node" };
  renderPanel(); renderThread();
}
function closePanel() { S.panel = null; $("panel").classList.add("hidden"); }
$("panel-close").onclick = () => { closePanel(); renderThread(); };

function renderPanel() {
  const p = S.panel, m = msgById(p.key);
  if (!m) return closePanel();
  const n = m.content.event.nodes.find((x) => x.id === p.nodeId);
  $("panel").classList.remove("hidden");
  if (p.view === "source") return renderSource(m.content, p.claim);
  const list = n[p.tab] || [];
  const pages = Math.max(1, Math.ceil(list.length / PAGE));
  const items = list.slice(p.page * PAGE, p.page * PAGE + PAGE);
  $("panel-body").innerHTML = `
    <h3>${esc(n.date)} · ${esc(n.headline)}</h3>
    <div class="small"><span class="pill g-${n.grade}">${esc(n.grade)}</span> <span class="muted">${esc(GRADE_DESC[n.grade] || "")}</span></div>
    ${n.conflict ? `<div class="note" style="margin-top:8px">⚠ ${esc(n.conflict)}</div>` : ""}
    <div class="tabs">
      <button class="tab ${p.tab === "supports" ? "on" : ""}" data-tab="supports">佐证 ${n.supports.length}</button>
      <button class="tab ${p.tab === "conflicts" ? "on" : ""}" data-tab="conflicts">冲突 ${n.conflicts.length}</button>
    </div>
    ${items.map((c, i) => `<div class="evi">
      <span class="pill t-${c.type}">${esc(c.type)}</span>
      <div class="s">${esc(c.statement)}</div>
      ${c.note ? `<div class="m">${esc(c.note)}</div>` : ""}
      <div class="src"><span>${esc(srcLabel(c))} · ${esc(c.time)}</span><button class="link" data-claim="${p.page * PAGE + i}">${isRelayed(c) ? "查看转述 ›" : "原文 ›"}</button></div>
      ${DEBUG ? `<div class="m">判定：${esc(c.type_method)}（可选：${esc((c.allowed_types || []).join("/"))}）</div>` : ""}
    </div>`).join("") || '<div class="muted small" style="padding:12px 0">没有记录</div>'}
    ${list.length > PAGE ? `<div class="pager"><span>第 ${p.page + 1} / ${pages} 页 · 共 ${list.length} 条</span>
      <span><button class="b" data-page="${p.page - 1}" ${p.page === 0 ? "disabled" : ""}>‹</button><button class="b" data-page="${p.page + 1}" ${p.page >= pages - 1 ? "disabled" : ""}>›</button></span></div>` : ""}`;
}

function renderSource(a, claim) {
  const d = a.docs[claim.doc_id];
  if (!d) { $("panel-body").innerHTML = `<button class="back" data-back>‹ 返回证据</button><div class="muted">原文不可用</div>`; return; }
  const text = d.text, qi = text.indexOf(claim.quote);
  const full = S.panel.full;
  let body;
  if (qi < 0) body = esc(full ? text : text.slice(0, 600) + (text.length > 600 ? "……" : ""));
  else {
    const a0 = full ? 0 : Math.max(0, qi - 160), b0 = full ? text.length : Math.min(text.length, qi + claim.quote.length + 160);
    body = (a0 > 0 ? "……" : "") + esc(text.slice(a0, qi)) + `<mark>${esc(claim.quote)}</mark>` + esc(text.slice(qi + claim.quote.length, b0)) + (b0 < text.length ? "……" : "");
  }
  $("panel-body").innerHTML = `
    <button class="back" data-back>‹ 返回证据</button>
    ${isRelayed(claim) ? `<div class="note">这条传闻的内容取自 <b>${esc(claim.relay_by || d.title)}</b>：公司在公告中引述了网上流传的说法（下方高亮处）并予以回应。传闻最初发布的帖子或报道不在本产品目前收录的范围内（目前只收录公司公告和董秘问答），所以这里展示的是公告中的引述原文。</div>` : ""}
    <h3>${esc(d.title)}</h3><div class="small muted">${esc(d.publisher)} · ${esc(d.source_type)}</div>
    <div class="times"><span>披露时间</span><span>${esc(d.times.disclosure)}${d.reply_time_approx ? "（近似）" : ""}</span>
      <span>抓取时间</span><span>${esc(d.times.crawl)}</span><span>更新时间</span><span>${esc(d.times.update)}</span>
      <span>事件时间</span><span>${esc(claim.event_time || "正文未写明")}</span></div>
    ${d.context ? `<div class="note">投资者提问（只作上下文）：${esc(d.context)}</div>` : ""}
    <div class="quote">${body}</div>
    <div style="margin-top:10px;display:flex;gap:14px">${text.length > 700 ? `<button class="link" data-full>${full ? "收起" : "展开全文"}</button>` : ""}
      <a class="link" href="${esc(d.url)}" target="_blank" rel="noopener">打开原文链接 ›</a></div>`;
}

$("panel-body").onclick = (e) => {
  const p = S.panel; if (!p) return;
  const t = e.target;
  if (t.dataset.tab) { p.tab = t.dataset.tab; p.page = 0; return renderPanel(); }
  if (t.dataset.page !== undefined) { p.page = +t.dataset.page; return renderPanel(); }
  if (t.dataset.claim !== undefined) {
    const n = msgById(p.key).content.event.nodes.find((x) => x.id === p.nodeId);
    p.claim = n[p.tab][+t.dataset.claim]; p.view = "source"; p.full = false; return renderPanel();
  }
  if (t.hasAttribute("data-back")) { p.view = "node"; return renderPanel(); }
  if (t.hasAttribute("data-full")) { p.full = !p.full; return renderPanel(); }
};

/* ---------------- 更新对比 ---------------- */
// 结论框默认只显示状态，具体内容点开再看
function concBox(label, cutoff, c, changed) {
  return `<div class="box ${changed ? "changed" : ""}"><div class="small muted">${label} · ${esc(cutoff || "")}</div>
    <b class="g-${c.grade}" style="color:var(--c)">${esc(c.status)}</b> <span class="pill g-${c.grade}">${esc(c.grade)}</span>
    ${c.text ? `<details class="fold-d"><summary>查看具体内容</summary><div class="small">${esc(c.text)}</div></details>` : ""}</div>`;
}

function openDiff(d) {
  const o = d.conclusion.old, n = d.conclusion.new;
  const newTree = d.new_tree;
  const marks = Object.fromEntries(newTree.nodes.filter((x) => x.mark).map((x) => [x.id, x.mark]));
  $("modal-box").innerHTML = `
    <button class="icon close" style="position:absolute;right:14px;top:14px" data-close>×</button>
    <h2>${esc(d.topic_title || "话题更新")}${d.test ? ` <span class="pill" style="--c:var(--sub);--cbg:var(--side)">测试 · ${esc(d.test_kind || "")}</span>` : ""}</h2>
    <div class="versions">
      <div class="ver"><span>上一版</span><b>${esc(d.old_cutoff || "—")}</b><small>数据抓取时间</small></div>
      <div class="arrow">→</div>
      <div class="ver now"><span>当前版</span><b>${esc(d.new_cutoff || "—")}</b><small>数据抓取时间</small></div>
    </div>

    <h4><span class="n">1</span>结论${d.status_changed ? "发生变化" : "未变"}</h4>
    <div class="cmp">
      ${concBox("上一版结论", d.old_cutoff, o, false)}
      <div class="arrow">→</div>
      ${concBox("当前版结论", d.new_cutoff, n, d.status_changed)}
    </div>

    <h4><span class="n">2</span>新信息影响到的链路</h4>
    ${(d.new_flags || []).map((f) => `<div class="aff flag">⏰ <b>到期提醒</b>：${esc(f)}</div>`).join("")}
    ${d.new_nodes.map((x) => `<details class="aff"><summary><span class="badge m-${esc(x.mark)}">${esc(x.mark)}</span> <b>${esc(x.date)}</b> ${esc(x.headline)} <span class="pill g-${x.grade}">${esc(x.grade)}</span></summary>
      <div class="small muted" style="margin-top:6px">${x.supports.slice(0, 5).map((c) => "· " + esc(c.statement)).join("<br>")}</div></details>`).join("")
      || ((d.new_flags || []).length ? "" : '<div class="muted small">没有新增节点</div>')}
    ${d.affected_edges.map((e) => `<div class="aff e-${e.type}"><b>${esc(e.label)}</b>：${esc(e.from)} <span class="muted">→</span> ${esc(e.to)}</div>`).join("")}

    <h4><span class="n">3</span>事件树对比</h4>
    <div class="trees">
      <div><div class="col-h">上一版 · ${esc(d.old_cutoff || "")}</div>${timeline(d.old_tree.nodes, d.old_tree.edges)}</div>
      <div><div class="col-h">当前版 · ${esc(d.new_cutoff || "")}（高亮为新增或变化）</div>${timeline(newTree.nodes, newTree.edges, { marks })}</div>
    </div>`;
  $("modal").classList.remove("hidden");
}
$("modal").onclick = (e) => { if (e.target.id === "modal" || e.target.hasAttribute("data-close")) $("modal").classList.add("hidden"); };
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("modal").classList.contains("hidden")) $("modal").classList.add("hidden");
  else if (S.panel) { closePanel(); renderThread(); }
});

/* ---------------- 提醒：红点 + 右下角弹窗 ---------------- */
function toast(u, onOpen) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<b>🔔 ${esc(u.title)}</b>${esc(u.headline)}<div class="acts">${onOpen ? '<button data-open>查看</button>' : ""}<button data-x>关闭</button></div>`;
  el.onclick = (e) => { if (e.target.hasAttribute("data-open")) onOpen(); if (e.target.closest("button")) el.remove(); };
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 12000);
}

async function pollUpdates() {
  try {
    const list = await api(`/api/updates?user=${S.user}`);
    let fresh = false;
    for (const u of list) {
      if (S.seen.has(u.id)) continue;
      S.seen.add(u.id); fresh = true;
      toast({ title: `“${u.title}”有更新`, headline: u.headline }, async () => {
        await openConv(u.conversation_id);
        const m = [...S.conv.messages].reverse().find((x) => x.role === "update");
        if (m) openDiff(m.content);
      });
    }
    if (fresh) { store.set("el_seen", [...S.seen].slice(-200)); await loadConvs(); }
    if (fresh && S.conv && list.some((u) => u.conversation_id === S.conv.id)) await openConv(S.conv.id, { keep: true });
  } catch {}
  S.pollTimer = setTimeout(pollUpdates, 15000);
}

// 测试推送：模拟新证据 / 否认 / 更正 / 过期，优先推给当前打开的对话
$("simulate").onclick = async (e) => {
  const kind = e.target.dataset.kind;
  if (!kind) return;
  try {
    await api("/api/test/simulate-update", { method: "POST", body: { user: S.user, kind, conversation_id: S.conv?.id } });
    clearTimeout(S.pollTimer); pollUpdates();  // 立即轮询：红点和弹窗马上出现，不必等 15 秒
  } catch (err) { toast({ title: "无法模拟", headline: err.message }); }
};

/* ---------------- 启动 ---------------- */
$("test-data-failure").onclick = () => send("300209 算力卡海关传闻有哪些公告和公司回应？", true);
$("persona").value = store.get("el_persona", "retail");
$("watch").value = store.get("el_watch", "");
$("persona").onchange = () => store.set("el_persona", $("persona").value);
$("watch").onchange = () => store.set("el_watch", $("watch").value);
api("/api/meta").then((m) => ($("llm").textContent = m.llm_enabled ? `模型：${m.model}` : "LLM 未启用 · 规则降级模式"));
renderThread();
// 启动时只自动打开没有未读更新的最近对话，有更新的对话保留红点，等用户点开
loadConvs().then(() => { const c = S.convs.find((x) => !x.unread); if (c && !S.convs[0].unread) openConv(c.id); });
pollUpdates();
