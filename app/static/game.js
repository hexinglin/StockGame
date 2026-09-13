/* ============================================================
 * StockGame 前端逻辑 — 轮次管理 + 游戏视图（分时图/盘口/下单）
 * ============================================================ */
"use strict";

// ───────────── 全局状态 ─────────────
const state = {
    roundId: null,          // 当前游戏轮次
    round: null,            // 轮次详情
    ticks: [],              // 已播出区间的 tick（接口按 last_time_key 截断，不含未来）
    tickTotal: 0,           // 全天可播条数（进度分母）
    minutePoints: [],       // 已定格分钟点 {time, price, vol, amount}
    livePoint: null,        // 进行中分钟（跳变点）
    cumAmount: 0,           // 累计成交额（本轮）
    cumVolume: 0,           // 累计成交量
    lastClose: 0,           // 昨收
    lastPrice: 0,           // 最新价
    dayOpen: 0, dayHigh: 0, dayLow: 0,
    side: "buy",            // buy/sell
    otype: "limit",         // 仅支持限价
    socket: null,
    chart: null,
    lastTickVol: 0,         // 当笔快照增量成交量（盘口模拟用）
    showPreMarket: false,   // 是否显示盘前集合竞价段：进入游戏默认按行情时间打开（<09:40），过 09:40 自动关闭
    preAutoClosed: false,   // 是否已跨过 09:40 自动关闭点（只自动关闭一次，之后交用户手动控制）
};

const FEE_RATE = 0.0001;    // 万1
const CODE_DEFAULT = "588000.SH";

// 账户卡/十档盘口为 innerHTML 全量重建，高速档每 tick 重建（x60≈60 次/秒）开销大且
// 视觉抖动，按时间节流；x1（1 tick/秒，间隔 > 阈值）不受影响。成交/进入等关键场景
// 传 force=true 立即渲染。行情数字与分时图仍每 tick 更新（textContent 轻量、需实时）。
const RENDER_THROTTLE_MS = 500;
let _lastAcctTs = 0, _lastLv5Ts = 0;

// 委托列表数据（盘口据此标注「我的委托」量，见表头筛选与 renderLevel5）
let ordersHideFilled = false;
let ordersLastRows = [];

// ───────────── API 封装 ─────────────
async function api(url, method = "GET", body = null) {
    const opt = { method, headers: { "Content-Type": "application/json" } };
    if (body) opt.body = JSON.stringify(body);
    const resp = await fetch(url, opt);
    const data = await resp.json().catch(() => ({}));
    if (data.code !== 0) throw new Error(data.message || `请求失败 ${url}`);
    return data;
}

function fmt(n, d = 2) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function fmtVol(n) {
    if (!n) return "0";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(2) + "万";
    return String(n);
}

// 成交额（元）：亿元为主口径（与成交量 亿/万 一致），不足万级时回落原值
function fmtAmt(n) {
    if (!n) return "0";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(2) + "万";
    return fmt(n);
}

// 价格有效性：0/空/NaN 视为无值（集合竞价时段 QMT 上报的 high/low 为 0，
// 若参与 min/max 会把今低算成 0）
function validPrice(v) {
    return typeof v === "number" && isFinite(v) && v > 0;
}

// 价格显示：无有效值显示 "-"，不展示 0.000 这类占位数
function fmtPx(v, d = 3) {
    return validPrice(v) ? fmt(v, d) : "-";
}

// 数量对外（操作/展示）统一用「万股」，计算与接口传输一律用「股」（在此集中换算）
const SHARES_PER_WAN = 10000;
function fmtWan(shares, d = 2) {
    // 股 → 万股显示：100 股 = 0.01 万股，默认 2 位小数即精确；非整百数量（如成本底仓）回退 4 位
    if (shares === null || shares === undefined || isNaN(shares)) return "--";
    const wan = Number(shares) / SHARES_PER_WAN;
    const nd = Math.abs(wan - Number(wan.toFixed(d))) < 1e-9 ? d : Math.max(d, 4);
    return wan.toLocaleString("zh-CN", { minimumFractionDigits: nd, maximumFractionDigits: nd });
}
function toShares(wan) {
    // 万股输入 → 股（四舍五入到整数股，消除浮点误差）
    const v = parseFloat(wan);
    return isNaN(v) ? 0 : Math.round(v * SHARES_PER_WAN);
}

function fmtFee(yuan) {
    // 手续费展示：小额（<1 元，如委托费用均摊值）保留 4 位小数，避免逐笔加总
    // 与委托总额出现舍入尾差；≥1 元仍按 2 位展示
    if (yuan === null || yuan === undefined || isNaN(yuan)) return "--";
    return fmt(yuan, Math.abs(Number(yuan)) < 1 ? 4 : 2);
}

// 金额对外（展示）统一用「万元」，计算与接口传输一律用「元」（在此集中换算）；
// 收益/盈亏类与手续费除外，仍按「元」原样展示（金额小、需精确到元）
function fmtAmtWan(yuan, d = 2) {
    // 元 → 万元显示：100 元 = 0.01 万元，默认 2 位小数（=百元精度）；
    // 非整百金额（如持仓市值）回退 4 位（=元精度）
    if (yuan === null || yuan === undefined || isNaN(yuan)) return "--";
    const wan = Number(yuan) / 10000;
    const nd = Math.abs(wan - Number(wan.toFixed(d))) < 1e-9 ? d : Math.max(d, 4);
    return wan.toLocaleString("zh-CN", { minimumFractionDigits: nd, maximumFractionDigits: nd });
}

// ───────────── Toast ─────────────
function toast(msg, type = "info") {
    const wrap = document.getElementById("toastWrap");
    const el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(() => el.classList.add("show"), 10);
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 2600);
}

// ───────────── 轮次管理视图 ─────────────

function allowSimChecked() {
    const el = document.getElementById("allowSim");
    return !!el && el.checked;
}

function datesUrl(code) {
    // source=1 返回带数据来源标记的日期列表；allow_sim 控制是否纳入转换模拟数据
    const q = ["source=1", "allow_sim=" + (allowSimChecked() ? 1 : 0)];
    if (code) q.unshift("code=" + encodeURIComponent(code));
    return "/api/v1/game/dates?" + q.join("&");
}

async function loadAll() {
    try {
        const [rounds, dates, agent] = await Promise.all([
            api("/api/v1/game/rounds"),
            api(datesUrl("")),
            api("/api/v1/agent/status").catch(() => null),
        ]);
        renderRoundList(rounds.data || []);
        const curCode = (document.getElementById("createCode").value || "").trim() || CODE_DEFAULT;
        const dateArr = (dates.data && typeof dates.data === "object" && !Array.isArray(dates.data))
            ? (dates.data[curCode] || []) : (dates.data || []);
        renderDateOptions(dateArr);
        renderCodeOptions(dates.data);
        renderAgentStatus(agent);
    } catch (e) {
        toast(e.message, "error");
    }
}

// 输入股票代码后按该 code 刷新交易日下拉（含模拟数据日期）
async function reloadDates() {
    const code = (document.getElementById("createCode").value || "").trim() || CODE_DEFAULT;
    try {
        const resp = await api(datesUrl(code));
        renderDateOptions(resp.data || []);
    } catch (e) {
        toast(e.message, "error");
    }
}

document.getElementById("createCode").addEventListener("input", reloadDates);

// 交易日搜索下拉：聚焦/输入展开，点击外部收起
(function () {
    const input = document.getElementById("dateSearch");
    const combo = document.getElementById("dateCombo");
    if (!input || !combo) return;
    input.addEventListener("focus", openDateDropdown);
    input.addEventListener("click", openDateDropdown);
    input.addEventListener("input", () => {
        combo.classList.add("open");
        renderDateDropdown(input.value.trim());
    });
    input.addEventListener("keydown", e => {
        if (e.key === "Escape") closeDateDropdown();
    });
    combo.addEventListener("click", e => e.stopPropagation());
    document.addEventListener("click", closeDateDropdown);
})();

function renderCodeOptions(datesData) {
    const dl = document.getElementById("codeList");
    dl.innerHTML = "";
    if (datesData && typeof datesData === "object" && !Array.isArray(datesData)) {
        Object.keys(datesData).forEach(c => {
            const opt = document.createElement("option");
            opt.value = c;
            dl.appendChild(opt);
        });
    }
}

// 当前标可用的交易日（含数据源标记），供创建面板搜索下拉使用
let DATE_ITEMS = [];

function renderDateOptions(dates) {
    // 归一化为 {trade_date, source}，数据来自后端从数据库选出的可运行日期
    DATE_ITEMS = (dates || []).map(d => (d && typeof d === "object")
        ? { trade_date: d.trade_date, source: d.source === "sim" ? "sim" : "qmt" }
        : { trade_date: d, source: "qmt" });
    const combo = document.getElementById("dateCombo");
    if (combo && combo.classList.contains("open")) {
        const input = document.getElementById("dateSearch");
        renderDateDropdown(input ? input.value.trim() : "");
    }
}

function dateSourceLabel(src) {
    return src === "sim" ? "模拟" : "QMT";
}

function filteredDates(q) {
    if (!q) return DATE_ITEMS;
    const lower = q.toLowerCase();
    return DATE_ITEMS.filter(d => d.trade_date.indexOf(q) >= 0
        || dateSourceLabel(d.source).toLowerCase().indexOf(lower) >= 0);
}

function renderDateDropdown(q) {
    const list = document.getElementById("dateDropdown");
    if (!list) return;
    const items = filteredDates(q || "");
    const rows = [];
    if (!q && DATE_ITEMS.length) {
        // 留空 = 随机：后端从可用日期中自动挑选一个
        rows.push('<div class="combo-item random" onclick="pickDate(\'\')">'
            + '<span>🎲 随机选择</span><span class="combo-hint">后端自动挑一日</span></div>');
    }
    if (items.length) {
        items.forEach(d => rows.push(
            '<div class="combo-item" onclick="pickDate(\'' + d.trade_date + '\')">'
            + '<span class="ci-date">' + d.trade_date + '</span>'
            + '<span class="src-tag ' + d.source + '">' + dateSourceLabel(d.source) + '</span></div>'));
    } else if (DATE_ITEMS.length) {
        rows.push('<div class="combo-empty">无匹配的可用日期</div>');
    } else {
        rows.push('<div class="combo-empty">暂无可用交易日，请先转换/上传行情数据</div>');
    }
    list.innerHTML = rows.join("");
}

function openDateDropdown() {
    const combo = document.getElementById("dateCombo");
    if (!combo || !combo.classList.contains("open")) {
        combo.classList.add("open");
        const input = document.getElementById("dateSearch");
        renderDateDropdown(input ? input.value.trim() : "");
    }
}

function closeDateDropdown() {
    const combo = document.getElementById("dateCombo");
    if (combo) combo.classList.remove("open");
}

function pickDate(v) {
    const input = document.getElementById("dateSearch");
    if (input) input.value = v;
    closeDateDropdown();
}

// Agent 在线状态：HTTP 全量（loadAll/监控面板）+ socket 增量（agent:status）合并渲染
let AGENTS = [];

function renderAgentStatus(agent) {
    AGENTS = ((agent && agent.data) || []).map(a => Object.assign({}, a));
    renderAgentBadge();
}

function onAgentStatus(a) {
    // 后端推送单条状态变化（首次上线/离线恢复/超时离线/角色变更）
    if (!a || !a.agent_name) return;
    const i = AGENTS.findIndex(x => x.agent_name === a.agent_name);
    if (i >= 0) Object.assign(AGENTS[i], a);
    else AGENTS.push(Object.assign({}, a));
    renderAgentBadge();
    if (agentPanelOpen()) refreshAgentPanel();   // 面板打开时同步（补全 age/上传标记）
}

function onAgentRemoved(d) {
    // 后端推送 Agent 被移除（离线清理）
    if (!d || !d.agent_name) return;
    AGENTS = AGENTS.filter(x => x.agent_name !== d.agent_name);
    renderAgentBadge();
    if (agentPanelOpen()) refreshAgentPanel();
}

// 徽标：多 Agent 概要 n/m 在线——全部在线绿 / 部分离线橙 / 全部离线红
function renderAgentBadge() {
    const badge = document.getElementById("agentStatusBadge");
    if (!badge) return;
    if (!AGENTS.length) {
        badge.textContent = "Agent: 无";
        badge.className = "mode-badge";
        badge.title = "Agent 监控：暂无已注册 Agent（点击打开）";
        return;
    }
    const alive = AGENTS.filter(a => a.is_alive).length;
    const total = AGENTS.length;
    badge.textContent = `Agent: ${alive}/${total} 在线`;
    badge.className = "mode-badge " +
        (alive === total ? "alive" : (alive === 0 ? "dead" : "warn"));
    badge.title = "Agent 监控（点击打开）：\n" + AGENTS.map(a =>
        `${a.is_alive ? "●" : "○"} ${a.agent_name}${a.role ? "（" + a.role + "）" : ""} ${a.is_alive ? "在线" : "离线"}`
    ).join("\n");
}

function statusText(s) {
    return { ready: "待开始", running: "进行中", paused: "已暂停", finished: "已结束", aborted: "已终止" }[s] || s;
}

function renderRoundList(rounds) {
    const box = document.getElementById("roundList");
    if (!rounds.length) {
        box.innerHTML = '<div class="empty-state">暂无轮次，先创建一局吧</div>';
        return;
    }
    box.innerHTML = rounds.map(r => `
        <div class="round-card ${r.status}">
            <div class="rc-head">
                <span class="rc-code">${r.code}</span>
                <span class="rc-date">${r.trade_date}</span>
                <span class="src-tag ${r.data_source === "sim" ? "sim" : "qmt"}">${r.data_source === "sim" ? "模拟" : "QMT"}</span>
                <span class="status-badge st-${r.status}">${statusText(r.status)}</span>
                <span class="rc-speed">${r.speed}x</span>
            </div>
            <div class="rc-progress"><div class="progress-fill" style="width:${r.progress || 0}%"></div></div>
            <div class="rc-meta">
                <span>期初资产(万元) <b>${fmtAmtWan(r.initial_assets)}</b></span>
                <span>期末资产(万元) <b>${fmtAmtWan(r.final_assets)}</b></span>
                <span>已实现盈亏(元) <b class="${(r.realized_pnl || 0) >= 0 ? "up" : "down"}">${fmt(r.realized_pnl)}</b></span>
                <span>手续费(元) <b>${fmt(r.fee_total)}</b></span>
            </div>
            <div class="rc-actions">
                <button class="btn-sm btn-primary" onclick="enterGame(${r.id})">进入游戏</button>
                ${r.status === "ready" ? `<button class="btn-sm btn-ok" onclick="startRound(${r.id})">开始</button>` : ""}
                ${r.status === "running" ? `<button class="btn-sm btn-warn" onclick="pauseRound(${r.id})">暂停</button>` : ""}
                ${r.status === "paused" ? `<button class="btn-sm btn-ok" onclick="resumeRound(${r.id})">继续</button>` : ""}
                ${(r.status === "running" || r.status === "paused") ? `
                    <button class="btn-sm" onclick="speedRound(${r.id}, ${r.speed === 1 ? 5 : (r.speed === 5 ? 10 : (r.speed === 10 ? 60 : 1))})">变速→${r.speed === 1 ? 5 : (r.speed === 5 ? 10 : (r.speed === 10 ? 60 : 1))}x</button>
                    <button class="btn-sm btn-warn" onclick="finishRoundFromList(${r.id})">结束</button>` : ""}
                <button class="btn-sm btn-danger" onclick="deleteRound(${r.id})">删除</button>
            </div>
        </div>`).join("");
}

