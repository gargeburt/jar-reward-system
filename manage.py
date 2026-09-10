# -*- coding: utf-8 -*-
"""命令行管理工具：初始化数据库 / 创建账号 / 启动服务。

用法：
    python manage.py init-db
    python manage.py create-user --username 爸爸 --role parent
    python manage.py create-user --username 小明 --role child
    python manage.py run               # 默认 0.0.0.0:5000，便于局域网手机访问
    python manage.py list-users
"""
import argparse
import getpass
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保可独立导入 app 模块（app 依赖 flask）
sys.path.insert(0, BASE_DIR)

from werkzeug.security import generate_password_hash  # noqa: E402


def default_db_path():
    return os.path.join(BASE_DIR, "jar.db")


def init_db(args=None):
    from database import init_db as _init
    _init(default_db_path())
    print(f"[OK] 数据库初始化完成：{default_db_path()}")


def create_user(username, role, password=None):
    import sqlite3
    if role not in ("parent", "child"):
        print("[错误] role 必须是 parent 或 child")
        sys.exit(1)
    if not username or not username.strip():
        print("[错误] username 不能为空")
        sys.exit(1)
    username = username.strip()
    if password is None:
        password = getpass.getpass(f"为账号「{username}」设置密码: ")
    if not password:
        print("[错误] 密码不能为空")
        sys.exit(1)

    # 确保库与表存在
    init_db()
    phash = generate_password_hash(password)
    conn = sqlite3.connect(default_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        exists = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            print(f"[错误] 用户名已存在：{username}")
            sys.exit(1)
        with conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
                (username, phash, role),
            )
        print(f"[OK] 已创建账号：{username}（角色：{role}）")
    finally:
        conn.close()


def list_users():
    import sqlite3
    if not os.path.exists(default_db_path()):
        print("（数据库尚未初始化，无账号。先执行 python manage.py init-db）")
        return
    conn = sqlite3.connect(default_db_path())
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY id"
        ).fetchall()
        if not rows:
            print("（暂无账号）")
        for r in rows:
            print(f"  #{r['id']}  {r['username']:<12} 角色={r['role']:<6} 创建于 {r['created_at']}")
    finally:
        conn.close()


def run_app(port, debug):
    from app import create_app
    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=debug)


def main():
    parser = argparse.ArgumentParser(description="亲子双罐激励系统 管理工具")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init-db", help="初始化数据库（幂等）")
    p_init.set_defaults(func=init_db)

    p_user = sub.add_parser("create-user", help="创建账号")
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--role", required=True, choices=["parent", "child"])
    p_user.add_argument("--password", default=None)
    p_user.set_defaults(func=lambda a: create_user(a.username, a.role, a.password))

    p_list = sub.add_parser("list-users", help="列出所有账号")
    p_list.set_defaults(func=lambda a: list_users())

    p_run = sub.add_parser("run", help="启动服务（默认 0.0.0.0:5000）")
    p_run.add_argument("--port", type=int, default=5000)
    p_run.add_argument("--debug", action="store_true")
    p_run.set_defaults(func=lambda a: run_app(a.port, a.debug))

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
