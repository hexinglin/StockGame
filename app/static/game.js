/* ============================================================
 * StockGame 前端逻辑 — 轮次管理 + 游戏视图（分时图/盘口/下单）
 * ============================================================ */
"use strict";

// ───────────── 全局状态 ─────────────
const state = {
    roundId: null,          // 当前游戏轮次
    round: null,            // 轮次详情
    ticks: [],              // 该日全部快照（恢复用；volume/amount 为相邻快照增量）
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

// 账户卡/五档盘口为 innerHTML 全量重建，高速档每 tick 重建（x60≈60 次/秒）开销大且
// 视觉抖动，按时间节流；x1（1 tick/秒，间隔 > 阈值）不受影响。成交/进入等关键场景
// 传 force=true 立即渲染。行情数字与分时图仍每 tick 更新（textContent 轻量、需实时）。
const RENDER_THROTTLE_MS = 500;
let _lastAcctTs = 0, _lastLv5Ts = 0;

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

// Agent 在线状态：HTTP 全量（loadAll）+ socket 增量（agent:status）合并渲染
let AGENTS = [];

function renderAgentStatus(agent) {
    AGENTS = ((agent && agent.data) || []).map(a => Object.assign({}, a));
    renderAgentBadge();
}

function onAgentStatus(a) {
    // 后端推送单条状态变化（首次上线/离线恢复/超时离线）
    if (!a || !a.agent_name) return;
    const i = AGENTS.findIndex(x => x.agent_name === a.agent_name);
    if (i >= 0) Object.assign(AGENTS[i], a);
    else AGENTS.push(Object.assign({}, a));
    renderAgentBadge();
}

function renderAgentBadge() {
    const badge = document.getElementById("agentStatusBadge");
    if (!badge) return;
    if (!AGENTS.length) {
        badge.textContent = "Agent: 无";
        badge.className = "mode-badge";
        return;
    }
    badge.textContent = "Agent: " + AGENTS
        .map(a => `${a.agent_name} ${a.is_alive ? "在线" : "离线"}`)
        .join(" / ");
    // 任一 agent 离线即整体警示（红色），全部在线为绿色
    badge.className = "mode-badge " +
        (AGENTS.some(a => !a.is_alive) ? "dead" : "alive");
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
                <span>期初资产 <b>${fmt(r.initial_assets)}</b></span>
                <span>期末资产 <b>${fmt(r.final_assets)}</b></span>
                <span>已实现盈亏 <b class="${(r.realized_pnl || 0) >= 0 ? "up" : "down"}">${fmt(r.realized_pnl)}</b></span>
                <span>手续费 <b>${fmt(r.fee_total)}</b></span>
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
        // 播放保持同一当日累计口径；进入后由行情推送增量更新
        state.lastClose = upto.length ? (upto[upto.length - 1].last_close || 0) : 0;
        state.dayOpen = upto.length ? upto[0].open : 0;
        state.dayHigh = upto.length ? Math.max(...upto.map(t => t.high || 0)) : 0;
        state.dayLow = upto.length ? Math.min(...upto.map(t => t.low || 0)) : 0;
        // 恢复最新价（供持仓/账户市值计算，socket 推送前避免现价显示 0）
        state.lastPrice = state.round.last_price || 0;
        // 恢复进度条（暂停/重进时不依赖行情推送也能显示正确进度）
        const tickTotal = state.ticks.length;
        updateProgress(tickTotal && upto.length ? upto.length / tickTotal * 100 : 0);

        // 加载委托/成交/账户/网格表
        loadOrders();
        loadTrades();
        loadAccount();
        loadGrid();
        refreshQuoteDisplay(true);   // 进入游戏首次完整渲染（含盘口），不节流

        // 若已结束，显示结算信息
        if (state.round.status === "finished") {
            toast(`本轮已结算：期末资产 ${fmt(state.round.final_assets)}，盈亏 ${fmt(state.round.realized_pnl)}`, "info");
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
    // 公共事件：agent 上线/离线实时推送（轮次管理页顶栏徽标）
    ws.on("agent:status", onAgentStatus);
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
    if (!state.dayOpen) state.dayOpen = q.open;
    state.dayHigh = Math.max(state.dayHigh || q.high, q.high);
    state.dayLow = state.dayLow ? Math.min(state.dayLow, q.low) : q.low;

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
}

function onTrade(t) {
    if (t.round_id !== undefined && t.round_id !== state.roundId) return;
    toast(`成交 ${t.direction === "buy" ? "买入" : "卖出"} ${fmt(t.shares)}股 @${t.price}`, "success");
    loadTrades();
    // 账户由随后的 game:account 推送实时刷新，无需再发 HTTP 请求（去冗余）
    if (document.getElementById("gridBox").style.display !== "none") loadGrid();
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
            toast(`本轮已结束${s.final_assets ? "，期末资产 " + fmt(s.final_assets) : ""}`, "success");
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
function initChart() {
    const el = document.getElementById("minuteChart");
    // 复用已存在实例，避免每次进入游戏重复 init（控制台警告 + 实例泄漏）
    state.chart = echarts.getInstanceByDom(el) || echarts.init(el);
    // resize 监听仅绑定一次，避免多次进入游戏叠加监听器
    if (!_chartResizeBound) {
        window.addEventListener("resize", () => state.chart && state.chart.resize());
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
    document.getElementById("gOpen").textContent = fmt(state.dayOpen, 3);
    document.getElementById("gHigh").textContent = fmt(state.dayHigh, 3);
    document.getElementById("gLow").textContent = fmt(state.dayLow, 3);
    document.getElementById("gLastClose").textContent =
        lastClose && lastClose > 0 ? fmt(lastClose, 3) : "--";
    document.getElementById("gVol").textContent = fmtVol(state.cumVolume);
    document.getElementById("gAmount").textContent = fmt(state.cumAmount);
    renderLevel5(price, force);
}

function updateProgress(pct) {
    document.getElementById("gProgress").style.width = (pct || 0) + "%";
    document.getElementById("gProgressText").textContent = (pct || 0) + "%";
}

// ───────────── 五档盘口（模拟，基于实际行情派生） ─────────────
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
    let html = "";
    // 随机决定最新价出现在买1还是卖1（模拟主动买/主动卖）
    const atBid = Math.random() < 0.5;
    // 卖 5→1
    for (let i = 5; i >= 1; i--) {
        const p = atBid ? price + step * i : price + step * (i - 1);
        const vol = Math.round(baseVol * (1.5 + Math.random() * 3) * (1 + i * 0.15));
        html += `<div class="lv-row ask" onclick="quickPriceByValue(${p})">
            <span class="lv-name">卖${i}</span><span class="lv-price down">${fmt(p, 3)}</span><span class="lv-vol">${fmtVol(vol)}</span></div>`;
    }
    // 买 1→5
    for (let i = 1; i <= 5; i++) {
        const p = atBid ? price - step * (i - 1) : price - step * i;
        const vol = Math.round(baseVol * (1.5 + Math.random() * 3) * (1 + i * 0.15));
        html += `<div class="lv-row bid" onclick="quickPriceByValue(${p})">
            <span class="lv-name">买${i}</span><span class="lv-price up">${fmt(p, 3)}</span><span class="lv-vol">${fmtVol(vol)}</span></div>`;
    }
    box.innerHTML = html;
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

function recalcEstimate() {
    const price = parseFloat(document.getElementById("orderPrice").value) || 0;
    const shares = parseInt(document.getElementById("orderShares").value) || 0;
    const amount = price * shares;
    const fee = amount * FEE_RATE;
    document.getElementById("estAmount").textContent = amount ? fmt(amount) : "--";
    document.getElementById("estFee").textContent = amount ? fmt(fee) : "--";
}

async function submitOrder() {
    const price = parseFloat(document.getElementById("orderPrice").value) || 0;
    const shares = parseInt(document.getElementById("orderShares").value) || 0;
    if (price <= 0) { toast("请输入有效委托价格", "warn"); return; }
    if (shares <= 0 || shares % 100 !== 0) { toast("委托数量必须为 100 的整数倍", "warn"); return; }
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
function switchRecTab(tab) {
    document.querySelectorAll(".rec-tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.getElementById("ordersTable").style.display = tab === "orders" ? "" : "none";
    document.getElementById("tradesTable").style.display = tab === "trades" ? "" : "none";
    document.getElementById("accountBox").style.display = tab === "account" ? "" : "none";
    document.getElementById("gridBox").style.display = tab === "grid" ? "" : "none";
    if (tab === "grid") { _lastGridTs = 0; loadGrid(); }   // 切 tab 强制刷新
}

async function loadOrders() {
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/orders`);
        const rows = resp.data || [];
        const tb = document.querySelector("#ordersTable tbody");
        if (!rows.length) {
            tb.innerHTML = '<tr><td colspan="8" class="empty-cell">暂无委托</td></tr>';
            return;
        }
        tb.innerHTML = rows.map(o => `
            <tr class="order-${o.status}">
                <td>${o.created_at || ""}</td>
                <td class="${o.direction === "buy" ? "up" : "down"}">${o.direction === "buy" ? "买入" : "卖出"}</td>
                <td>${o.order_type === "limit" ? "限价" : "市价"}</td>
                <td>${fmt(o.price, 3)}</td>
                <td>${o.status === "filled" ? fmt(o.filled_price, 3) : "--"}</td>
                <td>${fmt(o.shares)}</td>
                <td>${o.status === "pending" ? '<span class="st-pending">已报</span>'
                    : o.status === "filled" ? '<span class="st-filled">已成</span>'
                    : o.status === "cancelled" ? '<span class="st-cancelled">已撤</span>'
                    : `<span class="st-rejected" title="${o.reject_reason || ""}">拒单</span>`}</td>
                <td>${o.status === "pending" ? `<button class="btn-sm btn-warn" onclick="cancelOrder('${o.order_id}')">撤单</button>` : ""}</td>
            </tr>`).join("");
    } catch (e) { /* 忽略 */ }
}

async function cancelOrder(orderId) {
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/cancel`, "POST", { order_id: orderId });
        toast("撤单成功", "success");
        loadOrders();
        loadAccount();
    } catch (e) { toast(e.message, "error"); }
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
                <td>${fmt(t.shares)}</td>
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
            <div class="acct-item"><span>持仓量</span><b>${fmt(a.volume || 0)}</b></div>
            <div class="acct-item"><span>可卖</span><b>${fmt(sellable)}</b></div>
            <div class="acct-item"><span>成本价</span><b>${fmt(a.avg_price || 0, 3)}</b></div>
            <div class="acct-item"><span>浮动盈亏</span><b class="${floatPnl >= 0 ? 'up' : 'down'}">${fmt(floatPnl)}</b></div>
            <div class="acct-item"><span>可用现金</span><b>${fmt(a.available_cash)}</b></div>
            <div class="acct-item"><span>冻结资金</span><b>${fmt(a.frozen_cash)}</b></div>
            <div class="acct-item"><span>持仓市值</span><b>${fmt(marketValue)}</b></div>
            <div class="acct-item"><span>总资产</span><b>${fmt(total)}</b></div>
            <div class="acct-item"><span>期初资产</span><b>${fmt(initAssets)}</b></div>
            <div class="acct-item"><span>总盈亏</span><b class="${totalPnl >= 0 ? 'up' : 'down'}">${fmt(totalPnl)}</b></div>
            <div class="acct-item"><span>已实现盈亏</span><b class="${(a.realized_pnl || 0) >= 0 ? 'up' : 'down'}">${fmt(a.realized_pnl)}</b></div>
            <div class="acct-item"><span>累计手续费</span><b>${fmt(a.fee_total)}</b></div>
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

async function applyGridInterval(i, val) {
    const r = gridRows[i];
    if (!r || !gridLastData) return;
    const p = gridLastData.params || {};
    const nv = parseInt(val, 10);
    if (isNaN(nv)) return;
    const old = r.interval;
    _setRowInterval(r, nv, p);   // 乐观更新即时生效
    renderGrid();
    if (!state.roundId) return;
    // 随轮次持久化，重进保持一致
    try {
        await api(`/api/v1/game/rounds/${state.roundId}/grid/interval`, "PUT", { idx: r.idx, interval: nv });
    } catch (e) {
        _setRowInterval(r, old, p);   // 失败回滚
        renderGrid();
        toast(e.message || "间隔保存失败", "error");
    }
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
            <span>总持仓 <b>${fmt(g.total_shares)}</b></span>
            <span>间隔 <b>${p.interval}</b>(偏${p.interval - 1}格，可逐行调整)</span>
        </div>`;
    // 价格行高亮：当前最新价所在区间（前档买点 ≥ 现价 ≥ 后档买点）
    let activeIdx = null;
    const last = g.last_price || 0;
    for (let i = 0; i < rows.length; i++) {
        if (last >= rows[i].buy_price) activeIdx = i;
    }
    const trs = rows.map((r, i) => {
        const st = GRID_STATUS[r.status] || GRID_STATUS.pending;
        // 买卖点是否已触发
        const buyCls = r.buy_hit ? "up" : "";
        const sellCls = r.sell_hit ? "down" : "";
        // 现价恰在此格（买点与卖点之间）→ 高亮行
        const inRange = (last >= r.buy_price && last <= r.sell_price);
        const activeCls = (i === activeIdx || inRange) ? "grid-row-active" : "";
        const dirCls = (r.direction === "sell") ? "down" : "up";
        const dirText = (r.direction === "sell") ? "卖出" : "买入";
        const selOpts = [1, 2, 4, 6].map(v =>
            `<option value="${v}" ${v === r.interval ? "selected" : ""}>${v}</option>`).join("");
        return `
            <tr class="${activeCls}">
                <td>${i + 1}</td>
                <td>${r.buy_idx}</td>
                <td>${r.sell_idx}</td>
                <td><select class="grid-interval" onchange="applyGridInterval(${i}, this.value)">${selOpts}</select></td>
                <td class="${dirCls}">${dirText}</td>
                <td class="${buyCls} up">${fmt(r.buy_price, 3)}</td>
                <td class="${sellCls} down">${fmt(r.sell_price, 3)}</td>
                <td>${fmt(r.shares)}</td>
                <td><span class="${st.cls}">${st.text}</span></td>
            </tr>`;
    }).join("");
    box.innerHTML = `
        ${meta}
        <table class="rec-table grid-table">
            <thead><tr><th>序号</th><th>买格号</th><th>卖格号</th><th>间隔</th><th>方向</th><th>买点</th><th>卖点</th><th>配持仓</th><th>状态</th></tr></thead>
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