async function createRound() {
    const btn = document.getElementById("btnCreate");
    btn.disabled = true;
    try {
        const body = { allow_sim: allowSimChecked() };
        const code = document.getElementById("createCode").value.trim();
        const date = (document.getElementById("dateSearch").value || "").trim();
        if (code) body.code = code;
        if (date) {
            // 手动输入的日期须在可选范围内（下拉选择或模糊搜索命中）
            if (!DATE_ITEMS.some(d => d.trade_date === date)) {
                toast("交易日 " + date + " 不在可选范围，请从下拉列表中选择", "error");
                return;
            }
            body.trade_date = date;
        }
        // 未选日期（留空）→ 后端随机挑一个可用交易日
        const resp = await api("/api/v1/game/rounds", "POST", body);
        const rid = resp.data.id;
        const srcText = resp.data.data_source === "sim" ? "（模拟数据）" : "";
        // 创建即开始，并直接进入游戏视图（无需二次点击「开始/进入游戏」）
        await api(`/api/v1/game/rounds/${rid}/start`, "POST", {});
        toast(`创建成功 #${rid} ${resp.data.code} ${resp.data.trade_date}${srcText}，游戏已开始`, "success");
        loadAll();
        enterGame(rid);
    } catch (e) {
        toast(e.message, "error");
    } finally {
        btn.disabled = false;
    }
}

async function startRound(id) {
    try {
        await api(`/api/v1/game/rounds/${id}/start`, "POST", {});
        toast("游戏开始", "success");
        loadAll();
        enterGame(id);   // 开始后直接进入游戏视图，无需二次点击
    }
    catch (e) { toast(e.message, "error"); }
}
async function pauseRound(id) {
    try { await api(`/api/v1/game/rounds/${id}/pause`, "POST", {}); toast("已暂停", "info"); loadAll(); }
    catch (e) { toast(e.message, "error"); }
}
async function resumeRound(id) {
    try { await api(`/api/v1/game/rounds/${id}/resume`, "POST", {}); toast("已继续", "success"); loadAll(); }
    catch (e) { toast(e.message, "error"); }
}
async function speedRound(id, speed) {
    try { await api(`/api/v1/game/rounds/${id}/speed`, "POST", { speed }); toast(`速度 ${speed}x`, "info"); loadAll(); }
    catch (e) { toast(e.message, "error"); }
}
async function finishRoundFromList(id) {
    if (!confirm("确定提前结束该轮次并结算？")) return;
    try { await api(`/api/v1/game/rounds/${id}/finish`, "POST", {}); toast("已结算", "success"); loadAll(); }
    catch (e) { toast(e.message, "error"); }
}
async function deleteRound(id) {
    if (!confirm("确定删除该轮次？将级联删除委托与成交记录。")) return;
    try { await api(`/api/v1/game/rounds/${id}`, "DELETE"); toast("已删除", "success"); loadAll(); }
    catch (e) { toast(e.message, "error"); }
}

// ───────────── 进入游戏视图 ─────────────
async function enterGame(roundId) {
    state.roundId = roundId;
    try {
        const detail = await api(`/api/v1/game/rounds/${roundId}`);
        state.round = detail.data;
        // 全量 tick 恢复分时图（不可 tail 截断：进度过半后 tail=3000 只能覆盖
        // 最近 2.5 小时，图会从盘中截断开始，丢失早盘走势）
        const tk = await api(`/api/v1/game/rounds/${roundId}/ticks`);
        state.ticks = tk.data.ticks || [];
        // 进度分母 = 全天可播条数（接口 total；未下发未来快照故不能取 ticks.length）
        state.tickTotal = tk.data.total || state.ticks.length;

        document.getElementById("view-rounds").style.display = "none";
        document.getElementById("view-game").style.display = "block";
        document.title = `StockGame ${state.round.code} ${state.round.trade_date}`;

        renderGameHeader();
        // 行情时间（秒级）：仅进入时按 last_time_key 初始化一次，之后由行情推送驱动
        // （暂停/变速等状态刷新不重置，保证暂停时时间仍可见、不回落）
        const lt0 = state.round.last_time_key || "";
        document.getElementById("gTime").textContent =
            lt0.length >= 19 ? lt0.slice(11, 19) : (lt0.length >= 8 ? lt0.slice(-8) : "--:--:--");
        initChart();
        joinRoundRoom();

        // 新游戏默认打开盘前竞价；行情时间已过 09:40 则保持关闭（之后由用户手动切换）。
        // preAutoClosed 记录"已跨过自动关闭点"，过 09:40 后用户手动打开不会被反复覆盖
        const preAuto = preMarketAutoClose(state.round.last_time_key || "");
        state.preAutoClosed = preAuto;
        setShowPreMarket(!preAuto);

        // 恢复分时图到当前进度（按 last_time_key 截断）
        const lastKey = state.round.last_time_key;
        const upto = lastKey ? state.ticks.filter(t => t.time_key <= lastKey) : [];
        rebuildMinuteSeries(upto, true);

        // 恢复行情显示状态：昨收/今开/今高/今低均按已推进区间重算，与实时
        // 播放保持同一当日累计口径；进入后由行情推送增量更新。极值只纳入
        // 有效价格（竞价时段快照 high/low 为 0，否则今低会被算成 0）
        state.lastClose = upto.length ? (upto[upto.length - 1].last_close || 0) : 0;
        state.dayOpen = upto.length ? upto[0].open : 0;
        const highs = upto.map(t => t.high).filter(validPrice);
        const lows = upto.map(t => t.low).filter(validPrice);
        state.dayHigh = highs.length ? Math.max(...highs) : 0;
        state.dayLow = lows.length ? Math.min(...lows) : 0;
        // 恢复最新价（供持仓/账户市值计算，socket 推送前避免现价显示 0）
        state.lastPrice = state.round.last_price || 0;
        // 恢复进度条（暂停/重进时不依赖行情推送也能显示正确进度）
        const tickTotal = state.tickTotal;
        updateProgress(tickTotal && upto.length ? upto.length / tickTotal * 100 : 0);

        // 加载委托/成交/账户/网格表
        loadOrders();
        loadTrades();
        loadAccount();
        loadGrid();
        refreshQuoteDisplay(true);   // 进入游戏首次完整渲染（含盘口），不节流

        // 若已结束，显示结算信息
        if (state.round.status === "finished") {
            toast(`本轮已结算：期末资产 ${fmtAmtWan(state.round.final_assets)}万元，盈亏 ${fmt(state.round.realized_pnl)}元`, "info");
        }
    } catch (e) {
        toast(e.message, "error");
    }
}

function backToRounds() {
    // 退出轮次房间但保留全局连接（agent 状态推送等仍需实时接收）
    if (state.socket && state.socket.connected && state.roundId) {
        state.socket.emit("leave_round", { round_id: state.roundId });
    }
    state.roundId = null;
    setWsStatus("● 已连接", "ok");   // 全局实时通道保持，非断开
    document.getElementById("view-game").style.display = "none";
    document.getElementById("view-rounds").style.display = "block";
    document.title = "StockGame 股票模拟交易游戏";
    loadAll();
}

function renderGameHeader() {
    const r = state.round;
    document.getElementById("gCode").textContent = r.code;
    document.getElementById("gDate").textContent = r.trade_date;
    const srcEl = document.getElementById("gSource");
    const isSim = r.data_source === "sim";
    srcEl.textContent = isSim ? "模拟" : "QMT";
    srcEl.className = "src-tag " + (isSim ? "sim" : "qmt");
    const badge = document.getElementById("gStatus");
    badge.textContent = statusText(r.status);
    badge.className = "status-badge st-" + r.status;
    document.querySelectorAll(".speed-btn").forEach(b => {
        b.classList.toggle("active", Number(b.dataset.speed) === (r.speed || 1));
    });
    const pauseBtn = document.getElementById("btnPause");
    if (r.status === "running") { pauseBtn.textContent = "⏸ 暂停"; pauseBtn.disabled = false; }
    else if (r.status === "paused") { pauseBtn.textContent = "▶ 继续"; pauseBtn.disabled = false; }
    else { pauseBtn.textContent = "—"; pauseBtn.disabled = true; }
}

