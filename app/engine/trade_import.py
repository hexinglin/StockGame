"""
模块名称: engine/trade_import.py
说明:    成交记录导出文本解析（页面导入通道）

将 QMT 客户端导出的成交明细文本（表格复制粘贴 / CSV 文件）解析为标准成交
记录（与 agent 上报结构一致，直接复用 trade_analysis 分析）。

支持:
- 分隔符自动识别：Tab（客户端右键复制）优先，其次逗号（CSV 导出）；
- 表头自动识别：在前若干行内找含"成交/价格/数量/代码"等关键词的表头行，
  按列名模糊匹配（高优先列名先行，低优先兜底）：时间/代码/名称/方向/价格/
  数量/金额/成交编号/委托编号/交易市场；
- 时间：完整 'YYYY-MM-DD HH:MM:SS' 直取（含 '/' 分隔）；仅时间部分
  （'09:31:15' / '93115' 等）时结合所选日期组装；
- 方向：证券买入/融资买入/买入/买/48/23 → buy；卖出/融券卖出/49/24 → sell；
- 代码：'588000' 无后缀且带"市场"列（上海/深圳）时自动补 .SH/.SZ；
- 容错：跳过空行/合计行/脏行，逐行原因返回 skipped 供页面展示；
- 日期校验：记录时间若带完整日期且与所选日期不符 → 跳过并提示。
"""
import re

# 高优先列名（更精确，先行匹配）
_HIGH = (
    ("time", ("成交时间",)),
    ("price", ("成交价格", "成交均价")),
    ("volume", ("成交数量", "成交量")),
    ("amount", ("成交金额",)),
    ("code", ("证券代码", "股票代码")),
    ("trade_id", ("成交编号",)),
)
# 低优先列名（兜底）
_LOOSE = (
    ("time", ("时间", "日期")),
    ("price", ("价格",)),
    ("volume", ("数量",)),
    ("amount", ("金额", "成交额")),
    ("code", ("代码", "证券编码")),
    ("name", ("名称",)),
    ("direction", ("买卖", "操作", "方向", "类别")),
    ("trade_id", ("成交号",)),
    ("order_id", ("委托编号", "合同编号", "委托号", "委托序号")),
    ("market", ("市场", "交易所")),
)
# 表头行判定关键词（行内含 >=2 个即视为表头候选）
_HEADER_HINTS = ("成交", "价格", "数量", "代码", "时间", "金额", "名称", "买卖", "方向")

_MARKET_MAP = (
    ("上海", ".SH"), ("沪", ".SH"), ("SH", ".SH"), ("1", ".SH"),
    ("深圳", ".SZ"), ("深", ".SZ"), ("SZ", ".SZ"), ("0", ".SZ"),
)


def _split_line(line):
    """按分隔符拆分一行：Tab 优先，其次逗号，最后 2+ 连续空格"""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "," in line:
        return [c.strip().strip('"') for c in line.split(",")]
    if re.search(r"\s{2,}", line):
        return [c.strip() for c in re.split(r"\s{2,}", line)]
    return [line.strip()]


def _pick_delimiter(lines):
    """选表头所在行与分隔符：返回 (表头行号, 列数) 或 None"""
    best = None
    for i, line in enumerate(lines[:30]):
        cells = _split_line(line)
        if len(cells) < 3:
            continue
        hit = sum(1 for c in cells
                  if any(h in str(c) for h in _HEADER_HINTS))
        if hit >= 2 and (best is None or hit > best[2]):
            best = (i, len(cells), hit)
    return best


def _match_header(cells):
    """表头单元格 → 字段名映射（高优先先行，低优先兜底，同字段先到先得）"""
    mapping = {}
    assigned = set()

    def _try(table):
        for idx, cell in enumerate(cells):
            if idx in mapping:
                continue
            c = str(cell)
            for field, kws in table:
                if field in assigned:
                    continue
                if any(kw in c for kw in kws):
                    mapping[idx] = field
                    assigned.add(field)
                    break

    _try(_HIGH)
    _try(_LOOSE)
    return mapping


