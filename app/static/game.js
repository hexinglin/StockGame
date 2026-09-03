/* ============================================================
 * StockGame 前端逻辑 — 轮次管理 + 游戏视图（分时图/盘口/下单）
 * ============================================================ */
"use strict";

// ───────────── 全局状态 ─────────────
const state = {
    roundId: null,          // 当前游戏轮次
    round: null,            // 轮次详情
    ticks: [],              // 该日全部 tick（恢复用）
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
    
};

const FEE_RATE = 0.0001;    // 万1
const CODE_DEFAULT = "588000.SH";

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

function renderAgentStatus(agent) {
    const badge = document.getElementById("agentStatusBadge");
    if (!agent || !agent.data || !agent.data.length) {
        badge.textContent = "Agent: 无";
        badge.className = "mode-badge";
        return;
    }
    const a = agent.data[0];
    badge.textContent = `Agent: ${a.agent_name} ${a.is_alive ? "在线" : "离线"}`;
    badge.className = "mode-badge " + (a.is_alive ? "alive" : "dead");
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
                    <button class="btn-sm" onclick="speedRound(${r.id}, ${r.speed === 1 ? 10 : (r.speed === 10 ? 60 : 1)})">变速→${r.speed === 1 ? 10 : (r.speed === 10 ? 60 : 1)}x</button>
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
        initSocket();

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

        // 加载委托/成交/账户
        loadOrders();
        loadTrades();
        loadAccount();
        refreshQuoteDisplay();

        // 若已结束，显示结算信息
        if (state.round.status === "finished") {
            toast(`本轮已结算：期末资产 ${fmt(state.round.final_assets)}，盈亏 ${fmt(state.round.realized_pnl)}`, "info");
        }
    } catch (e) {
        toast(e.message, "error");
    }
}

function backToRounds() {
    if (state.socket) state.socket.disconnect();
    state.socket = null;
    state.roundId = null;
    setWsStatus("● 未连接");   // 主动返回：无实时通道，非故障
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
    if (state.socket) { state.socket.disconnect(); }
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
        ws.emit("join_round", { round_id: state.roundId });
    });
    ws.on("disconnect", () => {
        setWsStatus("● 已断开", "err");
    });
    ws.on("connect_error", () => {
        // socket.io 会自动重连，重连成功后上方 connect 回调恢复"已连接"
        setWsStatus("● 连接失败", "err");
    });
    ws.on("game:quote", onQuote);
    ws.on("game:order_update", onOrderUpdate);
    ws.on("game:trade", onTrade);
    ws.on("game:account", onAccount);
    ws.on("game:status", onGameStatus);
}

function onQuote(q) {
    if (q.round_id !== state.roundId) return;
    const lastClose = q.last_close || state.lastClose || 0;
    state.lastClose = lastClose;
    state.lastPrice = q.close;
    if (!state.dayOpen) state.dayOpen = q.open;
    state.dayHigh = Math.max(state.dayHigh || q.high, q.high);
    state.dayLow = state.dayLow ? Math.min(state.dayLow, q.low) : q.low;

    // 分钟聚合：分钟变化 → 定格上一分钟点
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
    loadAccount();
}

function onAccount(acct) {
    if (state.round) state.round.account = acct;
    renderAccount(acct);
}