// ───────────── Socket ─────────────
function setWsStatus(text, cls) {
    // 更新右上角实时推送连接状态（元素位于轮次管理页顶栏）
    const el = document.getElementById("wsStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "ws-status" + (cls ? " " + cls : "");
}

function initSocket() {
    if (state.socket) return;   // 页面级全局连接：打开即建立，进入/返回游戏不重建
    if (typeof io === "undefined") {
        // socket.io 客户端脚本（CDN）未加载：实时推送不可用，但不阻断页面其它功能
        console.error("socket.io 客户端未加载（检查 CDN 可用性）");
        setWsStatus("● 连接失败", "err");
        return;
    }
    const ws = io();
    state.socket = ws;
    ws.on("connect", () => {
        setWsStatus("● 已连接", "ok");
        // 断线自动重连后若处于游戏视图，重新加入轮次房间恢复推送
        if (state.roundId) ws.emit("join_round", { round_id: state.roundId });
    });
    ws.on("disconnect", () => {
        setWsStatus("● 已断开", "err");
    });
    ws.on("connect_error", () => {
        // socket.io 会自动重连，重连成功后上方 connect 回调恢复"已连接"
        setWsStatus("● 连接失败", "err");
    });
    // 公共事件：agent 上线/离线/移除实时推送（顶栏徽标 + 监控面板）
    ws.on("agent:status", onAgentStatus);
    ws.on("agent:removed", onAgentRemoved);
    // 游戏事件：常驻注册，进入游戏后 join_round 即收到本轮次推送
    ws.on("game:quote", onQuote);
    ws.on("game:order_update", onOrderUpdate);
    ws.on("game:trade", onTrade);
    ws.on("game:account", onAccount);
    ws.on("game:status", onGameStatus);
}

function joinRoundRoom() {
    // 进入游戏：加入轮次房间；socket 尚未连好时由 connect 回调兜底加入
    if (state.socket && state.socket.connected && state.roundId) {
        state.socket.emit("join_round", { round_id: state.roundId });
    }
}

function onQuote(q) {
    if (q.round_id !== state.roundId) return;
    const lastClose = q.last_close || state.lastClose || 0;
    state.lastClose = lastClose;
    state.lastPrice = q.close;
    if (!state.dayOpen && validPrice(q.open)) state.dayOpen = q.open;
    if (validPrice(q.high)) state.dayHigh = Math.max(state.dayHigh || q.high, q.high);
    if (validPrice(q.low)) state.dayLow = state.dayLow ? Math.min(state.dayLow, q.low) : q.low;

    // 分钟聚合：分钟变化 → 定格上一分钟点。quote 的 volume/amount 为与上一
    // 快照的增量（差分输出），同分钟累加 = 该分钟量柱（分钟量能守恒）；
    // 跨分钟定格时 price = 该分钟最后一跳 close（分钟定型价）
    const minute = q.time_key.slice(11, 16);
    if (!state.livePoint || state.livePoint.time !== minute) {
        if (state.livePoint) {
            state.minutePoints.push(state.livePoint);
            if (state.minutePoints.length > 300) state.minutePoints.shift();
        }
        state.livePoint = { time: minute, price: q.close, vol: q.volume || 0, amount: q.amount || 0 };
    } else {
        state.livePoint.price = q.close;      // 尾部跳变点
        state.livePoint.vol += q.volume || 0;
        state.livePoint.amount += q.amount || 0;
    }
    state.cumAmount = q.cum_amount;
    state.cumVolume = q.cum_volume;
    state.lastTickVol = q.volume || 0;   // 当笔 tick 成交量（供盘口模拟用）
    state.lastTk = q.time_key;           // 最新行情时间（供盘前自动关闭兜底）

    // 行情时间首次跨过 09:40 即标记"已自动关闭一次"，并顺带关闭当前打开的盘前段；
    // 之后用户手动切换显示/隐藏不再被反复覆盖
    if (!state.preAutoClosed && preMarketAutoClose(q.time_key)) {
        state.preAutoClosed = true;
        if (state.showPreMarket) setShowPreMarket(false);
    }

    // 行情时间显示（精确到秒）
    const tStr = q.time_key.length >= 19 ? q.time_key.slice(11, 19) : q.time_key.slice(-8);
    const gTimeEl = document.getElementById("gTime");
    if (gTimeEl && gTimeEl.textContent !== tStr) gTimeEl.textContent = tStr;

    updateChart();
    refreshQuoteDisplay();
    updateProgress(q.progress);
    // 行情驱动持仓/账户市值刷新（刷新页面后首次推送即恢复，无需等成交事件）
    if (state.round && state.round.account) {
        renderAccount(state.round.account);
    }
}

function onOrderUpdate(o) {
    if (o.round_id !== state.roundId) return;
    const statusMsg = { filled: "✅ 已成交", cancelled: "已撤单", rejected: "❌ 拒单" };
    if (statusMsg[o.status]) {
        toast(`${o.direction === "buy" ? "买入" : "卖出"} ${fmt(o.shares)}股 ${statusMsg[o.status]}${o.reject_reason ? "：" + o.reject_reason : ""}`,
            o.status === "filled" ? "success" : (o.status === "rejected" ? "error" : "warn"));
    }
    loadOrders();
    refreshGridIfVisible();   // 挂单/撤单/拒单 → 行的格号/间隔可调状态随之变化
}

function onTrade(t) {
    if (t.round_id !== undefined && t.round_id !== state.roundId) return;
    toast(`成交 ${t.direction === "buy" ? "买入" : "卖出"} ${fmt(t.shares)}股 @${t.price}`, "success");
    loadTrades();
    // 账户由随后的 game:account 推送实时刷新，无需再发 HTTP 请求（去冗余）
    refreshGridIfVisible();   // 成交会消/建梯度行
    refreshAnalysisIfVisible();   // 成交改变配对结果
}

// 网格表可见时刷新：该表依赖委托状态（行有无挂单决定格号/间隔能否调整），
// 故成交、下单、撤单、拒单等委托状态变化后都需同步（loadGrid 自带 500ms 节流）
function refreshGridIfVisible() {
    const box = document.getElementById("gridBox");
    if (box && box.style.display !== "none") loadGrid();
}

// 配对收益区可见时刷新（即账户 tab 打开时；成交/结算后配对与收益随之变化）
function refreshAnalysisIfVisible() {
    const box = document.getElementById("analysisBox");
    if (box && box.style.display !== "none") loadAnalysis();
}

function onAccount(acct) {
    if (state.round) state.round.account = acct;
    renderAccount(acct, true);   // 成交推送：立即刷新账户，不节流
}

function onGameStatus(s) {
    if (s.round_id !== state.roundId) return;
    if (s.status) {
        state.round.status = s.status;
        renderGameHeader();
        if (s.status === "finished") {
            toast(`本轮已结束${s.final_assets ? "，期末资产 " + fmtAmtWan(s.final_assets) + "万元" : ""}`, "success");
            loadAll();
            loadAccount();   // 节流后补偿：结束时 force 刷新账户卡，市值/盈亏定格最终价
        }
    }
    if (s.speed) {
        state.round.speed = s.speed;
        renderGameHeader();
    }
}

// ───────────── 分时图（固定交易日轴 + 1分钟聚合 + 尾部跳变） ─────────────
// 完整交易日分钟序列：09:30-11:30 + 13:00-15:00 共 241 个真实分钟 + 1 个午休占位；
// x 轴固定（同花顺式），数据按分钟对齐，未开盘/缺分钟的时段留空不连线，
// 午休占位点断开分时线；合并标签放在占位中点使两侧间隔对称（各 31 idx）
const TRADING_MINUTES = (() => {
    const out = [];
    const p2 = n => String(n).padStart(2, "0");
    // 上午 09:30-11:30（121 分钟）
    for (let h = 9; h <= 11; h++)
        for (let m = (h === 9 ? 30 : 0); m <= (h === 11 ? 30 : 59); m++)
            out.push(p2(h) + ":" + p2(m));
    out.push("午休");   // 午休占位（断线 + 合并标签锚点）
    // 下午 13:00-15:00（121 分钟）
    for (let h = 13; h <= 14; h++)
        for (let m = 0; m <= 59; m++) out.push(p2(h) + ":" + p2(m));
    out.push("15:00");
    return out;   // 121 + 1 + 121 = 243
})();

// 盘前集合竞价段：同花顺式分时图会在开盘（09:30）左侧附加一小段集合竞价区。
// 真实盘前竞价区间为 09:15-09:25（09:25 出竞价价），09:25-09:30 为等待段。
// 盘前段标签与价格在 updateChart 中按真实分钟点动态构建：多点连成竞价折线
// （反映竞价过程形态），点少时以开盘价填充（水平线贯穿竞价区）。
// 首锚点 "竞价" 为纯语义标签（集合竞价区占位），09:30 起无缝接开盘主分时线。
const PRE_MARKET_MINUTES = ["竞价", "09:15", "09:25"];


let _chartResizeBound = false;
let _chartRoPending = false;
function initChart() {
    const el = document.getElementById("minuteChart");
    // 复用已存在实例，避免每次进入游戏重复 init（控制台警告 + 实例泄漏）
    state.chart = echarts.getInstanceByDom(el) || echarts.init(el);
    // resize 监听仅绑定一次，避免多次进入游戏叠加监听器
    if (!_chartResizeBound) {
        window.addEventListener("resize", () => state.chart && state.chart.resize());
        // 容器尺寸随布局变化（横屏媒体查询生效、盘口高度变化等）时不会触发窗口
        // resize，图表会保持旧高度、面板下方留白 → 用 ResizeObserver 跟随容器。
        // 经 rAF 去抖，避免 ResizeObserver 通知循环告警。
        if (window.ResizeObserver) {
            new ResizeObserver(() => {
                if (_chartRoPending) return;
                _chartRoPending = true;
                requestAnimationFrame(() => {
                    _chartRoPending = false;
                    if (state.chart) state.chart.resize();
                });
            }).observe(el);
        }
        _chartResizeBound = true;
    }
}

function buildMinuteSeries(ticks) {
    const points = [];
    let cur = null;
    for (const t of ticks) {
        const minute = t.time_key.slice(11, 16);
        const amount = t.amount || (t.close * t.volume);
        if (!cur || cur.time !== minute) {
            if (cur) points.push(cur);
            cur = { time: minute, price: t.close, vol: t.volume || 0, amount };
        } else {
            cur.price = t.close;
            cur.vol += t.volume || 0;
            cur.amount += amount;
        }
    }
    return { points, live: cur };
}

function rebuildMinuteSeries(ticks, isRecover) {
    // 从 REST 恢复：只保留"已完整"的分钟（最后未完成的分钟作为跳变点）。
    // ticks 的 volume/amount 为相邻快照增量，逐点累加即可还原分钟量柱与
    // 当日累计（cum），与实时路径（onQuote）同口径；high/low 为快照滚动极值，
    // 今高/今低按已推进区间 max/min 重算（见 enterGame）
    const { points, live } = buildMinuteSeries(ticks);
    state.minutePoints = points;
    state.livePoint = live;
    // 累计成交额/量（快照差分累加 = 当日累计）
    let ca = 0, cv = 0;
    ticks.forEach(t => { ca += t.amount || (t.close * (t.volume || 0)); cv += t.volume || 0; });
    state.cumAmount = ca;
    state.cumVolume = cv;
    updateChart();
}

function updateChart() {
    if (!state.chart) return;
    const points = state.minutePoints.concat(state.livePoint ? [state.livePoint] : []);
    const byMin = new Map(points.map(p => [p.time, p]));

    // x 轴：默认 09:30-15:00 主交易日轴；开启盘前后在左侧附加集合竞价段。
    // 盘前段按真实分钟点动态构建：多点连成竞价折线；点少时用开盘价填充铺满
    const showPre = !!state.showPreMarket;
    // 盘前真实分钟点（< 09:30，按时间升序，含实时未完成的 live 点）
    const preRaw = points
        .filter(p => p.time < "09:30")
        .sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    // 开盘价：今开 > 09:30 首点 > 昨收（盘前少点填充用）
    const openP = state.dayOpen
        || (points.find(p => p.time >= "09:30") || {}).price
        || state.lastClose || 0;
    let preLabels, preVals;
    if (showPre && preRaw.length >= 2) {
        // 多点：真实竞价价连线（前导"竞价"锚点用首点价，保证折线连续）
        preLabels = ["竞价", ...preRaw.map(p => p.time)];
        preVals   = [preRaw[0].price, ...preRaw.map(p => p.price)];
    } else {
        // 点少/未开启：固定锚点 + 开盘价填充（水平线贯穿竞价区）；开盘价无效则留空
        const fillP = openP > 0 ? openP : null;
        preLabels = PRE_MARKET_MINUTES;
        preVals   = [fillP, fillP, fillP];
    }
    const preLen = showPre ? preLabels.length : 0;
    const times = showPre ? [...preLabels, ...TRADING_MINUTES] : TRADING_MINUTES;
    const data = times.map(m => byMin.get(m) || null);

    // 与完整 x 轴等长的 series 数据：盘前竞价段与盘中主分时线分离。
    //   盘中价格线只画 09:30 及其后（盘前段留空，不连线到竞价区）；
    //   盘前段用"盘前竞价"series 展示：多点=真实竞价价折线，点少=开盘价填充水平线；
    //   竞价段不参与均价累计（均价线自开盘 09:30 起算）。
    const preIsIdx = new Set();
    for (let i = 0; i < preLen; i++) preIsIdx.add(i);
    const prices = data.map((p, i) => (p && !preIsIdx.has(i) ? p.price : null));
    const vols = data.map((p, i) => (p && !preIsIdx.has(i) ? p.vol : null));
    // 竞价段价格：多点=真实竞价价（折线带点）；点少=开盘价填充（水平线）。
    // 数组按盘前标签索引取 preVals，与盘中各 series（null）对齐
    const prePrices = data.map((p, i) => (preIsIdx.has(i) ? preVals[i] : null));

    // 均价线：自开盘起累计成交额/量（缺分钟跳过，累计不中断）；盘前段不参与
    let ca = 0, cv = 0;
    const avgs = data.map((p, i) => {
        if (p && !preIsIdx.has(i)) { ca += p.amount || (p.price * p.vol); cv += p.vol; }
        return (p && !preIsIdx.has(i)) && cv > 0 ? +(ca / cv).toFixed(4) : null;
    });

    // 昨收基准线自开盘起；盘前段以竞价价作水平基准线（同花顺竞价区显示竞价价
    // 而非昨收），无竞价数据时以昨收兑底
    const lastClose = state.lastClose || (points.length && points[0].price) || 0;
    // 盘前竞价价线由"盘前竞价"series 承载（真实多点折线 / 开盘价填充水平线），
    // 不再叠加单独的水平虚线基准，避免与折线重叠
    const baseLine = data.map((p, i) => (p && !preIsIdx.has(i) ? lastClose : null));
    const upColor = "#e64545", downColor = "#1a9e5c";
    const lastP = points[points.length - 1];
    const prevP = points[points.length - 2];

    state.chart.setOption({
        animation: false,
        // 布局用百分比 + bottom 定位，避免固定像素在不同分辨率下失衡
        grid: [
            { left: 55, right: 16, top: 12, bottom: "25%" },   // 价格图（高度随容器自适应）
            { left: 55, right: 16, height: "17%", bottom: 5 }, // 成交量图（底部对齐）
        ],
        tooltip: {
            trigger: "axis",
            axisPointer: { type: "cross" },
            formatter: (params) => {
                const i = params[0].dataIndex;
                const p = data[i];
                if (!p) return "";
                // 盘前竞价段无均价（自开盘起算），盘口/量能仍可展示
                const isPre = i < preLen;
                const avgTxt = !isPre && avgs[i] !== null ? avgs[i] : "--";
                return `<b>${p.time}</b><br/>价格: ${p.price}<br/>成交量: ${fmtVol(p.vol)}<br/>均价: ${avgTxt}`;
            },
        },
        xAxis: [
            {
                type: "category", data: times, boundaryGap: false,
                axisLine: { lineStyle: { color: "#666" } },
                // 固定刻度：每半小时一个；午休合并标签放在占位中点使两侧等距。
                // 盘前竞价段（前缀）始终显示，「竞价」标签锚在 09:25 处
                axisLabel: {
                    color: "#999", fontSize: 10,
                    interval: (idx) => {
                        if (idx < preLen) return true;       // 盘前竞价段锚点
                        const m = times[idx];
                        if (!m) return false;
                        if (m === "午休") return true;      // 合并标签锚点
                        if (m === "11:30" || m === "13:00") return false;  // 已并入午休标签
                        return m.endsWith(":00") || m.endsWith(":30");
                    },
                    formatter: (val, idx) => {
                        if (idx < preLen) return val === "竞价" ? "竞价" : (val || "");
                        return times[idx] === "午休" ? "11:30/13:00" : val;
                    },
                },
            },
            { type: "category", data: times, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
        ],
        yAxis: [
            {
                type: "value", scale: true,
                splitLine: { lineStyle: { color: "rgba(255,255,255,0.08)" } },
                // 每个价格刻度同时展示当日涨跌幅（基准=昨收，红涨绿跌；昨收无效时仅显示价格）
                axisLabel: {
                    color: "#999", fontSize: 10,
                    rich: {
                        p:    { color: "#999",   fontSize: 10, lineHeight: 13 },
                        up:   { color: upColor,   fontSize: 10, lineHeight: 13 },
                        down: { color: downColor, fontSize: 10, lineHeight: 13 },
                        flat: { color: "#999",   fontSize: 10, lineHeight: 13 },
                    },
                    formatter: (v) => {
                        const lc = state.lastClose;
                        if (!(lc && lc === lc && lc > 0)) return fmt(v, 3);
                        const pct = (v - lc) / lc * 100;
                        const tag = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
                        const sign = pct > 0 ? "+" : "";
                        return `{p|${fmt(v, 3)}}\n{${tag}|${sign}${pct.toFixed(2)}%}`;
                    },
                },
            },
            { type: "value", gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false }, max: v => Math.max(...vols, 1) },
        ],
        dataZoom: [{ type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 }],
        series: [
            {
                name: "价格", type: "line", data: prices, showSymbol: false,
                lineStyle: { width: 1.5, color: "#fff" },
                areaStyle: {
                    color: {
                        type: "linear", x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: "rgba(255,255,255,0.18)" },
                            { offset: 1, color: "rgba(255,255,255,0)" },
                        ],
                    },
                },
            },
            // 盘前集合竞价线：多点=真实竞价价折线（带实点）；点少=开盘价填充的水平线。
            // 无数据（未开启 / 开盘价无效）时不渲染（全部 null）
            { name: "盘前竞价", type: "line", data: prePrices, xAxisIndex: 0, yAxisIndex: 0,
              showSymbol: true, symbol: "circle", symbolSize: 5,
              lineStyle: { width: 1.5, color: "#7aa2f7" },
              itemStyle: { color: "#7aa2f7" }, connectNulls: false },
            { name: "均价", type: "line", data: avgs, showSymbol: false, lineStyle: { width: 1, color: "#f5c542" } },
            { name: "昨收", type: "line", data: baseLine, showSymbol: false, lineStyle: { width: 1, color: "#888", type: "dashed" } },
            { name: "成交量", type: "bar", data: vols, xAxisIndex: 1, yAxisIndex: 1, barWidth: "70%",
              itemStyle: { color: (p) => {
                  const d = data[p.dataIndex];
                  return d ? (d.price >= lastClose ? "rgba(230,69,69,0.55)" : "rgba(26,158,92,0.55)") : "rgba(0,0,0,0)";
              } } },
        ],
    });
}

