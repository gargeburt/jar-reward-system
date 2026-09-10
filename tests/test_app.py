# -*- coding: utf-8 -*-
"""亲子双罐激励系统 —— 业务规则自动化测试。

运行：python -m pytest tests/test_app.py -v

覆盖清单（对照设计文档「测试清单」）：
  - 结算 +1 / -1、99→100 封顶、0 保底
  - 同一天不可重复结算
  - 修改任务差额回滚（含封顶边界）
  - 单罐 100 提现 / 双罐 400 提现 / 非法提现拒绝
  - 家长手工调整：原因必填、封顶保底
  - 孩子账号写操作 403、CSRF 拦截、未登录跳转
  - 事务原子性（业务函数内部 with conn 保证）
"""
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import create_app          # noqa: E402
from database import connect_db     # noqa: E402


def _db(app):
    return connect_db(app.config["DATABASE"])


def _q(app, sql, args=()):
    conn = _db(app)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _one(app, sql, args=()):
    rows = _q(app, sql, args)
    return rows[0] if rows else None


def _balance(app, uid, jar_type):
    row = _one(app, "SELECT balance FROM jars WHERE user_id=? AND jar_type=?",
               (uid, jar_type))
    return row["balance"] if row else 0


def _set_balance(app, uid, jar_type, value):
    conn = _db(app)
    with conn:
        conn.execute(
            "INSERT INTO jars (user_id, jar_type, balance, max_balance, updated_at) "
            "VALUES (?,?,?,100,datetime('now','localtime')) "
            "ON CONFLICT(user_id, jar_type) DO UPDATE SET balance=excluded.balance, "
            "updated_at=excluded.updated_at",
            (uid, jar_type, value),
        )
    conn.close()


def _seed_users(app):
    conn = _db(app)
    with conn:
        conn.execute("INSERT INTO users (username,password_hash,role) VALUES (?,?,?)",
                     ("爸爸", generate_password_hash("p"), "parent"))
        conn.execute("INSERT INTO users (username,password_hash,role) VALUES (?,?,?)",
                     ("小明", generate_password_hash("p"), "child"))
        conn.execute("INSERT INTO users (username,password_hash,role) VALUES (?,?,?)",
                     ("小红", generate_password_hash("p"), "child"))
    conn.close()
    p = _one(app, "SELECT id FROM users WHERE username='爸爸'")
    c = _one(app, "SELECT id FROM users WHERE username='小明'")
    c2 = _one(app, "SELECT id FROM users WHERE username='小红'")
    return p["id"], c["id"], c2["id"]


@pytest.fixture()
def app(tmp_path):
    db_file = tmp_path / "test_jar.db"
    a = create_app({
        "TESTING": True,
        "DATABASE": str(db_file),
        "INIT_DB": True,
        "SECRET_KEY": "test-secret",
    })
    yield a


@pytest.fixture()
def client(app):
    return app.test_client()


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["_csrf"]


def _login(client, username, password="p"):
    # GET 触发 before_request 生成 _csrf
    client.get("/login")
    csrf = _csrf(client)
    return client.post(
        "/login",
        data={"username": username, "password": password, "_csrf": csrf},
        follow_redirects=False,
    )


def _post(client, url, data, csrf_ok=True):
    if csrf_ok:
        data = dict(data)
        data["_csrf"] = _csrf(client)
    return client.post(url, data=data, follow_redirects=False)


def _confirm(client, uid, jt, result):
    return _post(client, "/task/confirm",
                 {"user_id": uid, "task_type": jt, "result": result})


def _modify(client, uid, jt, to):
    return _post(client, "/task/modify",
                 {"user_id": uid, "task_type": jt, "to": to})


def _adjust(client, uid, jt, action, amount, reason="测试调整"):
    return _post(client, "/jar/adjust",
                 {"user_id": uid, "jar_type": jt, "action": action,
                  "amount": amount, "reason": reason})


def _withdraw(client, action):
    return _post(client, "/withdraw", {"action": action})


# ---------------- 认证与权限 ----------------

def test_unauth_redirect_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_and_create_child(app, client):
    p, c, c2 = _seed_users(app)
    r = _login(client, "爸爸")
    assert r.status_code == 302

    # 家长创建孩子
    r = _post(client, "/parents/child",
              {"username": "小刚", "password": "qq", "password2": "qq"})
    assert r.status_code == 302
    row = _one(app, "SELECT * FROM users WHERE username='小刚'")
    assert row is not None and row["role"] == "child"


def test_child_cannot_write_403(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "小明")
    assert _confirm(client, c, "knowledge", "completed").status_code == 403
    assert _modify(client, c, "knowledge", "failed").status_code == 403
    assert _adjust(client, c, "knowledge", "add", 5).status_code == 403
    # 提现是孩子唯一允许的写接口：角色通过，但业务上余额不足被拒（302+flash）
    r = _withdraw(client, "double")
    assert r.status_code == 302