function onGameStatus(s) {
    if (s.round_id !== state.roundId) return;
    if (s.status) {
        state.round.status = s.status;
        renderGameHeader();
        if (s.status === "finished") {
            toast(`本轮已结束${s.final_assets ? "，期末资产 " + fmt(s.final_assets) : ""}`, "success");
            loadAll();
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

function initChart() {
    const el = document.getElementById("minuteChart");
    state.chart = echarts.init(el);
    window.addEventListener("resize", () => state.chart && state.chart.resize());
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
    // 从 REST 恢复：只保留"已完整"的分钟（最后未完成的分钟作为跳变点）
    const { points, live } = buildMinuteSeries(ticks);
    state.minutePoints = points;
    state.livePoint = live;
    // 累计成交额/量
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

    // 固定 x 轴：完整交易日 241 分钟；未开盘/缺分钟的时段为 null（不连线）
    const times = TRADING_MINUTES;
    const data = times.map(m => byMin.get(m) || null);
    const prices = data.map(p => (p ? p.price : null));
    const vols = data.map(p => (p ? p.vol : null));

    // 均价线：按交易日顺序累计成交额/量（缺分钟跳过，累计不中断）
    let ca = 0, cv = 0;
    const avgs = data.map(p => {
        if (p) { ca += p.amount || (p.price * p.vol); cv += p.vol; }
        return p && cv > 0 ? +(ca / cv).toFixed(4) : null;
    });

    const lastClose = state.lastClose || (points.length && points[0].price) || 0;
    const baseLine = data.map(p => (p ? lastClose : null));
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
                return `<b>${p.time}</b><br/>价格: ${p.price}<br/>成交量: ${fmtVol(p.vol)}<br/>均价: ${avgs[i] !== null ? avgs[i] : "--"}`;
            },
        },
        xAxis: [
            {
                type: "category", data: times, boundaryGap: false,
                axisLine: { lineStyle: { color: "#666" } },
                // 固定刻度：每半小时一个；午休合并标签放在占位中点使两侧等距
                axisLabel: {
                    color: "#999", fontSize: 10,
                    interval: (idx) => {
                        const m = times[idx];
                        if (!m) return false;
                        if (m === "午休") return true;      // 合并标签锚点
                        if (m === "11:30" || m === "13:00") return false;  // 已并入午休标签
                        return m.endsWith(":00") || m.endsWith(":30");
                    },
                    formatter: (val, idx) => (times[idx] === "午休" ? "11:30/13:00" : val),
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

// ───────────── 行情显示 ─────────────
function refreshQuoteDisplay() {
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
    renderLevel5(price);
}

function updateProgress(pct) {
    document.getElementById("gProgress").style.width = (pct || 0) + "%";
    document.getElementById("gProgressText").textContent = (pct || 0) + "%";
}

// ───────────── 五档盘口（模拟，基于实际行情派生） ─────────────
function renderLevel5(price) {
    if (!price) return;
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

function quickShares(kind) {
    const acct = state.round && state.round.account;
    const price = parseFloat(document.getElementById("orderPrice").value) || state.lastPrice || 0;
    let shares = 0;
    if (kind === "q1" || kind === "half" || kind === "full") {
        // 按可用资金/价格 估算可买数量（买入），或按可卖持仓（卖出）
        if (state.side === "buy" && acct) {
            const avail = (acct.available_cash - (acct.frozen_cash || 0)) || 0;
            const maxShares = price > 0 ? Math.floor(avail / (price * (1 + FEE_RATE)) / 100) * 100 : 0;
            const ratio = kind === "q1" ? 0.25 : (kind === "half" ? 0.5 : 1);
            shares = Math.floor(maxShares * ratio / 100) * 100;
        } else if (state.side === "sell" && acct) {
            const sellable = Math.max(0, (acct.volume || 0) - (acct.frozen_volume || 0) - (acct.today_bought || 0));
            const ratio = kind === "q1" ? 0.25 : (kind === "half" ? 0.5 : 1);
            shares = Math.floor(sellable * ratio / 100) * 100;
        }
    } else if (kind === "sellable") {
        if (acct) shares = Math.max(0, (acct.volume || 0) - (acct.frozen_volume || 0) - (acct.today_bought || 0));
    } else if (kind === "step") {
        shares = (parseInt(document.getElementById("orderShares").value) || 0) + 10000;
    } else if (kind === "unstep") {
        shares = Math.max(0, (parseInt(document.getElementById("orderShares").value) || 10000) - 10000);
    }
    if (shares > 0) {
        document.getElementById("orderShares").value = shares;
        recalcEstimate();
    } else {
        toast("当前无法计算该仓位（可用资金/持仓为 0）", "warn");
    }
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
function switchRecTab(tab) {
    document.querySelectorAll(".rec-tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
    document.getElementById("ordersTable").style.display = tab === "orders" ? "" : "none";
    document.getElementById("tradesTable").style.display = tab === "trades" ? "" : "none";
    document.getElementById("accountBox").style.display = tab === "account" ? "" : "none";
}

async function loadOrders() {
    try {
        const resp = await api(`/api/v1/game/rounds/${state.roundId}/orders`);
        const rows = resp.data || [];
        const tb = document.querySelector("#ordersTable tbody");
        if (!rows.length) {
            tb.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无委托</td></tr>';
            return;
        }
        tb.innerHTML = rows.map(o => `
            <tr class="order-${o.status}">
                <td>${o.created_at || ""}</td>
                <td class="${o.direction === "buy" ? "up" : "down"}">${o.direction === "buy" ? "买入" : "卖出"}</td>
                <td>${o.order_type === "limit" ? "限价" : "市价"}</td>
                <td>${fmt(o.price, 3)}</td>
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
            refreshQuoteDisplay();
        }
        renderAccount(a);
    } catch (e) { /* 忽略 */ }
}

function renderAccount(a) {
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
            <div class="acct-item"><span>已实现盈亏</span><b class="${(state.round.realized_pnl || 0) >= 0 ? 'up' : 'down'}">${fmt(state.round.realized_pnl)}</b></div>
            <div class="acct-item"><span>累计手续费</span><b>${fmt(state.round.fee_total)}</b></div>
        </div>`;
}

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
            e.preventDefault(); setSpeed(10); break;
        case "3":
            e.preventDefault(); setSpeed(60); break;
    }
});

// ───────────── 时钟 ─────────────
setInterval(() => {
    document.getElementById("dataTime").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}, 1000);

// 启动
loadAll();