// ───────────── 盘前竞价段显隐切换 ─────────────
// 判断行情时间是否已到/超过自动关闭盘前竞价的时刻（09:40）
function preMarketAutoClose(timeKey) {
    const hhmm = timeKey && timeKey.length >= 16 ? timeKey.slice(11, 16) : (timeKey || "");
    return !!hhmm && hhmm >= "09:40";
}
// 统一设置盘前竞价段显隐（同步按钮高亮），图表刷新由调用方控制
function setShowPreMarket(show) {
    state.showPreMarket = !!show;
    const btn = document.getElementById("btnPreMarket");
    if (btn) btn.classList.toggle("active", state.showPreMarket);
}
function togglePreMarket() {
    setShowPreMarket(!state.showPreMarket);
    updateChart();
}

// ───────────── 行情显示 ─────────────
function refreshQuoteDisplay(force) {
    const price = state.lastPrice;
    const lastClose = state.lastClose;
    const el = document.getElementById("gLastPrice");
    if (price) {
        el.textContent = fmt(price, 3);
        const chgEl = document.getElementById("gChange");
        const pctEl = document.getElementById("gChangePct");
        // 涨跌幅基准 = 上一交易日收盘价（昨收）；无效时展示 "--"，避免误导
        const baseOk = lastClose && lastClose === lastClose && lastClose > 0;
        if (baseOk) {
            const up = price >= lastClose;
            el.className = "big-price " + (up ? "up" : "down");
            const chg = price - lastClose;
            const pct = chg / lastClose * 100;
            chgEl.textContent = (chg >= 0 ? "+" : "") + fmt(chg, 3);
            pctEl.textContent = (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
            chgEl.className = "chg " + (up ? "up" : "down");
            pctEl.className = "chg " + (up ? "up" : "down");
        } else {
            el.className = "big-price";
            chgEl.textContent = "--";
            chgEl.className = "chg";
            pctEl.textContent = "--";
            pctEl.className = "chg";
        }
    }
    document.getElementById("gOpen").textContent = fmtPx(state.dayOpen);
    document.getElementById("gHigh").textContent = fmtPx(state.dayHigh);
    document.getElementById("gLow").textContent = fmtPx(state.dayLow);
    document.getElementById("gLastClose").textContent = fmtPx(lastClose);
    // 累计量额：尚未产生成交（0/无值）时显示 "-"，不展示占位数字
    document.getElementById("gVol").textContent = state.cumVolume ? fmtVol(state.cumVolume) : "-";
    document.getElementById("gAmount").textContent = state.cumAmount ? fmtAmt(state.cumAmount) : "-";
    renderLevel5(price, force);
}

function updateProgress(pct) {
    // 取整到 1 位小数（与后端 game:quote 的 progress 同口径）：进场恢复时进度由
    // 条数相除得出，未取整会显示成 47.6485891534921% 这类长小数——既不可读，
    // 又会溢出进度文字框把窄屏文档撑出横向滚动
    const v = Math.round((Number(pct) || 0) * 10) / 10;
    document.getElementById("gProgress").style.width = v + "%";
    document.getElementById("gProgressText").textContent = v + "%";
}

// ───────────── 十档盘口（模拟，基于实际行情派生；两列 = 左卖右买） ─────────────
// 十档：买卖各 10 档，两列并列展示（列内自上而下由远及近，与单列版阅读顺序一致）。
// 各档挂单量为模拟值；若当前有未成交委托落在该价位，在价格后标注委托量（万股）。
const LV_LEVELS = 10;

function renderLevel5(price, force) {
    if (!price) return;
    // 高速档节流：盘口为模拟跳动数据，无需每 tick 重建（x60≈60 次/秒）
    const now = Date.now();
    if (!force && now - _lastLv5Ts < RENDER_THROTTLE_MS) return;
    _lastLv5Ts = now;
    const step = 0.001;   // ETF 最小变动价位，价格连续
    // 基础量 = 最近一笔 tick 成交量，乘以随机系数模拟各档挂单量
    const baseVol = state.lastTickVol || Math.max(1000, Math.round(state.cumVolume / 200));
    const box = document.getElementById("level5Rows");
    const mine = _pendingSharesByPrice();   // 未成交委托按价位聚合（股）
    // 随机决定最新价出现在买1还是卖1（模拟主动买/主动卖）
    const atBid = Math.random() < 0.5;
    const asks = [], bids = [];
    for (let i = LV_LEVELS; i >= 1; i--) {   // 卖10 → 卖1
        const p = atBid ? price + step * i : price + step * (i - 1);
        asks.push(_lvRow("卖" + i, p, "ask", "down", baseVol, i, mine));
    }
    for (let i = 1; i <= LV_LEVELS; i++) {   // 买1 → 买10
        const p = atBid ? price - step * (i - 1) : price - step * i;
        bids.push(_lvRow("买" + i, p, "bid", "up", baseVol, i, mine));
    }
    box.innerHTML = `<div class="lv-col">${asks.join("")}</div>`
                  + `<div class="lv-col">${bids.join("")}</div>`;
}

function _lvRow(name, price, side, cls, baseVol, i, mine) {
    const vol = Math.round(baseVol * (1.5 + Math.random() * 3) * (1 + i * 0.15));
    const shares = mine[price.toFixed(3)];
    // 委托量标记：位数占位保证无委托时各列仍对齐
    const tag = shares
        ? `<span class="lv-mine" title="我的委托 ${fmtWan(shares)} 万股">${_fmtWanShort(shares)}</span>`
        : '<span class="lv-mine"></span>';
    return `<div class="lv-row ${side}" onclick="quickPriceByValue(${price})">
        <span class="lv-name">${name}</span><span class="lv-price ${cls}">${fmt(price, 3)}</span>${tag}<span class="lv-vol">${_fmtLvVol(vol)}</span></div>`;
}

// 盘口量显示压缩（两列并排，列宽约 143px）：模拟量取整到万/亿，委托量省去多余小数
function _fmtLvVol(n) {
    if (!n) return "0";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return Math.round(n / 1e4) + "万";
    return String(n);
}

function _fmtWanShort(shares) {
    // 股 → 万股短格式：1.00→1，2.50→2.5，12.34→12.34（单位固定为万股）
    return (Number(shares) / SHARES_PER_WAN).toFixed(2).replace(/\.?0+$/, "") + "万";
}

function _pendingSharesByPrice() {
    // 未成交委托按价位（3 位小数）聚合为股数，供盘口在价格后标注「我的委托」
    const map = {};
    (ordersLastRows || []).forEach(o => {
        if (o.status !== "pending" || !o.price) return;
        const key = Number(o.price).toFixed(3);
        map[key] = (map[key] || 0) + Number(o.shares || 0);
    });
    return map;
}

// ───────────── 下单面板 ─────────────
function switchSide(side) {
    state.side = side;
    document.getElementById("tabBuy").classList.toggle("active", side === "buy");
    document.getElementById("tabSell").classList.toggle("active", side === "sell");
    const btn = document.getElementById("btnSubmit");
    btn.textContent = side === "buy" ? "买入" : "卖出";
    btn.className = "order-submit " + side;
    recalcEstimate();
}

function getStep() {
    return 0.001;   // ETF 最小变动价位
}

function quickPrice(kind) {
    const price = state.lastPrice || 0;
    const step = getStep();
    const map = {
        last: price,
        bid1: price - step,
        ask1: price + step,
        up: price + step,
        down: price - step,
    };
    const v = map[kind];
    if (v > 0) {
        document.getElementById("orderPrice").value = v.toFixed(3);
        recalcEstimate();
    }
}

function quickPriceByValue(v) {
    document.getElementById("orderPrice").value = v.toFixed(3);
    recalcEstimate();
}

// 触屏用价格/数量微调（平板无物理键盘，逐格点按比调出软键盘快）
function nudgePrice(dir) {
    const el = document.getElementById("orderPrice");
    const step = getStep();                       // 最小变动价位 0.001
    const base = parseFloat(el.value) || state.lastPrice || 0;
    if (base <= 0) { toast("暂无最新价，无法微调", "warn"); return; }
    el.value = (base + dir * step).toFixed(3);    // 定 3 位小数，避免浮点尾差
    recalcEstimate();
}

function nudgeShares(dir) {
    const el = document.getElementById("orderShares");
    const cur = parseInt(el.value, 10);
    const base = isNaN(cur) ? 0 : cur;
    el.value = Math.max(1, base + dir);           // 下限 1 万股
    recalcEstimate();
}

function recalcEstimate() {
    const price = parseFloat(document.getElementById("orderPrice").value) || 0;
    const shares = toShares(document.getElementById("orderShares").value);   // 万股 → 股
    const amount = price * shares;
    const fee = amount * FEE_RATE;
    document.getElementById("estAmount").textContent = amount ? fmtAmtWan(amount) : "--";
    document.getElementById("estFee").textContent = amount ? fmt(fee) : "--";
}

async function submitOrder() {
    const price = parseFloat(document.getElementById("orderPrice").value) || 0;
    const shares = toShares(document.getElementById("orderShares").value);   // 万股 → 股
    if (price <= 0) { toast("请输入有效委托价格", "warn"); return; }
    if (shares <= 0 || shares % 100 !== 0) { toast("委托数量须为 0.01 万股（100 股）的整数倍", "warn"); return; }
    // 防连点
    const sb = document.getElementById("btnSubmit");
    sb.disabled = true;
    try {
        const body = {
            direction: state.side,
            order_type: state.otype,
            price,
            shares,
        };
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/order`, "POST", body);
        toast(`委托成功 ${resp.data.order_id}`, "success");
        loadOrders();
        loadAccount();
    } catch (e) {
        toast(e.message, "error");
    } finally {
        setTimeout(() => { sb.disabled = false; }, 800);
    }
}


// ───────────── 顶栏操作 ─────────────
async function setSpeed(speed) {
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/speed`, "POST", { speed });
        state.round.speed = speed;
        renderGameHeader();
    } catch (e) { toast(e.message, "error"); }
}

async function togglePause() {
    try {
        if (state.round.status === "running") {
            await api(`/api/v1/game/rounds/${state.roundId}/pause`, "POST", {});
            state.round.status = "paused";
            toast("已暂停", "info");
        } else if (state.round.status === "paused") {
            await api(`/api/v1/game/rounds/${state.roundId}/resume`, "POST", {});
            state.round.status = "running";
            toast("已继续", "success");
        }
        renderGameHeader();
    } catch (e) { toast(e.message, "error"); }
}

async function finishRound() {
    if (!confirm("确定提前结束本轮并结算？")) return;
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/finish`, "POST", {});
        toast("结算完成", "success");
        const detail = await api(`/api/v1/game/rounds/${state.roundId}`);
        state.round = detail.data;
        renderGameHeader();
        loadOrders();
    } catch (e) { toast(e.message, "error"); }
}

// ───────────── 记录 tab ─────────────
let _lastGridTs = 0;
// 网格表行级状态（保留每行各自调整后的间隔，避免全量重绘时丢失）
let gridRows = [];
let gridLastData = null;
let gridHideDone = false;   // 网格表筛选：隐藏已完成行（仅影响展示）
function switchRecTab(tab) {
    // 限本视图（游戏页）的记录 tab：交易明细是另一视图，其 tab 由 switchTrTab 管理，
    // 用全局 .rec-tab 选择器会连带清掉那边的 active 高亮
    document.querySelectorAll("#view-game .rec-tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.getElementById("ordersToolbar").style.display = tab === "orders" ? "" : "none";
    document.getElementById("ordersTable").style.display = tab === "orders" ? "" : "none";
    document.getElementById("tradesTable").style.display = tab === "trades" ? "" : "none";
    // 账户 tab 含两段：账户卡片 + 配对收益
    const isAcct = tab === "account";
    document.getElementById("accountBox").style.display = isAcct ? "" : "none";
    document.getElementById("analysisBox").style.display = isAcct ? "" : "none";
    document.getElementById("gridBox").style.display = tab === "grid" ? "" : "none";
    if (tab === "grid") { _lastGridTs = 0; loadGrid(); }        // 切 tab 强制刷新
    if (isAcct) loadAnalysis();                                 // 配对收益按需拉取
}

async function loadOrders() {
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/orders`);
        ordersLastRows = resp.data || [];
        renderOrders();
        // 盘口「我的委托」标注随委托变化即时刷新（否则暂停/无 tick 时盘口不重绘）
        renderLevel5(state.lastPrice, true);
    } catch (e) { /* 忽略 */ }
}

// 委托列表筛选：按钮在「隐藏已成 / 显示全部」间切换（仅影响展示，不重新请求）
function toggleHideFilled() {
    ordersHideFilled = !ordersHideFilled;
    renderOrders();
}

function renderOrders() {
    const all = ordersLastRows;
    const rows = ordersHideFilled ? all.filter(o => o.status !== "filled") : all;
    const filledCount = all.filter(o => o.status === "filled").length;
    const btn = document.getElementById("ordersFilterBtn");
    const note = document.getElementById("ordersFilterNote");
    if (btn) btn.textContent = ordersHideFilled ? "显示全部" : "隐藏已成";
    if (note) {
        note.textContent = ordersHideFilled && filledCount
            ? `共 ${all.length} 笔，已隐藏 ${filledCount} 笔已成`
            : `共 ${all.length} 笔`;
    }
    const tb = document.querySelector("#ordersTable tbody");
    if (!tb) return;
    if (!rows.length) {
        tb.innerHTML = `<tr><td colspan="8" class="empty-cell">${
            all.length ? `暂无未成交委托（已隐藏 ${filledCount} 笔已成）` : "暂无委托"}</td></tr>`;
        return;
    }
    tb.innerHTML = rows.map(o => `
        <tr class="order-${o.status}">
            <td>${o.created_at || ""}</td>
            <td class="${o.direction === "buy" ? "up" : "down"}">${o.direction === "buy" ? "买入" : "卖出"}</td>
            <td>${o.order_type === "limit" ? "限价" : "市价"}</td>
            <td>${fmt(o.price, 3)}</td>
            <td>${o.status === "filled" ? fmt(o.filled_price, 3) : "--"}</td>
            <td>${fmtWan(o.shares)}</td>
            <td>${o.status === "pending" ? '<span class="st-pending">已报</span>'
                : o.status === "filled" ? '<span class="st-filled">已成</span>'
                : o.status === "cancelled" ? '<span class="st-cancelled">已撤</span>'
                : `<span class="st-rejected" title="${o.reject_reason || ""}">拒单</span>`}</td>
            <td>${o.status === "pending" ? `<button class="btn-sm btn-warn" onclick="cancelOrder('${o.order_id}')">撤单</button>` : ""}</td>
        </tr>`).join("");
}

