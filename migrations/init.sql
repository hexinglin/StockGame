-- =============================================================================
-- StockGame 数据库初始化脚本（幂等，可重复执行）
-- 说明: 仅含建表 DDL，不含 CREATE DATABASE（由 migrate_db.py 动态执行）
-- 架构: 行情快照化（2026-09）— QMT 推送为"当日某时刻的股票快照"而非 3s
--       bar：close=最新价、high/low=截至该时刻的当日滚动极值、volume/amount=
--       当日累计量额。tick 表逐点如实记录快照；day_kline 记录当日真实终值
--       （volume/amount=末条快照累计值）；今开 open / 昨收 last_close 为当日
--       常量，统一在 day_kline 维护（tick 表不存）。
-- =============================================================================

-- 当日快照点序列（游戏数据源；每个 (code, time_key) 仅一条）
-- 字段口径（快照语义）: close=最新价；high/low=截至该时刻的当日滚动最高/最低；
-- volume/amount=截至该时刻的当日累计成交量/额（单调不减）。游戏播放时对
-- volume/amount 取相邻快照差分还原分钟量能，high/low 原样透传驱动今高/今低。
-- 注意: 昨收 last_close / 今开 open 为当日常量，统一维护于 day_kline 表。
CREATE TABLE IF NOT EXISTS tick_data (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  high REAL, low REAL, close REAL,    -- 快照：当日滚动最高/最低 + 最新价
  volume BIGINT NOT NULL DEFAULT 0,      -- 当日累计成交量（截至该快照时刻）
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,  -- 当日累计成交额（可达亿级，需双精度）
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_data_trade_date ON tick_data (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_data_code_date ON tick_data (code, trade_date);

-- 模拟快照行情（stockkline 1min → 3s 等间隔快照流，QMT 无该日数据时的兜底数据源；
-- 结构同 tick_data，均为当日快照口径）
CREATE TABLE IF NOT EXISTS tick_data_sim (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  high REAL, low REAL, close REAL,    -- 快照：当日滚动最高/最低 + 最新价
  volume BIGINT NOT NULL DEFAULT 0,      -- 当日累计成交量（截至该快照时刻）
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,  -- 当日累计成交额
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_sim_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_sim_trade_date ON tick_data_sim (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_sim_code_date ON tick_data_sim (code, trade_date);

-- 天维度真实行情（每 code+trade_date+data_source 一条，与 tick 表对齐的当日终值）
-- 开局原始数据源：tick 快照入库时同步聚合写入（与 tick 表对账）。
-- 口径（快照语义）:
--   open=今开（当日常量，随上传/生成方 hint 维护，缺失时首条 close 兑底）
--   high/low=当日最高/最低（max/min 各快照滚动极值）；close=当日最新收盘价
--   volume/amount=当日累计终值（末条快照累计值，非逐点求和）
--   last_close=昨收（当日常量，随生成方携带，缺失时从 stock_kline 回补）
-- 昨收必须为有效正数：空/0/NaN 视为异常日行情，写入前拦截拒绝（不入库），
-- 表 CHECK 约束兜底（见文件尾部昨收防线段）。
CREATE TABLE IF NOT EXISTS day_kline (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  data_source VARCHAR(10) NOT NULL DEFAULT 'qmt',  -- qmt(实盘)/sim(转换模拟)
  open REAL, high REAL, low REAL, close REAL,      -- 日级 OHLC（快照口径终值）
  volume BIGINT NOT NULL DEFAULT 0,        -- 当日累计成交量终值（末条快照）
  amount DOUBLE PRECISION NOT NULL DEFAULT 0,  -- 当日累计成交额终值（末条快照）
  last_close REAL NOT NULL,   -- 昨收（上一交易日收盘，必须有效 >0，无默认值防异常入库）
  tick_count INT NOT NULL DEFAULT 0,       -- 该日已入库快照条数（对齐）
  first_time_key VARCHAR(19) NOT NULL DEFAULT '',  -- 首条快照时间（对齐）
  last_time_key VARCHAR(19) NOT NULL DEFAULT '',   -- 末条快照时间（对齐）
  is_complete BOOLEAN NOT NULL DEFAULT FALSE,  -- 完整交易日（末条快照 >= 15:00:00）
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_day_kline_code_date_src UNIQUE (code, trade_date, data_source)
);
CREATE INDEX IF NOT EXISTS ix_day_kline_code_date ON day_kline (code, trade_date);
CREATE INDEX IF NOT EXISTS ix_day_kline_complete ON day_kline (is_complete);

-- 可开局交易日索引（游戏选择/管理层；每 code+trade_date+data_source 一条）
-- 不存行情数据：is_complete 由 day_kline 生成时派生同步，开局原始数据从
-- day_kline 读取；round_count 为游戏侧状态（该日已创建轮次数），不随行情重刷丢失。
CREATE TABLE IF NOT EXISTS game_days (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  data_source VARCHAR(10) NOT NULL DEFAULT 'qmt',  -- qmt(实盘)/sim(转换模拟)
  is_complete BOOLEAN NOT NULL DEFAULT FALSE,  -- 可开局（末条快照 >= 15:00:00）
  round_count INT NOT NULL DEFAULT 0,       -- 该日已创建轮次数
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_game_day_code_date_src UNIQUE (code, trade_date, data_source)
);
CREATE INDEX IF NOT EXISTS ix_game_days_code_date ON game_days (code, trade_date);
CREATE INDEX IF NOT EXISTS ix_game_days_complete ON game_days (is_complete);

-- QMT 心跳状态
CREATE TABLE IF NOT EXISTS agent_status (
  id SERIAL PRIMARY KEY,
  agent_name VARCHAR(50) UNIQUE NOT NULL,
  last_heartbeat_at TIMESTAMP,
  last_tick_at TIMESTAMP,
  is_alive BOOLEAN DEFAULT true,
  updated_at TIMESTAMP DEFAULT now()
);

-- 游戏轮次（一个轮次 = 一个交易日的完整游戏周期）
CREATE TABLE IF NOT EXISTS game_rounds (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) DEFAULT '588000.SH',  -- 游戏标的
  trade_date VARCHAR(10) NOT NULL,
  status VARCHAR(20) DEFAULT 'ready', -- ready/running/paused/finished/aborted
  speed INT DEFAULT 1,                -- 1/10/60
  data_source VARCHAR(10) DEFAULT 'qmt', -- 行情数据源: qmt(实盘)/sim(转换模拟)
  created_at TIMESTAMP DEFAULT now(),
  started_at TIMESTAMP, finished_at TIMESTAMP,
  initial_cash REAL, base_shares INT,
  initial_assets REAL, final_assets REAL,
  realized_pnl REAL DEFAULT 0,        -- 已实现盈亏
  fee_total REAL DEFAULT 0,
  last_price REAL, last_time_key VARCHAR(19),
  account_json TEXT          -- 账户快照 JSON（Redis 之外的 DB 兜底）
  -- 同一 code+交易日同时只允许一个未结束轮次（ready/running/paused），由应用层校验
);
CREATE INDEX IF NOT EXISTS ix_game_rounds_code_date ON game_rounds (code, trade_date);
CREATE INDEX IF NOT EXISTS ix_game_rounds_status ON game_rounds (status);

-- 委托（状态机 pending/filled/cancelled/rejected，整单成交）
CREATE TABLE IF NOT EXISTS game_orders (
  id BIGSERIAL PRIMARY KEY,
  order_id VARCHAR(50) UNIQUE NOT NULL,
  round_id INT NOT NULL REFERENCES game_rounds(id) ON DELETE CASCADE,
  code VARCHAR(20), direction VARCHAR(10),   -- buy/sell
  order_type VARCHAR(10) DEFAULT 'limit',    -- limit/market
  price REAL, shares INT,
  frozen_amount REAL DEFAULT 0,   -- 下单冻结金额（买单，含手续费）
  status VARCHAR(20) DEFAULT 'pending',
  filled_shares INT DEFAULT 0, filled_price REAL DEFAULT 0,
  fee REAL DEFAULT 0,
  created_at TIMESTAMP DEFAULT now(), filled_at TIMESTAMP,
  reject_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_game_orders_round_id ON game_orders (round_id);
CREATE INDEX IF NOT EXISTS ix_game_orders_status ON game_orders (status);

-- 成交记录
CREATE TABLE IF NOT EXISTS game_trades (
  id BIGSERIAL PRIMARY KEY,
  round_id INT NOT NULL REFERENCES game_rounds(id) ON DELETE CASCADE,
  order_id VARCHAR(50), code VARCHAR(20), direction VARCHAR(10),
  price REAL, shares INT, fee REAL,
  trade_time VARCHAR(19)
);
CREATE INDEX IF NOT EXISTS ix_game_trades_round_id ON game_trades (round_id);

-- 存量刷新（幂等）：day_kline 统计/对齐字段按 tick 表现存快照重算
-- （仅更新已存在行，不插新行：day_kline 行由行情上传时 refresh_day 写入——
-- 昨收无效的异常日拒绝入库，此处无昨收来源，无法也不应补插；缺失行请先补
-- 有效昨收后由 refresh_day/refresh_all_days 重建）。
-- 快照口径：open 不覆盖（无 hint 来源，保留库内值，无效时以首条 close 兜底）；
-- high=max(各快照滚动 high)/low=min(low)；close=末条 close；volume/amount=
-- 末条快照累计值（array_agg DESC 取末条），绝不可 sum（快照量额为累计值）。
UPDATE day_kline d SET
  tick_count = s.cnt, first_time_key = s.t0, last_time_key = s.t1,
  is_complete = s.cmp,
  open = COALESCE(NULLIF(d.open, 0), s.o),
  high = s.h, low = s.l, close = s.c,
  volume = s.v, amount = s.a, updated_at = now()
FROM (
  SELECT code, trade_date, count(*) AS cnt, min(time_key) AS t0,
         max(time_key) AS t1, (max(time_key) >= trade_date || ' 15:00:00') AS cmp,
         (array_agg(close ORDER BY time_key))[1] AS o,   -- 首条 close（今开兜底）
         max(high) AS h, min(low) AS l,
         (array_agg(close ORDER BY time_key DESC))[1] AS c,
         (array_agg(volume ORDER BY time_key DESC))[1] AS v,
         (array_agg(amount ORDER BY time_key DESC))[1] AS a
  FROM tick_data GROUP BY code, trade_date
) s
WHERE d.code = s.code AND d.trade_date = s.trade_date AND d.data_source = 'qmt';

UPDATE day_kline d SET
  tick_count = s.cnt, first_time_key = s.t0, last_time_key = s.t1,
  is_complete = s.cmp,
  open = COALESCE(NULLIF(d.open, 0), s.o),
  high = s.h, low = s.l, close = s.c,
  volume = s.v, amount = s.a, updated_at = now()
FROM (
  SELECT code, trade_date, count(*) AS cnt, min(time_key) AS t0,
         max(time_key) AS t1, (max(time_key) >= trade_date || ' 15:00:00') AS cmp,
         (array_agg(close ORDER BY time_key))[1] AS o,
         max(high) AS h, min(low) AS l,
         (array_agg(close ORDER BY time_key DESC))[1] AS c,
         (array_agg(volume ORDER BY time_key DESC))[1] AS v,
         (array_agg(amount ORDER BY time_key DESC))[1] AS a
  FROM tick_data_sim GROUP BY code, trade_date
) s
WHERE d.code = s.code AND d.trade_date = s.trade_date AND d.data_source = 'sim';

-- 选择/管理层派生：game_days 可开局标记随 day_kline 同步（round_count 不覆盖）
INSERT INTO game_days (code, trade_date, data_source, is_complete)
SELECT code, trade_date, data_source, is_complete FROM day_kline
ON CONFLICT (code, trade_date, data_source) DO UPDATE SET
  is_complete = EXCLUDED.is_complete,
  updated_at = now();

-- 昨收防线（幂等）：last_close 无效（空/0/NaN/Infinity）的日行情属异常数据，
-- 物理删除不入库；关联的 game_days 选择行（该日无轮次占用）同步清理，
-- 保证选择/管理层与 day_kline 派生一致
DELETE FROM day_kline
WHERE last_close IS NULL OR NOT (last_close > 0 AND last_close < 'Infinity');
DELETE FROM game_days g
WHERE g.round_count = 0 AND NOT EXISTS (
  SELECT 1 FROM day_kline d
  WHERE d.code = g.code AND d.trade_date = g.trade_date
    AND d.data_source = g.data_source);
-- 昨收去默认值（新行必须显式携带有效昨收）并加 CHECK 约束兜底防异常入库
ALTER TABLE day_kline ALTER COLUMN last_close DROP DEFAULT;
ALTER TABLE day_kline DROP CONSTRAINT IF EXISTS ck_day_kline_last_close_valid;
ALTER TABLE day_kline ADD CONSTRAINT ck_day_kline_last_close_valid
  CHECK (last_close IS NOT NULL AND last_close > 0 AND last_close < 'Infinity');
