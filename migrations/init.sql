-- =============================================================================
-- StockGame 数据库初始化脚本（幂等，可重复执行）
-- 说明: 仅含建表 DDL，不含 CREATE DATABASE（由 migrate_db.py 动态执行）
-- =============================================================================

-- 原始 3s 行情（游戏数据源，每个 (code, time_key) 仅一条）
-- 注意：昨收 last_close 不在此表维护，天维度原始行情统一存 day_kline 表
--（与 tick 对齐），game_days 为游戏选择/管理层（由 day_kline 派生）
CREATE TABLE IF NOT EXISTS tick_data (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  open REAL, high REAL, low REAL, close REAL,
  volume BIGINT, amount REAL,
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_data_trade_date ON tick_data (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_data_code_date ON tick_data (code, trade_date);

-- 转换模拟行情（stockkline 1min K 线拓展为 3s，QMT 无该日数据时的兜底数据源）
-- 注意：昨收 last_close 不在此表维护，天维度原始行情统一存 day_kline 表
--（与 tick 对齐），game_days 为游戏选择/管理层（由 day_kline 派生）
CREATE TABLE IF NOT EXISTS tick_data_sim (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  open REAL, high REAL, low REAL, close REAL,
  volume BIGINT, amount REAL,
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_sim_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_sim_trade_date ON tick_data_sim (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_sim_code_date ON tick_data_sim (code, trade_date);

-- 天维度原始行情（与 tick 表对齐；每 code+trade_date+data_source 一条）
-- 开局原始数据源：tick 入库时同步聚合写入（OHLC/量额/条数/首末时间均对账自 tick
-- 表），游戏选择与开局读取不再扫描 tick 表。last_close（昨收）在此维护：随生成方
-- 携带，缺失时仅生成侧从 stock_kline 天维度回补（migrate_db.py）。
-- 昨收必须为有效正数：空/0/NaN 视为异常日行情，写入前拦截拒绝（不入库），
-- 表 CHECK 约束兜底（见文件尾部昨收防线段）。
CREATE TABLE IF NOT EXISTS day_kline (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  data_source VARCHAR(10) NOT NULL DEFAULT 'qmt',  -- qmt(实盘)/sim(转换模拟)
  open REAL, high REAL, low REAL, close REAL,      -- 日级 OHLC（对账自 tick）
  volume BIGINT NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  last_close REAL NOT NULL,   -- 昨收（上一交易日收盘，必须有效 >0，无默认值防异常入库）
  tick_count INT NOT NULL DEFAULT 0,       -- 该日已入库 3s tick 数（对齐）
  first_time_key VARCHAR(19) NOT NULL DEFAULT '',  -- 首根 tick 时间（对齐）
  last_time_key VARCHAR(19) NOT NULL DEFAULT '',   -- 末根 tick 时间（对齐）
  is_complete BOOLEAN NOT NULL DEFAULT FALSE,  -- 完整交易日（末根 tick >= 15:00:00）
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
  is_complete BOOLEAN NOT NULL DEFAULT FALSE,  -- 可开局（末根 tick >= 15:00:00）
  round_count INT NOT NULL DEFAULT 0,       -- 该日已创建轮次数
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_game_day_code_date_src UNIQUE (code, trade_date, data_source)
);
CREATE INDEX IF NOT EXISTS ix_game_days_code_date ON game_days (code, trade_date);
CREATE INDEX IF NOT EXISTS ix_game_days_complete ON game_days (is_complete);

-- 存量升级（幂等）：旧版 game_days 曾内嵌行情统计列，现已迁至 day_kline
-- （统计/昨收列删除，数据由下方 day_kline 回填重建），保留管理状态并补 round_count
ALTER TABLE game_days DROP COLUMN IF EXISTS tick_count;
ALTER TABLE game_days DROP COLUMN IF EXISTS first_time_key;
ALTER TABLE game_days DROP COLUMN IF EXISTS last_time_key;
ALTER TABLE game_days DROP COLUMN IF EXISTS open;
ALTER TABLE game_days DROP COLUMN IF EXISTS high;
ALTER TABLE game_days DROP COLUMN IF EXISTS low;
ALTER TABLE game_days DROP COLUMN IF EXISTS close;
ALTER TABLE game_days DROP COLUMN IF EXISTS volume;
ALTER TABLE game_days DROP COLUMN IF EXISTS amount;
ALTER TABLE game_days DROP COLUMN IF EXISTS last_close;
ALTER TABLE game_days ADD COLUMN IF NOT EXISTS round_count INT NOT NULL DEFAULT 0;

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
  -- 无 UNIQUE(trade_date)：同一交易日可重复开局（上一轮 finished/aborted 后）
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

-- 存量刷新（幂等）：day_kline 统计/对齐字段按 tick 表现存数据重算
-- （仅更新已存在行，不插新行：day_kline 行由行情上传时 refresh_day 写入——
-- 昨收无效的异常日拒绝入库，此处无昨收来源，无法也不应补插；缺失行请先补
-- 有效昨收后由 refresh_day/refresh_all_days 重建）
UPDATE day_kline d SET
  tick_count = s.cnt, first_time_key = s.t0, last_time_key = s.t1,
  is_complete = s.cmp, open = s.o, high = s.h, low = s.l, close = s.c,
  volume = s.v, amount = s.a, updated_at = now()
FROM (
  SELECT code, trade_date, count(*) AS cnt, min(time_key) AS t0,
         max(time_key) AS t1, (max(time_key) >= trade_date || ' 15:00:00') AS cmp,
         (array_agg(open ORDER BY time_key))[1] AS o, max(high) AS h,
         min(low) AS l, (array_agg(close ORDER BY time_key DESC))[1] AS c,
         COALESCE(sum(volume), 0) AS v, COALESCE(sum(amount), 0) AS a
  FROM tick_data GROUP BY code, trade_date
) s
WHERE d.code = s.code AND d.trade_date = s.trade_date AND d.data_source = 'qmt';

UPDATE day_kline d SET
  tick_count = s.cnt, first_time_key = s.t0, last_time_key = s.t1,
  is_complete = s.cmp, open = s.o, high = s.h, low = s.l, close = s.c,
  volume = s.v, amount = s.a, updated_at = now()
FROM (
  SELECT code, trade_date, count(*) AS cnt, min(time_key) AS t0,
         max(time_key) AS t1, (max(time_key) >= trade_date || ' 15:00:00') AS cmp,
         (array_agg(open ORDER BY time_key))[1] AS o, max(high) AS h,
         min(low) AS l, (array_agg(close ORDER BY time_key DESC))[1] AS c,
         COALESCE(sum(volume), 0) AS v, COALESCE(sum(amount), 0) AS a
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

-- 存量表升级：tick 表删除 last_close 列（昨收改由 day_kline 维护）
ALTER TABLE tick_data DROP COLUMN IF EXISTS last_close;
ALTER TABLE tick_data_sim DROP COLUMN IF EXISTS last_close;

-- ── 存量表升级（幂等）：老库 game_rounds 补 data_source 列 ──
ALTER TABLE game_rounds ADD COLUMN IF NOT EXISTS data_source VARCHAR(10) NOT NULL DEFAULT 'qmt';

-- 存量表升级：game_rounds 补 account_json 列（账户 DB 兑底快照）
ALTER TABLE game_rounds ADD COLUMN IF NOT EXISTS account_json TEXT;