async function cancelOrder(orderId) {
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/cancel`, "POST", { order_id: orderId });
        toast("撤单成功", "success");
        loadOrders();
        loadAccount();
        refreshGridIfVisible();   // 撤单后该行恢复可调整
    } catch (e) { toast(e.message, "error"); }
}

// ───────────── 配对收益（并入「账户」tab 下半段） ─────────────
// 配对：同一标的配满 min(买量, 卖量)，卖取最高价、买取最低价逐量对消（余量入
// 「无法匹配」）。手续费取游戏记录自带值（引擎按模拟费率逐笔计费，与上方账户
// 卡的累计手续费同源），故与真实记录的「按委托 min 5 元」口径不同。
async function loadAnalysis() {
    if (!state.roundId) return;
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/analysis`);
        renderAnalysis(resp.data || {});
    } catch (e) { /* 忽略 */ }
}

function renderAnalysis(d) {
    const box = document.getElementById("analysisBox");
    if (!box) return;
    const s = d.summary || {};
    const rnd = d.round || {};
    const netCls = Number(s.net_profit) >= 0 ? "up" : "down";
    const pnlCls = Number(rnd.realized_pnl) >= 0 ? "up" : "down";
    const rate = (rnd.return_rate === null || rnd.return_rate === undefined)
        ? "--" : rnd.return_rate + "%";
    const pairs = d.pairs || [];
    const unmatched = d.unmatched || [];
    const pairRows = pairs.length ? pairs.map(p => `
        <tr>
            <td>${p.buy_time || "--"}</td>
            <td>${fmt(p.buy_price, 3)}</td>
            <td>${p.sell_time || "--"}</td>
            <td>${fmt(p.sell_price, 3)}</td>
            <td>${fmtWan(p.qty)}</td>
            <td>${fmt(p.gross_profit)}</td>
            <td>${fmt(p.buy_fee + p.sell_fee)}</td>
            <td class="${p.net_profit >= 0 ? "up" : "down"}">${fmt(p.net_profit)}</td>
        </tr>`).join("")
        : '<tr><td colspan="8" class="empty-cell">暂无配对（需同日一买一卖）</td></tr>';
    const unRows = unmatched.length ? unmatched.map(u => `
        <tr>
            <td>${u.time || "--"}</td>
            <td class="${dirCls(u.direction)}">${dirText(u.direction)}</td>
            <td>${fmt(u.price, 3)}</td>
            <td>${fmtWan(u.volume)}</td>
            <td>${fmtWan(u.unmatched_volume)}</td>
            <td class="tr-reason">${u.reason || "--"}</td>
        </tr>`).join("")
        : '<tr><td colspan="6" class="empty-cell">全部成交均已配对</td></tr>';

    box.innerHTML = `
        <h4 class="rc-section rc-section-top">配对收益（最大同日收益口径，与真实交易记录同一套配对规则）</h4>
        <div class="acct-grid">
            <div class="acct-item"><span>成交笔数</span><b>${s.count || 0}</b><i>买 ${s.buy_count || 0} / 卖 ${s.sell_count || 0}</i></div>
            <div class="acct-item"><span>配对</span><b>${s.matched_count || 0} 组</b><i>配对量 ${fmtWan(s.matched_volume)} 万股</i></div>
            <div class="acct-item"><span>配对毛收益(元)</span><b>${fmt(s.gross_profit)}</b><i>卖出额 − 买入额</i></div>
            <div class="acct-item"><span>手续费(元)</span><b>${fmt(s.total_fee)}</b><i>已配对 ${fmt(s.matched_fee)} 元</i></div>
            <div class="acct-item"><span>配对净收益(元)</span><b class="${netCls}">${fmt(s.net_profit)}</b><i>毛收益 − 已配对手续费</i></div>
            <div class="acct-item"><span>每日收益率</span><b class="${netCls}">${rate}</b><i>净收益 ÷ 期初资产 ${fmtAmtWan(rnd.initial_assets)} 万元</i></div>
            <div class="acct-item"><span>无法匹配</span><b>${s.unmatched_count || 0} 笔</b><i>留仓买入 / 卖出昨仓</i></div>
            <div class="acct-item"><span>引擎已实现盈亏(元)</span><b class="${pnlCls}">${fmt(rnd.realized_pnl)}</b><i>持仓成本法口径（供对照）</i></div>
        </div>
        <div class="tr-fee-note">配对规则与「交易明细」完全一致：同一标的配满 min(买量, 卖量)，卖取最高价、买取最低价逐量对消（余量列入无法匹配）；
        手续费取本轮成交记录自带值（引擎按模拟费率逐笔计费，与「账户」tab 累计手续费同源），区别于真实记录的「按委托计一次 max(金额×万分之0.85, 5元)」。
        「配对净收益」为最大同日收益口径，「引擎已实现盈亏」为持仓成本法口径，两者存在差异属正常。</div>
        <h4 class="rc-section">配对明细</h4>
        <table class="rec-table">
            <thead><tr><th>买入时间</th><th>买入价</th><th>卖出时间</th><th>卖出价</th><th>数量(万股)</th><th>毛收益(元)</th><th>手续费(元)</th><th>净收益(元)</th></tr></thead>
            <tbody>${pairRows}</tbody>
        </table>
        <h4 class="rc-section">无法匹配</h4>
        <table class="rec-table">
            <thead><tr><th>时间</th><th>方向</th><th>成交价</th><th>数量(万股)</th><th>未匹配数量(万股)</th><th>原因</th></tr></thead>
            <tbody>${unRows}</tbody>
        </table>`;
}

async function loadTrades() {
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/trades`);
        const rows = resp.data || [];
        const tb = document.querySelector("#tradesTable tbody");
        if (!rows.length) {
            tb.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无成交</td></tr>';
            return;
        }
        tb.innerHTML = rows.map(t => `
            <tr>
                <td>${t.trade_time || ""}</td>
                <td class="${t.direction === "buy" ? "up" : "down"}">${t.direction === "buy" ? "买入" : "卖出"}</td>
                <td>${fmt(t.price, 3)}</td>
                <td>${fmtWan(t.shares)}</td>
                <td>${fmt(t.fee)}</td>
            </tr>`).join("");
    } catch (e) { /* 忽略 */ }
}

async function loadAccount() {
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/account`);
        const a = resp.data;
        if (!a) return;
        if (state.round) state.round.account = a;
        // 账户接口携带 last_price，作为轮次详情 last_price 为 0 时的兜底恢复
        if (!state.lastPrice && a.last_price) {
            state.lastPrice = a.last_price;
            refreshQuoteDisplay(true);
        }
        renderAccount(a, true);   // 进入/REST 加载：立即渲染，不节流
    } catch (e) { /* 忽略 */ }
}

function renderAccount(a, force) {
    // 高速档节流：账户卡为 innerHTML 全量重建，无需每 tick 刷新（x60≈60 次/秒）
    const now = Date.now();
    if (!force && now - _lastAcctTs < RENDER_THROTTLE_MS) return;
    _lastAcctTs = now;
    const box = document.getElementById("accountBox");
    const price = state.lastPrice || 0;
    const marketValue = (a.volume || 0) * price;
    const total = (a.available_cash || 0) + (a.frozen_cash || 0) + marketValue;
    const initAssets = state.round && state.round.initial_assets;
    const totalPnl = initAssets ? total - initAssets : 0;
    const floatPnl = price ? (price - (a.avg_price || 0)) * (a.volume || 0) : 0;
    const sellable = Math.max(0, (a.volume || 0) - (a.frozen_volume || 0) - (a.today_bought || 0));
    box.innerHTML = `
        <div class="acct-grid">
            <div class="acct-item"><span>持仓量(万股)</span><b>${fmtWan(a.volume || 0)}</b></div>
            <div class="acct-item"><span>可卖(万股)</span><b>${fmtWan(sellable)}</b></div>
            <div class="acct-item"><span>成本价</span><b>${fmt(a.avg_price || 0, 3)}</b></div>
            <div class="acct-item"><span>浮动盈亏(元)</span><b class="${floatPnl >= 0 ? 'up' : 'down'}">${fmt(floatPnl)}</b></div>
            <div class="acct-item"><span>可用现金(万元)</span><b>${fmtAmtWan(a.available_cash)}</b></div>
            <div class="acct-item"><span>冻结资金(万元)</span><b>${fmtAmtWan(a.frozen_cash)}</b></div>
            <div class="acct-item"><span>持仓市值(万元)</span><b>${fmtAmtWan(marketValue)}</b></div>
            <div class="acct-item"><span>总资产(万元)</span><b>${fmtAmtWan(total)}</b></div>
            <div class="acct-item"><span>期初资产(万元)</span><b>${fmtAmtWan(initAssets)}</b></div>
            <div class="acct-item"><span>总盈亏(元)</span><b class="${totalPnl >= 0 ? 'up' : 'down'}">${fmt(totalPnl)}</b></div>
            <div class="acct-item"><span>已实现盈亏(元)</span><b class="${(a.realized_pnl || 0) >= 0 ? 'up' : 'down'}">${fmt(a.realized_pnl)}</b></div>
            <div class="acct-item"><span>累计手续费(元)</span><b>${fmt(a.fee_total)}</b></div>
        </div>`;
}

// ───────────── 网格表 ─────────────
const GRID_STATUS = {
    pending: { text: "待成交", cls: "st-pending" },
    buy:     { text: "已购", cls: "st-buy" },
    sell:    { text: "已售", cls: "st-sell" },
    done:    { text: "已完成", cls: "st-filled" },
};

async function loadGrid() {
    if (!state.roundId) return;
    // 高速档节流：网格表为 innerHTML 全量重建，无需每 tick 刷新
    const now = Date.now();
    if (now - _lastGridTs < RENDER_THROTTLE_MS) return;
    _lastGridTs = now;
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/grid`);
        const g = resp.data || {};
        gridLastData = g;
        const iv = (g.params && g.params.interval) || 2;
        // 每行附带独立的间隔（默认取全局，可逐行调整）
        gridRows = (g.rows || []).map(r => ({
            ...r,
            interval: (r.interval !== undefined && r.interval !== null) ? r.interval : iv,
        }));
        renderGrid();
    } catch (e) { /* 忽略 */ }
}

// 间隔下拉框取值 → 该行买卖格号/价格重算
function _gridBuyPrice(idx, sp, init) { return +(idx * sp + init).toFixed(3); }
function _gridSellPrice(idx, sp, init, off) { return +(idx * sp + init + off).toFixed(3); }

// 用指定间隔重算某行（方向分侧：买入行固定买格号，卖出行固定卖格号，即主格号 idx）
function _setRowInterval(r, iv, p) {
    const off = Math.max(iv - 1, 0);
    const sp = parseFloat(p.grid_spacing) || 0.005;
    const init = parseFloat(p.init_value) || 0.003;
    const offset = parseFloat(p.offset) || 0.001;
    r.interval = iv;
    if (r.direction === "sell") {
        r.sell_idx = r.idx;
        r.buy_idx = r.idx - off;
        r.sell_price = _gridSellPrice(r.sell_idx, sp, init, offset);
        r.buy_price = _gridBuyPrice(r.buy_idx, sp, init);
    } else {
        r.buy_idx = r.idx;
        r.sell_idx = r.idx + off;
        r.buy_price = _gridBuyPrice(r.buy_idx, sp, init);
        r.sell_price = _gridSellPrice(r.sell_idx, sp, init, offset);
    }
}

// 行可调整性（与后端 save_grid_interval 的校验一致）：
//   - 有未成交委托 → 整行锁定（委托价挂在当前网格线上，改动会使其脱节）
//   - 已完成（双腿均已成交）→ 整行锁定（历史既成事实）
//   - 已成交的进场腿（行方向那一侧）→ 该侧格号锁定，只有未成交的出场腿可调
function _gridRowLock(r) {
    if (r.pending_order_id) return { all: true, entry: true, exit: true, why: "该行已有未成交委托，撤单后可调整" };
    if (r.status === "done") return { all: true, entry: true, exit: true, why: "该行买卖均已成交，不可调整" };
    return { all: false, entry: true, exit: false };   // entry=已成交侧（锁），exit=可调侧
}

async function applyGridInterval(i, val) {
    const r = gridRows[i];
    if (!r || !gridLastData) return;
    const lock = _gridRowLock(r);
    if (lock.all) { renderGrid(); toast(lock.why, "warn"); return; }
    const p = gridLastData.params || {};
    const nv = parseInt(val, 10);
    if (isNaN(nv)) return;
    const old = r.interval;
    _setRowInterval(r, nv, p);   // 乐观更新即时生效
    renderGrid();
    if (!state.roundId) return;
    // 随轮次持久化，重进保持一致
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/grid/interval`, "PUT",
                  { idx: r.idx, interval: nv });
    } catch (e) {
        _setRowInterval(r, old, p);   // 失败回滚
        renderGrid();
        toast(e.message || "间隔保存失败", "error");
    }
}

// 出场腿格号微调：只移动未成交那一侧（买入行→卖出侧、卖出行→买入侧），
// 已成交侧恒不动。该侧位置与间隔一一对应（off_grid = interval - 1），
// 故换算成间隔后复用同一条保存/回滚链路与持久化。
async function applyGridIdx(i, val, side) {
    const r = gridRows[i];
    if (!r || !gridLastData) return;
    const lock = _gridRowLock(r);
    if (lock.all) { renderGrid(); toast(lock.why, "warn"); return; }
    if (side === (r.direction === "sell" ? "sell" : "buy")) {
        renderGrid();                        // 已成交侧：不可调整，还原显示
        toast("该侧网格已成交，只有未成交的出场腿可调整", "warn");
        return;
    }
    const p = gridLastData.params || {};
    const nv = parseInt(val, 10);
    if (isNaN(nv)) { renderGrid(); return; }
    // 出场格号 → 间隔：off_grid = 卖格号 - 买格号（买卖两侧格号差，与方向无关）
    const off = side === "sell" ? nv - r.buy_idx : r.sell_idx - nv;
    const iv = Math.min(Math.max(off + 1, 1), 6);            // 与下拉/后端同域 1-6
    if (iv === r.interval) { renderGrid(); return; }         // 已到边界或未变化
    await applyGridInterval(i, iv);
}

