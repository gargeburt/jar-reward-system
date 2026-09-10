-- ============================================================
-- 亲子双罐激励系统 数据库 Schema
-- knowledge=知识罐（阅读）  energy=能量罐（运动）
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK(role IN ('parent','child')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS jars (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  jar_type     TEXT NOT NULL CHECK(jar_type IN ('knowledge','energy')),
  balance      INTEGER NOT NULL DEFAULT 0,
  max_balance  INTEGER NOT NULL DEFAULT 100,
  updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, jar_type)
);

CREATE TABLE IF NOT EXISTS daily_tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  task_date     TEXT NOT NULL,               -- 'YYYY-MM-DD'（服务器本地日期）
  task_type     TEXT NOT NULL CHECK(task_type IN ('knowledge','energy')),
  status        TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','failed')),
  reward_amount INTEGER NOT NULL DEFAULT 0,  -- 名义应得 +1 / -1
  confirmed_by  INTEGER REFERENCES users(id),
  confirmed_at  TEXT,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, task_date, task_type)      -- 防重复结算的数据库级保障
);

CREATE TABLE IF NOT EXISTS transactions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id              INTEGER NOT NULL REFERENCES users(id),
  jar_type             TEXT NOT NULL CHECK(jar_type IN ('knowledge','energy')),
  change_amount        INTEGER NOT NULL,     -- 真实净变化（可能为0）
  before_balance       INTEGER NOT NULL,
  after_balance        INTEGER NOT NULL,
  op_type              TEXT NOT NULL,        -- daily_reward/daily_penalty/task_modify/parent_add/parent_sub/single_withdraw/double_withdraw/system
  operator_id          INTEGER,              -- 操作人 users.id
  reason               TEXT,
  related_task_id      INTEGER,
  related_withdrawal_id INTEGER,
  created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          INTEGER NOT NULL REFERENCES users(id),
  withdraw_type    TEXT NOT NULL CHECK(withdraw_type IN ('single_knowledge','single_energy','double')),
  knowledge_amount INTEGER NOT NULL DEFAULT 0,
  energy_amount    INTEGER NOT NULL DEFAULT 0,
  reward_amount    INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'completed',
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
