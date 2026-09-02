-- =============================================================================
-- StockGame 数据库初始化脚本（幂等，可重复执行）
-- 说明: 仅含建表 DDL，不含 CREATE DATABASE（由 migrate_db.py 动态执行）
-- =============================================================================

-- 原始 3s 行情（游戏数据源，每个 (code, time_key) 仅一条）
CREATE TABLE IF NOT EXISTS tick_data (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  open REAL, high REAL, low REAL, close REAL,
  volume BIGINT, amount REAL, last_close REAL,
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_data_trade_date ON tick_data (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_data_code_date ON tick_data (code, trade_date);

-- 转换模拟行情（stockkline 1min K 线拓展为 3s，QMT 无该日数据时的兜底数据源）
CREATE TABLE IF NOT EXISTS tick_data_sim (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  time_key VARCHAR(19) NOT NULL,      -- 'YYYY-MM-DD HH:MM:SS'
  open REAL, high REAL, low REAL, close REAL,
  volume BIGINT, amount REAL, last_close REAL,
  created_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_tick_sim_code_time UNIQUE (code, time_key)
);
CREATE INDEX IF NOT EXISTS ix_tick_sim_trade_date ON tick_data_sim (trade_date);
CREATE INDEX IF NOT EXISTS ix_tick_sim_code_date ON tick_data_sim (code, trade_date);

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
  last_price REAL, last_time_key VARCHAR(19)
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

-- ── 存量表升级（幂等）：老库 game_rounds 补 data_source 列 ──
ALTER TABLE game_rounds ADD COLUMN IF NOT EXISTS data_source VARCHAR(10) NOT NULL DEFAULT 'qmt';