def _norm_dt(v, date):
    """时间列 → 'YYYY-MM-DD HH:MM:SS'；仅时间时结合 date；失败返回空串"""
    t = str(v or "").strip()
    if not t:
        return ""
    t = t.replace("/", "-")
    # 完整形态
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2}):(\d{2})", t)
    if m:
        return "%s-%s-%s %02d:%s:%s" % (m.group(1), m.group(2), m.group(3),
                                        int(m.group(4)), m.group(5), m.group(6))
    # 紧凑日期前缀 'YYYYMMDD HH:MM:SS'
    m = re.match(r"^(\d{8})[ T](\d{1,2}):(\d{2}):(\d{2})", t)
    if m:
        d = m.group(1)
        return "%s-%s-%s %02d:%s:%s" % (d[:4], d[4:6], d[6:8],
                                        int(m.group(2)), m.group(3), m.group(4))
    # 仅时间 'H:MM:SS' / 'HH:MM' / '93115' / '093115'
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
    if m:
        return "%s %02d:%s:%s" % (date, int(m.group(1)), m.group(2),
                                  m.group(3) or "00")
    if t.isdigit() and 1 <= len(t) <= 6:
        t6 = t.rjust(6, "0")
        return "%s %s:%s:%s" % (date, t6[:2], t6[2:4], t6[4:6])
    return ""


def _norm_direction(v):
    """方向归一：买入类→buy，卖出类→sell，其余 unknown"""
    s = str(v or "").strip()
    if not s:
        return "unknown"
    if s in ("48", "23"):
        return "buy"
    if s in ("49", "24"):
        return "sell"
    if "买" in s:
        return "buy"
    if "卖" in s:
        return "sell"
    low = s.lower()
    if low in ("buy", "b"):
        return "buy"
    if low in ("sell", "s"):
        return "sell"
    return "unknown"


def _norm_code(code, market):
    """代码归一：无后缀且有市场列时补 .SH/.SZ"""
    c = str(code or "").strip().upper()
    if not c:
        return ""
    if "." in c:
        return c
    m = str(market or "").strip()
    if m:
        for kw, suffix in _MARKET_MAP:
            if kw in m:
                return c + suffix
    return c


def parse_export_text(text, date):
    """解析导出文本 → (records, meta)

    records: 标准成交记录列表（字段与 agent 上报一致）
    meta: {parsed, skipped, header_line, total_lines}
          skipped: [{'line': 行号(1 起), 'reason': 原因, 'text': 原文摘要}]
    """
    records = []
    skipped = []
    lines = [ln for ln in str(text or "").replace("\r\n", "\n").split("\n")]
    if not any(ln.strip() for ln in lines):
        return [], {"parsed": 0, "skipped": skipped,
                    "header_line": 0, "total_lines": 0}

    found = _pick_delimiter(lines)
    if found is None:
        raise ValueError(
            "未找到表头行（需包含 成交/价格/数量/代码 等列名；"
            "请从 QMT 表格右键复制后粘贴，或导出 CSV）")
    header_idx = found[0]
    mapping = _match_header(_split_line(lines[header_idx]))
    if "code" not in mapping.values() or "price" not in mapping.values() \
            or "volume" not in mapping.values():
        raise ValueError(
            "表头缺少必需列（证券代码/成交价格/成交数量），识别到的列: %s"
            % list(mapping.values()))

    for i in range(header_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        lineno = i + 1
        cells = _split_line(line)
        # 合计/汇总行
        if any(k in line for k in ("合计", "总计", "汇总")) \
                and not str(cells[0] if cells else "").strip().isdigit():
            skipped.append({"line": lineno, "reason": "合计/汇总行",
                            "text": line[:80]})
            continue

        def _val(field):
            for idx, f in mapping.items():
                if f == field and idx < len(cells):
                    return cells[idx]
            return ""

        rec_time = _norm_dt(_val("time"), date)
        if rec_time and rec_time[:10] != date:
            skipped.append({"line": lineno,
                            "reason": "日期与所选不符(%s)" % rec_time[:10],
                            "text": line[:80]})
            continue
        code = _norm_code(_val("code"), _val("market"))
        try:
            volume = int(float(str(_val("volume")).replace(",", "") or 0))
        except ValueError:
            volume = 0
        try:
            price = float(str(_val("price")).replace(",", "") or 0)
        except ValueError:
            price = 0.0
        try:
            amount = float(str(_val("amount")).replace(",", "") or 0)
        except ValueError:
            amount = 0.0
        if not code or volume <= 0:
            skipped.append({"line": lineno, "reason": "代码或数量缺失",
                            "text": line[:80]})
            continue
        if amount <= 0:
            amount = round(price * volume, 2)
        records.append({
            "time": rec_time or ("%s 00:00:00" % date),
            "code": code,
            "name": str(_val("name") or "")[:50],
            "direction": _norm_direction(_val("direction")),
            "price": price,
            "volume": volume,
            "amount": round(amount, 2),
            "trade_id": str(_val("trade_id") or "")[:50],
            "order_id": str(_val("order_id") or "")[:50],
        })

    return records, {"parsed": len(records), "skipped": skipped,
                     "header_line": header_idx + 1, "total_lines": len(lines)}
