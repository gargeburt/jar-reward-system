# -*- coding: utf-8 -*-
"""数据库连接管理与 schema 初始化。

- 每次请求新建连接（Flask app context 中经 get_db() 复用）。
- row_factory = sqlite3.Row，便于按列名访问。
- 初始化 / 每次连接开启 PRAGMA foreign_keys = ON。
- 所有写操作调用方必须包在事务中（with conn:），失败自动回滚。
"""
import os
import sqlite3

from flask import current_app, g

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def connect_db(db_path=None):
    """建立到 SQLite 的连接。db_path 缺省时取 Flask 配置 DATABASE。"""
    if db_path is None:
        db_path = current_app.config["DATABASE"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """获取当前请求上下文内的数据库连接（每次请求新建，teardown 关闭）。"""
    if "db" not in g:
        g.db = connect_db()
    return g.db


def close_db(_exc=None):
    """请求结束时关闭连接（未提交的事务由 sqlite3 回滚）。"""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path=None):
    """执行 schema.sql 初始化所有表（IF NOT EXISTS，幂等）。"""
    conn = connect_db(db_path)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()