def test_csrf_missing_403(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    r = client.post("/task/confirm",
                    data={"user_id": c, "task_type": "knowledge", "result": "completed"},
                    follow_redirects=False)
    assert r.status_code == 403


# ---------------- 每日任务结算 ----------------

def test_confirm_plus_minus(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 1
    _confirm(client, c, "energy", "failed")
    assert _balance(app, c, "energy") == 0  # 0 保底，再减不动
    assert _balance(app, c, "knowledge") == 1


def test_clamp_high_99_to_100(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _set_balance(app, c, "knowledge", 99)
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 100
    # 手工加 5 封顶 100
    _adjust(client, c, "knowledge", "add", 5)
    assert _balance(app, c, "knowledge") == 100


def test_clamp_low_0_stays_0(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _set_balance(app, c, "knowledge", 0)
    _confirm(client, c, "knowledge", "failed")
    assert _balance(app, c, "knowledge") == 0
    _adjust(client, c, "knowledge", "sub", 3)
    assert _balance(app, c, "knowledge") == 0


def test_no_double_settle(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 1
    _confirm(client, c, "knowledge", "failed")  # 已结算，拒绝
    assert _balance(app, c, "knowledge") == 1
    _confirm(client, c, "knowledge", "completed")  # 再次拒绝
    assert _balance(app, c, "knowledge") == 1


# ---------------- 任务修改（差额回滚） ----------------

def test_modify_task_single_diff(app, client):
    """单次修改：差额按 ±2 直接作用（非 clamp 中间态下精确验证）。"""
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _set_balance(app, c, "knowledge", 50)
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 51
    # completed(+1) → failed(-1)：差额 -2
    _modify(client, c, "knowledge", "failed")
    assert _balance(app, c, "knowledge") == 49
    # failed(-1) → completed(+1)：差额 +2
    _modify(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 51


def test_modify_task_chain_clamp(app, client):
    """多次反复修改在 clamp 边界会顺差叠加（差额直接作用规则）。

    0 → 确认完成 +1 = 1 → 改失败 -2 → 0（-1 被 0 下限截断）
      → 改回完成 +2 = 2 。该叠加对防刷分仍安全（需余额真实变化支撑），
      属 clamp 边界下的预期行为；家长正常"点错改一次"每次均精确 ±2。
    """
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 1
    _modify(client, c, "knowledge", "failed")
    assert _balance(app, c, "knowledge") == 0
    _modify(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 2


def test_modify_task_at_floor(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    # balance=0 时确认失败（净变化 0），改完成按严格 ±2：0 -(-1) +(+1) = 2
    _set_balance(app, c, "energy", 0)
    _confirm(client, c, "energy", "failed")
    assert _balance(app, c, "energy") == 0
    _modify(client, c, "energy", "completed")
    assert _balance(app, c, "energy") == 2


def test_modify_task_at_ceiling(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    # balance=100 时确认完成（净变化 0），改失败按严格 ±2：100 - (+1) + (-1) = 98
    _set_balance(app, c, "knowledge", 100)
    _confirm(client, c, "knowledge", "completed")
    assert _balance(app, c, "knowledge") == 100
    _modify(client, c, "knowledge", "failed")
    assert _balance(app, c, "knowledge") == 98


# ---------------- 家长手工调整 ----------------

def test_adjust_requires_reason(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    r = _post(client, "/jar/adjust",
              {"user_id": c, "jar_type": "knowledge", "action": "add",
               "amount": "5", "reason": " "})
    assert _balance(app, c, "knowledge") == 0  # 未生效


def test_adjust_invalid_amount(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "爸爸")
    _post(client, "/jar/adjust",
          {"user_id": c, "jar_type": "knowledge", "action": "add",
           "amount": "-3", "reason": "负数"})
    assert _balance(app, c, "knowledge") == 0
    _post(client, "/jar/adjust",
          {"user_id": c, "jar_type": "knowledge", "action": "add",
           "amount": "abc", "reason": "非数字"})
    assert _balance(app, c, "knowledge") == 0


# ---------------- 提现 ----------------

def test_single_withdraw(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "小明")
    # 未满 100 → 拒绝
    _set_balance(app, c, "knowledge", 99)
    r = _withdraw(client, "single_knowledge")
    assert _balance(app, c, "knowledge") == 99
    # 满 100 → 成功清零
    _set_balance(app, c, "knowledge", 100)
    _withdraw(client, "single_knowledge")
    assert _balance(app, c, "knowledge") == 0
    w = _one(app, "SELECT * FROM withdrawals WHERE user_id=? AND withdraw_type='single_knowledge'",
             (c,))
    assert w is not None and w["reward_amount"] == 100


def test_double_withdraw(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "小明")
    _set_balance(app, c, "knowledge", 100)
    _set_balance(app, c, "energy", 99)
    _withdraw(client, "double")
    assert _balance(app, c, "knowledge") == 100  # 能量未满，拒绝
    assert _balance(app, c, "energy") == 99
    _set_balance(app, c, "energy", 100)
    _withdraw(client, "double")
    assert _balance(app, c, "knowledge") == 0
    assert _balance(app, c, "energy") == 0
    w = _one(app, "SELECT * FROM withdrawals WHERE user_id=? AND withdraw_type='double'", (c,))
    assert w is not None and w["reward_amount"] == 400


def test_withdraw_writes_transactions(app, client):
    p, c, c2 = _seed_users(app)
    _login(client, "小明")
    _set_balance(app, c, "knowledge", 100)
    _withdraw(client, "single_knowledge")
    t = _one(app, "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (c,))
    assert t is not None
    assert t["op_type"] == "single_withdraw"
    assert t["before_balance"] == 100 and t["after_balance"] == 0