// 网格行一键下单：出场腿待成交（已购/已售）的行按行推导委托（已购→挂卖点卖、
// 已售→挂买点价买回）；已有未成交委托的行按钮置灰，撤单（未成交）后恢复可下。
async function placeGridOrder(i) {
    const r = gridRows[i];
    if (!r || !state.roundId) return;
    if (r.pending_order_id) return;   // 已有挂单（按钮置灰双保险）
    const sellSide = r.direction === "buy";   // 出场腿：已购行卖、已售行买
    const btnText = sellSide ? "卖" : "买";
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/grid/order`, "POST", { idx: r.idx });
        toast(`网格行 ${r.idx} ${btnText}单委托成功`, "success");
    } catch (e) {
        toast(e.message || "一键下单失败", "error");
    }
    loadGrid();   // 刷新行状态（pending_order_id 置灰）
}

// 网格表筛选：在「隐藏已完成 / 显示全部」间切换（仅影响展示，不重新请求）
function toggleGridHideDone() {
    gridHideDone = !gridHideDone;
    renderGrid();
}

function renderGrid() {
    const box = document.getElementById("gridBox");
    if (!box || !gridLastData) return;
    const g = gridLastData;
    const rows = gridRows;
    if (!rows.length) {
        box.innerHTML = '<div class="empty-cell">暂无网格</div>';
        return;
    }
    const p = g.params || {};
    // 参数摘要栏
    const meta = `
        <div class="grid-meta">
            <span>锚点 <b>${fmt(g.anchor_price, 3)}</b></span>
            <span>最新价 <b class="${(g.last_price >= g.anchor_price) ? "up" : "down"}">${fmt(g.last_price, 3)}</b></span>
            <span>总持仓(万股) <b>${fmtWan(g.total_shares)}</b></span>
            <span>间隔 <b>${p.interval}</b>(偏${p.interval - 1}格，可逐行调整)</span>
        </div>`;
    // 价格行高亮：当前最新价所在区间（前档买点 ≥ 现价 ≥ 后档买点）
    let activeIdx = null;
    const last = g.last_price || 0;
    for (let i = 0; i < rows.length; i++) {
        if (last >= rows[i].buy_price) activeIdx = i;
    }
    // 展示行 = [行, 原始下标]：下标必须保留（间隔调整/一键下单均按 gridRows 下标
    // 定位），隐藏已完成仅影响展示，序号列随之保留空档（与行身份一致）
    const doneCount = rows.filter(r => r.status === "done").length;
    const view = rows.map((r, i) => [r, i])
        .filter(([r]) => !gridHideDone || r.status !== "done");
    const toolbar = `<div class="rec-toolbar">
        <button class="btn-sm" onclick="toggleGridHideDone()">${gridHideDone ? "显示全部" : "隐藏已完成"}</button>
        <span class="rec-toolbar-note">${gridHideDone && doneCount
            ? `共 ${rows.length} 行，已隐藏 ${doneCount} 行已完成`
            : `共 ${rows.length} 行`}</span>
    </div>`;
    if (!view.length) {
        box.innerHTML = `${meta}${toolbar}<div class="empty-cell">暂无未完成网格（已隐藏 ${doneCount} 行已完成）</div>`;
        return;
    }
    const trs = view.map(([r, i]) => {
        const st = GRID_STATUS[r.status] || GRID_STATUS.pending;
        // 买卖点是否已触发
        const buyCls = r.buy_hit ? "up" : "";
        const sellCls = r.sell_hit ? "down" : "";
        // 现价恰在此格（买点与卖点之间）→ 高亮行
        const inRange = (last >= r.buy_price && last <= r.sell_price);
        const activeCls = (i === activeIdx || inRange) ? "grid-row-active" : "";
        const dirCls = (r.direction === "sell") ? "down" : "up";
        const dirText = (r.direction === "sell") ? "卖出" : "买入";
        const selOpts = [1, 2, 3, 4, 5, 6].map(v =>
            `<option value="${v}" ${v === r.interval ? "selected" : ""}>${v}</option>`).join("");
        // 可调整性：已成交的进场腿格号锁定（历史既成事实），只有未成交的出场腿
        // 可微调；有未成交委托或已完成的整行锁定（与后端校验一致）
        const lock = _gridRowLock(r);
        const lockTitleAttr = lock.all ? ` title="${lock.why}"` : "";
        // 格号微调控件：数字 + 自绘上下箭头（原生 spinner 为浅色方块，与暗色主题不搭）
        const entrySide = (r.direction === "sell") ? "sell" : "buy";   // 已成交的进场腿
        const stepper = (side, val) => {
            const dis = lock.all || side === entrySide;
            const why = lock.all ? lock.why : "该侧网格已成交，不可调整（只能调未成交的出场腿）";
            const attr = dis ? ` disabled title="${why}"` : ` title="微调出场腿格号：卖点价随格号同步，成交价不变"`;
            return `<div class="grid-idx-box"${attr}>
            <input type="number" class="grid-idx" value="${val}"${attr} onchange="applyGridIdx(${i}, this.value, '${side}')">
            <span class="grid-idx-btns">
                <button type="button" class="gi-btn"${attr} onclick="applyGridIdx(${i}, ${val + 1}, '${side}')">▲</button>
                <button type="button" class="gi-btn"${attr} onclick="applyGridIdx(${i}, ${Math.max(val - 1, 0)}, '${side}')">▼</button>
            </span>
        </div>`;
        };
        const lockAttr = lock.all ? " disabled" : "";
        // 一键下单：出场腿待成交（已购/已售）可下，按钮随出场腿方向显示 买/卖；
        // 该行已有未成交委托 → 置灰（title 提示），撤单后恢复
        const sellSide = r.direction === "buy";   // 出场腿：已购行卖、已售行买
        const orderBtn = (r.status === "buy" || r.status === "sell")
            ? `<button class="grid-order-btn ${sellSide ? "down" : "up"}"
                    ${r.pending_order_id ? `disabled title="已有未成交委托 ${r.pending_order_id}，撤单后可再委托"` : `title="挂${sellSide ? "卖点" : "买点"}价 ${fmt(sellSide ? r.sell_price : r.buy_price, 3)}`}"
                    onclick="placeGridOrder(${i})">${sellSide ? "卖" : "买"}</button>`
            : "";
        return `
            <tr class="${activeCls}">
                <td>${i + 1}</td>
                <td>${stepper("buy", r.buy_idx)}</td>
                <td>${stepper("sell", r.sell_idx)}</td>
                <td><select class="grid-interval"${lockAttr}${lockTitleAttr} onchange="applyGridInterval(${i}, this.value)">${selOpts}</select></td>
                <td class="${dirCls}">${dirText}</td>
                <td class="${buyCls} up">${fmt(r.buy_price, 3)}</td>
                <td class="${sellCls} down">${fmt(r.sell_price, 3)}</td>
                <td class="${r.buy_fill_price != null ? "up" : ""}">${fmt(r.buy_fill_price, 3)}</td>
                <td class="${r.sell_fill_price != null ? "down" : ""}">${fmt(r.sell_fill_price, 3)}</td>
                <td>${fmtWan(r.shares)}</td>
                <td><span class="${st.cls}">${st.text}</span></td>
                <td>${orderBtn}</td>
            </tr>`;
    }).join("");
    box.innerHTML = `
        ${meta}
        ${toolbar}
        <table class="rec-table grid-table">
            <thead><tr><th>序号</th><th>买格号</th><th>卖格号</th><th>间隔</th><th>方向</th><th>买点</th><th>卖点</th><th title="该行买入侧真实成交价（多次成交取加权均价，未成交显示 --）">买成交价</th><th title="该行卖出侧真实成交价（多次成交取加权均价，未成交显示 --）">卖成交价</th><th>配持仓(万股)</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>${trs}</tbody>
        </table>`;
}

// ───────────── 配置浮层 ─────────────
function openConfig(type) {
    // 目前仅网格配置；后续功能参数可扩展 type 分支
    const overlay = document.getElementById("configOverlay");
    overlay.style.display = "flex";
    document.getElementById("configTitle").textContent = "⚙ 网格配置";
    loadGridConfig();
}

function closeConfig() {
    document.getElementById("configOverlay").style.display = "none";
}

async function loadGridConfig() {
    try {
        const resp = await api("/api/v1/game/config/grid");
        const p = resp.data || {};
        const set = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = v; };
        set("cfg_spacing", p.grid_spacing);
        set("cfg_init", p.init_value);
        set("cfg_offset", p.offset);
        set("cfg_ratio", p.interval);
        set("cfg_up", p.grid_up);
        set("cfg_down", p.grid_down);
        set("cfg_tol", p.hit_tolerance);
    } catch (e) { toast(e.message, "error"); }
}

async function saveGridConfig() {
    const body = {
        grid_spacing: Number(document.getElementById("cfg_spacing").value),
        init_value: Number(document.getElementById("cfg_init").value),
        offset: Number(document.getElementById("cfg_offset").value),
        interval: Number(document.getElementById("cfg_ratio").value),
        grid_up: Number(document.getElementById("cfg_up").value),
        grid_down: Number(document.getElementById("cfg_down").value),
        hit_tolerance: Number(document.getElementById("cfg_tol").value),
    };
    try {
        await api("/api/v1/game/config/grid", "PUT", body);
        toast("网格配置已保存", "success");
        // 保存后若正在游戏视图且已打开网格表，立即刷新
        if (state.roundId) loadGrid();
        closeConfig();
    } catch (e) { toast(e.message, "error"); }
}

// 点击浮层外关闭
(function () {
    const ov = document.getElementById("configOverlay");
    if (ov) ov.addEventListener("click", closeConfig);
})();

// ───────────── Agent 监控面板 ─────────────
// 点击顶栏 Agent 徽标打开：多 Agent 在线状态/心跳/最近行情/上传记录，离线可移除
let AGENT_PANEL_DATA = [];       // 最近一次 /status 数据（相对时间本地推算）
let AGENT_PANEL_TICKER = null;   // 相对时间刷新定时器（1s）

function agentPanelOpen() {
    const ov = document.getElementById("agentOverlay");
    return !!ov && ov.style.display !== "none";
}

function openAgentPanel() {
    const ov = document.getElementById("agentOverlay");
    if (!ov) return;
    ov.style.display = "flex";
    backToAgentList();      // 每次打开回到列表视图
    refreshAgentPanel();
    if (!AGENT_PANEL_TICKER) AGENT_PANEL_TICKER = setInterval(updateAgentAges, 1000);
}

function closeAgentPanel() {
    const ov = document.getElementById("agentOverlay");
    if (ov) ov.style.display = "none";
    if (AGENT_PANEL_TICKER) { clearInterval(AGENT_PANEL_TICKER); AGENT_PANEL_TICKER = null; }
}

async function refreshAgentPanel() {
    if (!agentPanelOpen()) return;
    try {
        const resp = await api("/api/v1/agent/status");
        AGENT_PANEL_DATA = (resp.data || []).map(a => {
            a._recvAt = Date.now();     // 本地接收时刻：相对时间在此基准上继续走
            return a;
        });
        AGENTS = AGENT_PANEL_DATA.map(a => Object.assign({}, a));
        renderAgentBadge();
        renderAgentSummary();
        renderAgentList();
    } catch (e) { toast(e.message, "error"); }
}

// 心跳相对时间：服务端 age_sec + 本地流逝时间
function agentAgeSec(a) {
    if (a.age_sec === null || a.age_sec === undefined) return null;
    return a.age_sec + (Date.now() - (a._recvAt || Date.now())) / 1000;
}

function fmtAgo(sec) {
    if (sec === null || sec === undefined) return "--";
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + " 秒前";
    if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
    return Math.floor(sec / 3600) + " 小时前";
}

function updateAgentAges() {
    if (!agentPanelOpen()) return;
    AGENT_PANEL_DATA.forEach(a => {
        const el = document.getElementById("agAge_" + a.agent_name);
        if (el) el.textContent = fmtAgo(agentAgeSec(a));
    });
}

function renderAgentSummary() {
    const el = document.getElementById("agentSummary");
    if (!el) return;
    const total = AGENT_PANEL_DATA.length;
    const alive = AGENT_PANEL_DATA.filter(a => a.is_alive).length;
    if (!total) {
        el.innerHTML = '<span class="ag-state">暂无 Agent</span>' +
            '<span class="ag-note">QMT 脚本上报心跳后自动注册</span>' +
            '<button class="btn-sm" onclick="refreshAgentPanel()">⟳ 刷新</button>';
        return;
    }
    const cls = alive === total ? "ok" : (alive === 0 ? "bad" : "warn");
    const note = alive === total ? "全部运行正常"
        : (alive === 0 ? "全部离线，请检查脚本" : "存在离线 Agent，请检查");
    el.innerHTML = `<span class="ag-state ${cls}">${alive}/${total} 在线</span>` +
        `<span class="ag-note">${note}</span>` +
        '<button class="btn-sm" onclick="refreshAgentPanel()">⟳ 刷新</button>';
}

function renderAgentList() {
    const list = document.getElementById("agentList");
    if (!list) return;
    list.innerHTML = AGENT_PANEL_DATA.map(a => `
        <div class="agent-row ${a.is_alive ? "" : "offline"}">
            <span class="agent-dot ${a.is_alive ? "on" : "off"}"></span>
            <div class="agent-main">
                <div class="agent-name-line">
                    <b>${a.agent_name}</b>
                    ${a.role ? `<span class="agent-role">${a.role}</span>` : ""}
                </div>
                <div class="agent-meta">
                    心跳 <span class="agent-age" id="agAge_${a.agent_name}">${fmtAgo(agentAgeSec(a))}</span>
                    · ${a.last_heartbeat_at || "无记录"}
                    ${a.last_tick_at ? ` · 最近行情 ${a.last_tick_at}` : ""}
                </div>
            </div>
            <div class="agent-actions">
                ${a.has_latest_upload ? `<button class="btn-sm" onclick="showAgentLatest('${a.agent_name}')">上传记录</button>` : ""}
                ${a.is_alive ? "" : `<button class="btn-sm btn-danger" onclick="deleteAgent('${a.agent_name}')">移除</button>`}
            </div>
        </div>`).join("");
}

// 详情子视图：某 Agent 的最近一次行情上传（Redis 保留 25 小时）
async function showAgentLatest(name) {
    document.getElementById("agentSummary").style.display = "none";
    document.getElementById("agentList").style.display = "none";
    const d = document.getElementById("agentDetail");
    d.style.display = "block";
    d.innerHTML = '<div class="latest-loading">⏳ 加载中…</div>';
    try {
        const resp = await api("/api/v1/agent/latest?agent=" + encodeURIComponent(name));
        renderAgentLatest(d, name, resp.data, "");
    } catch (e) {
        renderAgentLatest(d, name, null, e.message || "请求出错");
    }
}

function backToAgentList() {
    const d = document.getElementById("agentDetail");
    if (d) { d.style.display = "none"; d.innerHTML = ""; }
    const s = document.getElementById("agentSummary");
    if (s) s.style.display = "";
    const l = document.getElementById("agentList");
    if (l) l.style.display = "";
}

async function deleteAgent(name) {
    if (!confirm(`确定移除 Agent「${name}」的注册记录？\n（仅离线可删；其心跳与上传记录一并清理，脚本重跑会自动重新注册）`)) return;
    try {
        await api("/api/v1/agent/status/" + encodeURIComponent(name), "DELETE");
        toast(`已移除 ${name}`, "success");
        refreshAgentPanel();
    } catch (e) { toast(e.message, "error"); }
}

function renderAgentLatest(el, name, d, errMsg) {
    const head = `<div class="agent-detail-head">
        <button class="btn-sm" onclick="backToAgentList()">← 返回列表</button>
        <b>${name}</b>
        <span class="agent-desc">最近一次行情上传（Redis 保留 25 小时）</span>
    </div>`;
    if (!d) {
        el.innerHTML = head + `<div class="latest-empty">
            <div class="latest-empty-icon">${errMsg ? "😕" : "📭"}</div>
            <p class="latest-empty-title">${errMsg ? "加载失败" : "暂无上传数据"}</p>
            <p class="latest-empty-tip">${errMsg || "该 Agent 尚未上传行情；上报后即可在此查看。"}</p>
        </div>`;
        return;
    }
    const close = Number(d.close) || 0;
    const lastClose = Number(d.last_close) || 0;
    const up = close >= lastClose;
    const chg = close - lastClose;
    const pct = lastClose ? chg / lastClose * 100 : 0;
    const chgCls = up ? "up" : "down";
    const sign = chg >= 0 ? "+" : "-";
    const tm = d.trade_date || (d.time_key || "").slice(0, 10);
    // 行情时间显示到秒（time_key 为 "YYYY-MM-DD HH:MM:SS"），缺秒时回退到分
    const hhmmss = (d.time_key || "").length >= 19 ? d.time_key.slice(11, 19)
        : ((d.time_key || "").length >= 16 ? d.time_key.slice(11, 16) : (d.time_key || "--:--"));
    el.innerHTML = head + `
        <div class="latest-stock">
            <span class="rc-code">${d.code || "--"}</span>
            <span class="src-tag qmt">QMT</span>
            <span class="latest-date">${tm} ${hhmmss}</span>
        </div>
        <div class="latest-price">
            <span class="big-price ${up ? "up" : "down"}">${fmt(close, 3)}</span>
            <span class="chg-box">
                <span class="chg ${chgCls}">${sign}${fmt(Math.abs(chg), 3)}</span>
                <span class="chg ${chgCls}">${sign}${Math.abs(pct).toFixed(2)}%</span>
            </span>
        </div>
        <div class="latest-grid">
            <div class="acct-item"><span>今开</span><b>${fmtPx(Number(d.open))}</b></div>
            <div class="acct-item"><span>最高</span><b class="up">${fmtPx(Number(d.high))}</b></div>
            <div class="acct-item"><span>最低</span><b class="down">${fmtPx(Number(d.low))}</b></div>
            <div class="acct-item"><span>昨收</span><b>${fmtPx(lastClose)}</b></div>
            <div class="acct-item"><span>成交量</span><b>${fmtVol(Number(d.volume))}</b></div>
            <div class="acct-item"><span>成交额</span><b>${fmtVol(Number(d.amount))}</b></div>
            <div class="acct-item"><span>上传时间</span><b class="latest-up">${d.created_at || "--"}</b></div>
            <div class="acct-item"><span>Agent</span><b>${d.agent_name || "unknown"}</b></div>
        </div>`;
}

// 点击浮层外关闭
(function () {
    const ov = document.getElementById("agentOverlay");
    if (ov) ov.addEventListener("click", closeAgentPanel);
})();

// ───────────── QMT 交易记录视图 ─────────────
// 流程：页面选日期 → 命令直写 Redis（TTL 2 分钟）→ QMT Agent 10s 轮询
// 直读并查询上报 → 服务器删命令 + 整日替换落库 PostgreSQL（trade_records）
// → 页面轮询展示（成交价倒序）；历史日期可经「导入」通道补齐（客户端导出文本）
let TR_POLL_TIMER = null;
const TR_POLL_MS = 3000;      // 命令进度轮询周期（页面侧）
let TR_CMD_TTL_MIN = 2;       // 命令有效期（分钟，取后端 command_ttl_sec）
let TR_CMD_SEEN = null;       // 本页最近观察到的命令 {date, cmd_id}（失效提示用）
let TR_AGG = false;           // 交易明细「按委托聚合」开关（同一委托的拆分成交合并展示）
let TR_LAST_TRADES = [];      // 最近一次交易明细（本地切换聚合视图用，避免重复请求）

function todayStr() {
    const d = new Date();
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function trIsToday(date) {
    return date === todayStr();
}

function openTradeRecords() {
    document.getElementById("view-rounds").style.display = "none";
    document.getElementById("view-traderec").style.display = "block";
    document.title = "StockGame QMT 交易记录";
    const di = document.getElementById("trDate");
    if (di && !di.value) di.value = todayStr();
    loadTradeRecords();
}

function backFromTradeRecords() {
    stopTrPoll();
    document.getElementById("view-traderec").style.display = "none";
    document.getElementById("view-rounds").style.display = "block";
    document.title = "StockGame 股票模拟交易游戏";
    loadAll();
}

function stopTrPoll() {
    if (TR_POLL_TIMER) { clearInterval(TR_POLL_TIMER); TR_POLL_TIMER = null; }
}

function startTrPoll() {
    stopTrPoll();
    // 轮询回调动态读取当前选中日期（切换日期后自动生效）
    TR_POLL_TIMER = setInterval(() => loadTradeRecords(null, true), TR_POLL_MS);
}

async function fetchTradeRecords() {
    const date = document.getElementById("trDate").value;
    if (!date) { toast("请先选择日期", "warn"); return; }
    if (!trIsToday(date)) {
        // 历史日期：不发命令，直接读取数据库（补齐用导入通道）
        await loadTradeRecords();
        toast(`${date}：历史数据来自数据库；如需补齐请用「📥 导入历史」`, "success");
        return;
    }
    try {
        const resp = await api("/api/v1/agent/trade_fetch", "POST", { date });
        TR_CMD_SEEN = { date, cmd_id: (resp.data || {}).cmd_id || "" };
        toast(`${date} 命令已下发（${TR_CMD_TTL_MIN} 分钟内有效），等待 QMT 执行…`, "success");
        setTrStatus("pending", `⏳ ${date} 命令已下发（${TR_CMD_TTL_MIN} 分钟内有效），等待 QMT Agent 执行…`);
        startTrPoll();
    } catch (e) { toast(e.message, "error"); }
}

// ── 导入通道：券商导出成交明细文件直传 → 解析入库（补齐历史日期） ──
let trImportFile = null;   // 待上传文件（.xlsx 工作簿 / .csv、.txt 文本）

function toggleTrImport(show) {
    const p = document.getElementById("trImportPanel");
    if (!p) return;
    const open = show !== undefined ? show : p.style.display === "none";
    p.style.display = open ? "block" : "none";
    if (!open) _resetTrImportFile();
}

function _resetTrImportFile() {
    trImportFile = null;
    const input = document.getElementById("trImportFile");
    if (input) input.value = "";
}

function onTrImportFile(input) {
    const f = input.files && input.files[0];
    if (!f) { trImportFile = null; return; }
    if (/\.xls$/i.test(f.name)) {   // 老版 .xls（非 xlsx）快速失败
        trImportFile = null;
        input.value = "";
        toast("暂不支持老版 .xls，请在客户端导出/另存为 .xlsx", "warn");
        return;
    }
    trImportFile = f;
    toast(`已选择 ${f.name}，点击"解析并导入"上传入库`, "success");
}

async function submitTrImport() {
    if (!trImportFile) { toast("请选择券商导出的成交文件（.xlsx / .csv / .txt）", "warn"); return; }
    const date = document.getElementById("trDate").value;
    const btn = document.getElementById("btnTrImport");
    if (btn) btn.disabled = true;
    try {
        const fd = new FormData();
        fd.append("file", trImportFile);
        if (date) fd.append("date", date);
        const r = await fetch("/api/v1/agent/trade_records/import", { method: "POST", body: fd });
        const resp = await r.json().catch(() => ({}));
        if (resp.code !== 0) throw new Error(resp.message || "导入失败");
        toast(resp.message || "导入成功", "success");
        const days = (resp.data && resp.data.days) || {};
        const dayKeys = Object.keys(days).sort();
        const scope = dayKeys.length > 1
            ? `${dayKeys.length} 个交易日（${dayKeys[0]} ~ ${dayKeys[dayKeys.length - 1]}）`
            : (dayKeys[0] || date);
        toggleTrImport(false);
        setTrStatus("done", `✅ ${scope} 导入成功：${resp.count} 笔（来源：导入）`);
        stopTrPoll();
        await loadTradeRecords();
    } catch (e) {
        toast(e.message, "error");
    } finally {
        if (btn) btn.disabled = false;
    }
}

// 删除当日交易记录（含采集/导入来源，物理删除不可恢复；确认前带出笔数与来源）
async function deleteTrDay() {
    const date = document.getElementById("trDate").value;
    if (!date) { toast("请先选择日期", "warn"); return; }
    let count = 0, source = "";
    try {
        const resp = await api(`/api/v1/agent/trade_fetch?date=${encodeURIComponent(date)}`);
        const r = (resp.data || {}).result;
        if (r && r.success) { count = r.count || 0; source = r.source || ""; }
    } catch (e) { /* 现状查询失败不阻断删除，确认框按未知笔数提示 */ }
    if (!count) { toast(`${date}：当日无记录，无需删除`, "warn"); return; }
    const tip = `确认删除 ${date} 的交易记录？\n\n共 ${count} 笔（${trSourceText(source)}），` +
        `将整日物理删除记录与采集状态，不可恢复。`;
    if (!window.confirm(tip)) return;
    const btn = document.getElementById("btnTrDelete");
    if (btn) btn.disabled = true;
    try {
        const resp = await api(`/api/v1/agent/trade_records/${encodeURIComponent(date)}`, "DELETE");
        toast(resp.message || "删除成功", "success");
        stopTrPoll();
        await loadTradeRecords();
    } catch (e) {
        toast(e.message, "error");
    } finally {
        if (btn) btn.disabled = false;
    }
}

function setTrStatus(state, text) {
    const el = document.getElementById("trStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "tr-status " + state;
}

// 按钮状态随日期/命令同步：today=命令采集；历史日期=纯数据库刷新（不发命令）
function syncTrFetchBtn(date, hasCmd) {
    const btn = document.getElementById("btnFetchTr");
    if (!btn) return;
    if (trIsToday(date)) {
        btn.textContent = "获取交易记录";
        btn.title = "向 QMT 下发命令采集当日成交（命令 2 分钟内有效）";
        btn.disabled = !!hasCmd;
    } else {
        btn.textContent = "🔄 刷新（数据库）";
        btn.title = "历史日期数据读取自数据库；如需补齐请用「📥 导入历史」";
        btn.disabled = false;
    }
}

// 命令存在即"等待执行"（命令有效期内禁点，TTL 到期命令消失后按钮自动恢复）
function renderTrCommand(date, cmd) {
    syncTrFetchBtn(date, !!cmd);
    if (!cmd) return;
    TR_CMD_SEEN = { date, cmd_id: cmd.cmd_id || "" };
    setTrStatus("pending", `⏳ ${date} 命令已下发（${cmd.created_at || ""}，${TR_CMD_TTL_MIN} 分钟内有效），等待 QMT Agent 执行…`);
}

// 结果状态提示（成功/失败）
function finishTrStatus(r) {
    if (!r) return;
    if (r.success === false) {
        setTrStatus("failed", `❌ 获取失败：${r.error || "未知错误"}（${r.fetched_at || ""}）`);
    } else {
        setTrStatus("done", `✅ 获取成功：${r.count} 笔（${r.fetched_at || ""}）`);
    }
}

async function loadTradeRecords(date, polling) {
    if (!date) date = document.getElementById("trDate").value;
    if (!date) return;
    try {
        const resp = await api(`/api/v1/agent/trade_fetch?date=${encodeURIComponent(date)}`);
        const dd = resp.data || {};
        if (dd.command_ttl_sec) TR_CMD_TTL_MIN = Math.round(dd.command_ttl_sec / 60);
        renderTrCommand(date, dd.command);
        if (dd.command) {
            // 命令仍在：有结果先展示（旧数据），本次命令的结果未到则继续轮询
            if (dd.result) renderTradeResult(dd.result);
            const fresh = dd.result && dd.result.cmd_id === dd.command.cmd_id;
            if (fresh) {
                finishTrStatus(dd.result);
                stopTrPoll();
            } else if (!TR_POLL_TIMER) {
                startTrPoll();
            }
        } else {
            // 无命令：已被上报删除（有结果）或 TTL 到期失效（无对应结果）
            const seen = TR_CMD_SEEN && TR_CMD_SEEN.date === date;
            // 结果无 cmd_id（导入/旧数据）时视为匹配，避免误报"命令已失效"
            const matched = !!dd.result && (!seen || !dd.result.cmd_id
                || dd.result.cmd_id === TR_CMD_SEEN.cmd_id);
            if (dd.result) {
                renderTradeResult(dd.result);      // 有结果先展示（可能是上一次的旧数据）
            } else {
                renderTrEmpty(date);
            }
            if (matched) {
                finishTrStatus(dd.result);
            } else if (seen) {
                setTrStatus("failed", `⌛ 命令已失效（超过 ${TR_CMD_TTL_MIN} 分钟未执行），请重新获取`);
            } else if (!dd.result) {
                setTrStatus("", trIsToday(date)
                    ? `选择日期后点击"获取交易记录"（命令 ${TR_CMD_TTL_MIN} 分钟内有效）`
                    : `历史日期（${date}）暂无数据：点击"📥 导入历史"从 QMT 客户端导出补齐`);
            }
            stopTrPoll();
        }
    } catch (e) {
        if (!polling) toast(e.message, "error");
    }
}

function dirText(d) {
    return d === "buy" ? "买入" : (d === "sell" ? "卖出" : "未知");
}
function dirCls(d) {
    return d === "buy" ? "st-buy" : (d === "sell" ? "st-sell" : "");
}
function trSourceText(src) {
    if (src === "import") return "来源：导入";
    if (src === "agent") return "来源：QMT 当日采集";
    return "来源：--";
}

function renderTradeResult(r) {
    const box = document.getElementById("trSummary");
    if (!r.success) {
        box.innerHTML = `<div class="tr-empty tr-error">❌ ${r.date || ""} 获取失败：`
            + `${r.error || "未知错误"}<br>可选择日期后重新点击"获取交易记录"重试</div>`;
        clearTrTables();
        return;
    }
    const s = r.summary || {};
    const netCls = Number(s.net_profit) >= 0 ? "up" : "down";
    box.innerHTML = `
        <div class="acct-grid">
            <div class="acct-item"><span>成交笔数</span><b>${s.count || 0}</b><i>买 ${s.buy_count || 0} / 卖 ${s.sell_count || 0}</i></div>
            <div class="acct-item"><span>买入金额(万元)</span><b>${fmtAmtWan(s.buy_amount)}</b><i>买手续费 ${fmt(s.buy_fee)}元</i></div>
            <div class="acct-item"><span>卖出金额(万元)</span><b>${fmtAmtWan(s.sell_amount)}</b><i>卖手续费 ${fmt(s.sell_fee)}元</i></div>
            <div class="acct-item"><span>配对毛收益(元)</span><b>${fmt(s.gross_profit)}</b><i>配对数 ${s.matched_count || 0}</i></div>
            <div class="acct-item"><span>手续费合计(元)</span><b>${fmt(s.total_fee)}</b><i>已配对 ${fmt(s.matched_fee)}元</i></div>
            <div class="acct-item"><span>当日实际收益(元)</span><b class="${netCls}">${fmt(s.net_profit)}</b><i>已配对净收益（扣双边手续费）</i></div>
            <div class="acct-item"><span>无法匹配</span><b>${s.unmatched_count || 0} 笔</b><i>留仓买入 / 卖出昨仓</i></div>
            <div class="acct-item"><span>数据时间</span><b class="tr-small">${r.fetched_at || "--"}</b><i>${trSourceText(r.source)} · 已入库</i></div>
        </div>
        <div class="tr-fee-note">手续费口径：按委托计一次 max(委托合计成交金额 × 万分之0.85, 5 元)，平均分摊到该委托的每笔成交（买卖各计，不免 5）；当日实际收益 = 配对毛收益 − 已配对手续费。</div>`;
    renderTrTrades(r.trades || []);
    renderTrPairs(r.pairs || []);
    renderTrUnmatched(r.unmatched || []);
}

// 交易明细「按委托聚合」开关：同委托编号（order_id）的拆分成交合并为一行；
// 明细模式末列=成交编号，聚合模式末列=委托编号（列头随模式切换）。
// 按钮文案显示点击后将切换到的视图：明细态→「按委托聚合」，聚合态→「按成交明细」
function toggleTrAgg() {
    TR_AGG = !TR_AGG;
    const btn = document.getElementById("btnTrAgg");
    if (btn) {
        btn.classList.toggle("active", TR_AGG);
        btn.textContent = TR_AGG ? "按成交明细" : "按委托聚合";
        btn.title = TR_AGG
            ? "当前为按委托聚合视图（同一委托的拆分成交已合并为一行），点击切回逐笔成交明细"
            : "按委托编号合并同一笔委托的拆分成交：成交价取数量加权均价，数量/成交金额/手续费逐笔合计";
    }
    const th = document.querySelector("#trTradesTable thead tr th:last-child");
    if (th) th.textContent = TR_AGG ? "委托编号" : "成交编号";
    if (TR_LAST_TRADES.length) renderTrTrades(TR_LAST_TRADES);
}

// 按委托编号聚合：成交价=数量加权均价，数量/金额/手续费逐笔合计；
// 无委托编号的记录（旧导入数据）各自独立成行
function aggTradesByOrder(trades) {
    const groups = [], map = new Map();
    trades.forEach((t, i) => {
        const key = t.order_id || ("#solo#" + i);
        let g = map.get(key);
        if (!g) {
            g = { order_id: t.order_id || "", trade_id: t.trade_id || "",
                  direction: t.direction, time: t.time || "", endTime: t.time || "",
                  volume: 0, amount: 0, fee: 0, _pv: 0, count: 0, price: 0 };
            map.set(key, g);
            groups.push(g);
        }
        const vol = Number(t.volume) || 0;
        g.volume += vol;
        g.amount += Number(t.amount) || 0;
        g.fee += Number(t.fee) || 0;
        g._pv += (Number(t.price) || 0) * vol;
        g.count += 1;
        if (t.time && (!g.time || t.time < g.time)) g.time = t.time;
        if (t.time && (!g.endTime || t.time > g.endTime)) g.endTime = t.time;
    });
    groups.forEach(g => { g.price = g.volume > 0 ? g._pv / g.volume : 0; });
    return groups;
}

// 交易明细：明细按成交价倒序；聚合模式合并同委托后按（加权）价倒序
function renderTrTrades(trades) {
    TR_LAST_TRADES = trades;
    const tb = document.querySelector("#trTradesTable tbody");
    if (!trades.length) {
        tb.innerHTML = '<tr><td colspan="7" class="empty-cell">当日无成交记录</td></tr>';
        return;
    }
    const rows = (TR_AGG ? aggTradesByOrder(trades) : trades.slice())
        .sort((a, b) => (Number(b.price) || 0) - (Number(a.price) || 0));
    tb.innerHTML = rows.map(t => {
        // 聚合行时间：首笔~末笔（同日省略重复日期）；编号列=委托编号（多笔附笔数）
        const timeTxt = (TR_AGG && t.endTime && t.endTime !== t.time)
            ? `${t.time} ~ ${t.endTime.slice(11)}` : (t.time || "--");
        const idTxt = (TR_AGG && t.order_id)
            ? `${t.order_id}${t.count > 1 ? ` (${t.count}笔)` : ""}` : (t.trade_id || "--");
        return `
        <tr>
            <td>${timeTxt}</td>
            <td class="${dirCls(t.direction)}">${dirText(t.direction)}</td>
            <td>${fmt(t.price, 3)}</td>
            <td>${fmtWan(t.volume)}</td>
            <td>${fmtAmtWan(t.amount)}</td>
            <td>${fmtFee(t.fee)}</td>
            <td>${idTxt}</td>
        </tr>`;
    }).join("");
}

// 配对明细按「委托对」聚合展示：同一（买委托×卖委托）的多笔配对合并为一行，
// 大委托拆单配给多个对手时对应多行；每行两侧均为单边委托（含委托编号）
function aggPairsByOrder(pairs) {
    const groups = [], map = new Map();
    pairs.forEach((p, i) => {
        const key = (p.buy_order_id || ("#b#" + i)) + "|" + (p.sell_order_id || ("#s#" + i));
        let g = map.get(key);
        if (!g) {
            g = { buy_order_id: p.buy_order_id || "", sell_order_id: p.sell_order_id || "",
                  buy_time: p.buy_time || "", buy_end: p.buy_time || "",
                  sell_time: p.sell_time || "", sell_end: p.sell_time || "",
                  qty: 0, buy_amount: 0, sell_amount: 0,
                  buy_fee: 0, sell_fee: 0, gross_profit: 0, net_profit: 0 };
            map.set(key, g);
            groups.push(g);
        }
        g.qty += Number(p.qty) || 0;
        g.buy_amount += Number(p.buy_amount) || 0;
        g.sell_amount += Number(p.sell_amount) || 0;
        g.buy_fee += Number(p.buy_fee) || 0;
        g.sell_fee += Number(p.sell_fee) || 0;
        g.gross_profit += Number(p.gross_profit) || 0;
        g.net_profit += Number(p.net_profit) || 0;
        if (p.buy_time && (!g.buy_time || p.buy_time < g.buy_time)) g.buy_time = p.buy_time;
        if (p.buy_time && (!g.buy_end || p.buy_time > g.buy_end)) g.buy_end = p.buy_time;
        if (p.sell_time && (!g.sell_time || p.sell_time < g.sell_time)) g.sell_time = p.sell_time;
        if (p.sell_time && (!g.sell_end || p.sell_time > g.sell_end)) g.sell_end = p.sell_time;
    });
    groups.forEach(g => {
        g.buy_price = g.qty > 0 ? g.buy_amount / g.qty : 0;     // 数量加权均价
        g.sell_price = g.qty > 0 ? g.sell_amount / g.qty : 0;
    });
    return groups;
}

// 时间显示：单点或“首 ~ 末”（跨秒时省略重复日期）
function pairTimeTxt(t, end) {
    if (!t) return "--";
    return (end && end !== t) ? `${t} ~ ${end.slice(11)}` : t;
}

function renderTrPairs(pairs) {
    const tb = document.querySelector("#trPairsTable tbody");
    if (!pairs.length) {
        tb.innerHTML = '<tr><td colspan="8" class="empty-cell">无配对记录</td></tr>';
        return;
    }
    const rows = aggPairsByOrder(pairs);
    tb.innerHTML = rows.map(p => `
        <tr>
            <td>${pairTimeTxt(p.buy_time, p.buy_end)}${p.buy_order_id ? `<br><span class="pair-oid" title="买入委托编号">${p.buy_order_id}</span>` : ""}</td>
            <td>${fmt(p.buy_price, 3)}</td>
            <td>${pairTimeTxt(p.sell_time, p.sell_end)}${p.sell_order_id ? `<br><span class="pair-oid" title="卖出委托编号">${p.sell_order_id}</span>` : ""}</td>
            <td>${fmt(p.sell_price, 3)}</td>
            <td>${fmtWan(p.qty)}</td>
            <td>${fmt(p.gross_profit)}</td>
            <td>${fmt(p.buy_fee + p.sell_fee)}</td>
            <td class="${p.net_profit >= 0 ? "up" : "down"}">${fmt(p.net_profit)}</td>
        </tr>`).join("");
}

function renderTrUnmatched(items) {
    const tb = document.querySelector("#trUnmatchedTable tbody");
    if (!items.length) {
        tb.innerHTML = '<tr><td colspan="6" class="empty-cell">全部成交均已配对</td></tr>';
        return;
    }
    tb.innerHTML = items.map(u => `
        <tr>
            <td>${u.time || "--"}</td>
            <td class="${dirCls(u.direction)}">${dirText(u.direction)}</td>
            <td>${fmt(u.price, 3)}</td>
            <td>${fmtWan(u.volume)}</td>
            <td>${fmtWan(u.unmatched_volume)}</td>
            <td class="tr-reason">${u.reason || "--"}</td>
        </tr>`).join("");
}

function renderTrEmpty(date, text) {
    document.getElementById("trSummary").innerHTML =
        `<div class="tr-empty">${text || `暂无 ${date} 的交易记录：点击"获取交易记录"下发命令（${TR_CMD_TTL_MIN} 分钟内有效），QMT Agent 每 10 秒轮询并回传，结果保留 6 小时`}</div>`;
    clearTrTables();
}

function clearTrTables() {
    TR_LAST_TRADES = [];
    document.querySelector("#trTradesTable tbody").innerHTML =
        '<tr><td colspan="7" class="empty-cell">暂无数据</td></tr>';
    document.querySelector("#trPairsTable tbody").innerHTML =
        '<tr><td colspan="8" class="empty-cell">暂无数据</td></tr>';
    document.querySelector("#trUnmatchedTable tbody").innerHTML =
        '<tr><td colspan="6" class="empty-cell">暂无数据</td></tr>';
}

function switchTrTab(tab) {
    document.querySelectorAll("[data-trtab]").forEach(b =>
        b.classList.toggle("active", b.dataset.trtab === tab));
    document.getElementById("trTradesTable").style.display = tab === "trades" ? "" : "none";
    document.getElementById("trPairsTable").style.display = tab === "pairs" ? "" : "none";
    document.getElementById("trUnmatchedTable").style.display = tab === "unmatched" ? "" : "none";
    // 「按委托聚合」仅作用于交易明细，其他 tab 隐藏
    const aggBtn = document.getElementById("btnTrAgg");
    if (aggBtn) aggBtn.style.display = tab === "trades" ? "" : "none";
}

// 切换日期后自动加载该日结果/命令进度
document.getElementById("trDate").addEventListener("change", () => loadTradeRecords());

// ───────────── 快捷键帮助浮层 ─────────────
function toggleHelp() {
    const el = document.getElementById("helpOverlay");
    el.style.display = el.style.display === "none" ? "flex" : "none";
}

// ───────────── 快捷键 ─────────────
document.addEventListener("keydown", (e) => {
    if (document.getElementById("view-game").style.display === "none") return;
    const tag = document.activeElement && document.activeElement.tagName;
    const inInput = (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT");

    // ? 键显示/关闭帮助（任何情况下都可用）
    if (e.key === "?" || (e.key === "/" && e.shiftKey)) { e.preventDefault(); toggleHelp(); return; }
    // Escape 关闭帮助浮层
    if (e.key === "Escape") {
        const el = document.getElementById("helpOverlay");
        if (el.style.display !== "none") { el.style.display = "none"; return; }
    }
    // 输入框聚焦时不拦截其他快捷键
    if (inInput) return;

    switch (e.key) {
        case " ":
            e.preventDefault(); togglePause(); break;
        case "b": case "B":
            e.preventDefault(); switchSide("buy"); break;
        case "s": case "S":
            e.preventDefault(); switchSide("sell"); break;
        case "ArrowUp":
            e.preventDefault(); quickPrice("up"); break;
        case "ArrowDown":
            e.preventDefault(); quickPrice("down"); break;
        case "Enter":
            e.preventDefault(); submitOrder(); break;
        case "1":
            e.preventDefault(); setSpeed(1); break;
        case "2":
            e.preventDefault(); setSpeed(5); break;
        case "3":
            e.preventDefault(); setSpeed(10); break;
        case "4":
            e.preventDefault(); setSpeed(60); break;
    }
});

// ───────────── 时钟 ─────────────
setInterval(() => {
    document.getElementById("dataTime").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

// 盘前竞价自动关闭兜底：个别 tick 边界漏判时按最新行情时间补一次（过 09:40 即关闭一次）
setInterval(() => {
    if (!state.preAutoClosed && state.lastTk && preMarketAutoClose(state.lastTk)) {
        state.preAutoClosed = true;
        if (state.showPreMarket) setShowPreMarket(false);
    }
}, 500);

// 启动：全局实时连接（agent 上线/离线实时推送）+ 首屏数据加载
initSocket();
loadAll();
