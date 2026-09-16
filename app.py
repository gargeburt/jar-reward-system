# -*- coding: utf-8 -*-
"""亲子双罐激励系统 —— Flask 主应用。

职责：路由 + 服务端权限控制 + 全部核心业务逻辑。
技术栈：Python 3 + Flask + SQLite（内置 sqlite3），唯一第三方依赖 Flask。

安全要点：
- 密码 pbkdf2 哈希入库（werkzeug.generate_password_hash）
- 服务端强制角色：孩子只能读自己数据 + 合法提现；写接口一律 parent_required
- CSRF 防护：登录后 session['_csrf']，所有 POST 表单携带并校验
- 提现金额由服务端重新计算，绝不信任前端传值
- 所有写操作单事务（with conn:），失败回滚，保证原子性
"""
import hmac
import os
import secrets
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database import close_db, get_db, init_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_BALANCE = 100
MIN_BALANCE = 0

# ---------------- 中文展示映射 ----------------
OP_NAMES = {
    "daily_reward": "每日任务奖励",
    "daily_penalty": "每日任务扣款",
    "task_modify": "任务结果修改",
    "parent_add": "家长增加",
    "parent_sub": "家长减少",
    "single_withdraw": "单罐提现",
    "double_withdraw": "双罐提现",
    "system": "系统调整",
}

STATUS_NAMES = {
    "pending": "待确认",
    "completed": "已完成",
    "failed": "未完成",
}

JAR_TYPES = ("knowledge", "energy")

TASK_META = {
    "knowledge": {
        "name": "知识罐 · 阅读",
        "emoji": "📚",
        "desc": "今天阅读 ≥ 15 分钟",
        "reward_desc": "完成 +1 分",
        "penalty_desc": "未完成 -1 分",
        "confirm_success": "阅读≥15分钟完成",
        "confirm_fail": "阅读不足15分钟未完成",
    },
    "energy": {
        "name": "能量罐 · 运动",
        "emoji": "⚡",
        "desc": "今天运动打卡",
        "reward_desc": "完成 +1 分",
        "penalty_desc": "未完成 -1 分",
        "confirm_success": "运动完成",
        "confirm_fail": "运动未完成",
    },
}

JAR_NAMES = {
    "knowledge": "知识罐（阅读）",
    "energy": "能量罐（运动）",
}


# ---------------- 工具函数 ----------------
def clamp(v: int) -> int:
    """余额封顶 100 / 保底 0。"""
    return max(MIN_BALANCE, min(MAX_BALANCE, v))


def today_str() -> str:
    """家庭单机，使用服务器本地日期。"""
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_jar(uid: int, jar_type: str):
    """读取罐子余额；不存在时返回虚拟 0 元行（不写库）。"""
    row = get_db().execute(
        "SELECT * FROM jars WHERE user_id=? AND jar_type=?", (uid, jar_type)
    ).fetchone()
    if row is None:
        return {"user_id": uid, "jar_type": jar_type, "balance": 0,
                "max_balance": MAX_BALANCE}
    return row


def _upsert_jar(conn, uid: int, jar_type: str, balance: int) -> None:
    """以 UPSERT 方式写入罐子余额（行不存在则插入，存在则覆盖）。"""
    conn.execute(
        """
        INSERT INTO jars (user_id, jar_type, balance, max_balance, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, jar_type) DO UPDATE SET
            balance = excluded.balance,
            updated_at = excluded.updated_at
        """,
        (uid, jar_type, balance, MAX_BALANCE, _now()),
    )


def _write_transaction(conn, *, user_id, jar_type, change_amount,
                       before_balance, after_balance, op_type,
                       operator_id=None, reason=None,
                       related_task_id=None, related_withdrawal_id=None):
    """写一条流水（必须与余额变更同一事务内调用）。"""
    conn.execute(
        """
        INSERT INTO transactions
          (user_id, jar_type, change_amount, before_balance, after_balance,
           op_type, operator_id, reason, related_task_id, related_withdrawal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, jar_type, change_amount, int(before_balance), int(after_balance),
         op_type, operator_id, reason, related_task_id, related_withdrawal_id),
    )


def get_target_child(uid: int):
    """归属校验：确认操作目标必须是 child 角色用户，否则返回 None。"""
    row = get_db().execute(
        "SELECT * FROM users WHERE id=? AND role='child'", (uid,)
    ).fetchone()
    return row


def _load_user(uid: int):
    row = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row


# ---------------- 业务函数（单事务） ----------------

def settle_task(uid: int, task_type: str, completed: bool, operator_id: int):
    """每日结算（家长确认）。

    返回 (ok, message)。任务记录与余额变更同一事务原子提交。
    """
    conn = get_db()
    date = today_str()

    if task_type not in JAR_TYPES:
        return False, "非法任务类型"
    meta = TASK_META[task_type]

    existing = conn.execute(
        "SELECT * FROM daily_tasks WHERE user_id=? AND task_date=? AND task_type=?",
        (uid, date, task_type),
    ).fetchone()
    if existing and existing["status"] != "pending":
        return False, f"今天{meta['name']}已经结算，不能重复确认"

    expected = 1 if completed else -1
    jar = get_jar(uid, task_type)
    before = int(jar["balance"])
    after = clamp(before + expected)
    diff = after - before

    status = "completed" if completed else "failed"
    op_type = "daily_reward" if completed else "daily_penalty"
    reason = meta["confirm_success"] if completed else meta["confirm_fail"]
    now = _now()

    with conn:
        _upsert_jar(conn, uid, task_type, after)
        if existing is not None:
            conn.execute(
                """
                UPDATE daily_tasks SET status=?, reward_amount=?, confirmed_by=?,
                  confirmed_at=?, updated_at=? WHERE id=?
                """,
                (status, expected, operator_id, now, now, existing["id"]),
            )
            task_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO daily_tasks
                  (user_id, task_date, task_type, status, reward_amount,
                   confirmed_by, confirmed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, date, task_type, status, expected,
                 operator_id, now, now),
            )
            task_id = cur.lastrowid
        _write_transaction(
            conn, user_id=uid, jar_type=task_type, change_amount=diff,
            before_balance=before, after_balance=after, op_type=op_type,
            operator_id=operator_id, reason=reason, related_task_id=task_id,
        )
    return True, f"已确认：{meta['name']} {STATUS_NAMES[status]}"


def modify_task(uid: int, task_type: str, to_status: str, operator_id: int):
    """修改当天已确认任务：回滚式重算差额，产生 task_modify 流水。"""
    conn = get_db()
    date = today_str()

    if task_type not in JAR_TYPES:
        return False, "非法任务类型"
    if to_status not in ("completed", "failed"):
        return False, "目标状态非法"

    task = conn.execute(
        "SELECT * FROM daily_tasks WHERE user_id=? AND task_date=? AND task_type=?",
        (uid, date, task_type),
    ).fetchone()
    if not task or task["status"] == "pending":
        return False, "该任务尚未确认，无法修改"
    if task["status"] == to_status:
        return False, f"任务本来就是「{STATUS_NAMES[to_status]}」，无需修改"

    old_status = task["status"]
    # 修改任务 = 把任务贡献从「旧状态名义值」换成「新状态名义值」：
    # completed 名义 +1 / failed 名义 -1，差额 = new - old 直接作用于当前余额再 clamp。
    # 严格满足「修改任务差额 ±2」，且连续多次修改不会因历史流水漂移。
    old_reward = 1 if old_status == "completed" else -1
    new_reward = 1 if to_status == "completed" else -1
    jar = get_jar(uid, task_type)
    before = int(jar["balance"])
    desired = clamp(before + new_reward - old_reward)
    diff = desired - before
    now = _now()

    with conn:
        _upsert_jar(conn, uid, task_type, desired)
        conn.execute(
            """
            UPDATE daily_tasks SET status=?, reward_amount=?, confirmed_by=?,
              confirmed_at=?, updated_at=? WHERE id=?
            """,
            (to_status, new_reward, operator_id, now, now, task["id"]),
        )
        reason = f"任务结果修改：{STATUS_NAMES[old_status]}→{STATUS_NAMES[to_status]}（差额{diff:+d}）"
        _write_transaction(
            conn, user_id=uid, jar_type=task_type, change_amount=diff,
            before_balance=before, after_balance=desired, op_type="task_modify",
            operator_id=operator_id, reason=reason, related_task_id=task["id"],
        )
    return True, reason


def backfill_task(uid: int, task_date: str, task_type: str, completed: bool, operator_id: int):
    """补录指定日期的任务（家长操作）。

    允许补录过去某一天未记录的任务：插入 daily_tasks 并把对应的 +1/-1 作用到当前余额，写入流水。
    若该日期已存在已确认记录（非 pending），拒绝补录以避免历史冲突。
    返回 (ok, message)
    """
    conn = get_db()
    # 校验类型
    if task_type not in JAR_TYPES:
        return False, "非法任务类型"
    try:
        # 简单校验日期格式 YYYY-MM-DD
        datetime.strptime(task_date, "%Y-%m-%d")
    except Exception:
        return False, "日期格式非法，需为 YYYY-MM-DD"

    existing = conn.execute(
        "SELECT * FROM daily_tasks WHERE user_id=? AND task_date=? AND task_type=?",
        (uid, task_date, task_type),
    ).fetchone()
    if existing and existing["status"] != "pending":
        return False, "该日期已有已确认记录，若需修改请使用修改功能"

    expected = 1 if completed else -1
    jar = get_jar(uid, task_type)
    before = int(jar["balance"])
    after = clamp(before + expected)
    diff = after - before

    status = "completed" if completed else "failed"
    op_type = "daily_reward" if completed else "daily_penalty"
    reason = TASK_META[task_type]["confirm_success"] if completed else TASK_META[task_type]["confirm_fail"]
    now = _now()

    with conn:
        _upsert_jar(conn, uid, task_type, after)
        if existing is not None:
            conn.execute(
                """
                UPDATE daily_tasks SET status=?, reward_amount=?, confirmed_by=?,
                  confirmed_at=?, updated_at=? WHERE id=?
                """,
                (status, expected, operator_id, now, now, existing["id"]),
            )
            task_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO daily_tasks
                  (user_id, task_date, task_type, status, reward_amount,
                   confirmed_by, confirmed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, task_date, task_type, status, expected, operator_id, now, now),
            )
            task_id = cur.lastrowid
        _write_transaction(
            conn, user_id=uid, jar_type=task_type, change_amount=diff,
            before_balance=before, after_balance=after, op_type=op_type,
            operator_id=operator_id, reason=f"补录 {task_date}：{reason}", related_task_id=task_id,
        )
    return True, f"已补录：{task_date} {TASK_META[task_type]['name']} {STATUS_NAMES[status]}"


def adjust_jar(uid: int, jar_type: str, amount, action: str, reason: str, operator_id: int):
    """家长手工调整余额（受 0..100 上限约束，原因必填）。"""
    conn = get_db()
    if not reason or not str(reason).strip():
        return False, "请填写调整原因"
    reason = str(reason).strip()
    if jar_type not in JAR_TYPES:
        return False, "非法罐子类型"
    if action not in ("add", "sub"):
        return False, "非法操作类型"
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return False, "金额必须是正整数"
    if amount <= 0:
        return False, "金额必须是正整数"

    signed = amount if action == "add" else -amount
    jar = get_jar(uid, jar_type)
    before = int(jar["balance"])
    after = clamp(before + signed)
    diff = after - before
    op_type = "parent_add" if action == "add" else "parent_sub"
    now = _now()

    with conn:
        _upsert_jar(conn, uid, jar_type, after)
        _write_transaction(
            conn, user_id=uid, jar_type=jar_type, change_amount=diff,
            before_balance=before, after_balance=after, op_type=op_type,
            operator_id=operator_id, reason=f"{JAR_NAMES[jar_type]}：{reason}",
        )
    verb = "增加" if action == "add" else "减少"
    return True, f"已{verb}：{JAR_NAMES[jar_type]}（实际变动{diff:+d}）"


def single_withdraw(uid: int, jar_type: str):
    """单罐提现：余额必须恰为 100，清零并记账（原子）。"""
    conn = get_db()
    if jar_type not in JAR_TYPES:
        return False, "非法罐子类型"

    jar = get_jar(uid, jar_type)
    if int(jar["balance"]) != MAX_BALANCE:
        return False, "余额不足100元，不能提现"

    now = _now()
    withdraw_type = f"single_{jar_type}"
    with conn:
        _upsert_jar(conn, uid, jar_type, 0)
        cur = conn.execute(
            """
            INSERT INTO withdrawals
              (user_id, withdraw_type, knowledge_amount, energy_amount, reward_amount, status)
            VALUES (?, ?, ?, ?, ?, 'completed')
            """,
            (uid, withdraw_type,
             MAX_BALANCE if jar_type == "knowledge" else 0,
             MAX_BALANCE if jar_type == "energy" else 0,
             MAX_BALANCE),
        )
        wid = cur.lastrowid
        _write_transaction(
            conn, user_id=uid, jar_type=jar_type, change_amount=-MAX_BALANCE,
            before_balance=MAX_BALANCE, after_balance=0,
            op_type="single_withdraw", reason=f"{JAR_NAMES[jar_type]}满{MAX_BALANCE}兑换{MAX_BALANCE}",
            related_withdrawal_id=wid,
        )
    return True, f"提现成功：{JAR_NAMES[jar_type]}兑换{MAX_BALANCE}元"


def double_withdraw(uid: int):
    """双罐 400 提现：两罐都必须恰为 100，双双清零（原子）。"""
    conn = get_db()
    k = get_jar(uid, "knowledge")
    e = get_jar(uid, "energy")
    kb, eb = int(k["balance"]), int(e["balance"])
    if kb != MAX_BALANCE or eb != MAX_BALANCE:
        which = []
        if kb != MAX_BALANCE:
            which.append(f"知识罐({kb}/100)")
        if eb != MAX_BALANCE:
            which.append(f"能量罐({eb}/100)")
        return False, "双罐提现需要两罐都满100元，目前：" + "、".join(which)

    now = _now()
    with conn:
        _upsert_jar(conn, uid, "knowledge", 0)
        _upsert_jar(conn, uid, "energy", 0)
        cur = conn.execute(
            """
            INSERT INTO withdrawals
              (user_id, withdraw_type, knowledge_amount, energy_amount, reward_amount, status)
            VALUES (?, 'double', ?, ?, 400, 'completed')
            """,
            (uid, MAX_BALANCE, MAX_BALANCE),
        )
        wid = cur.lastrowid
        _write_transaction(
            conn, user_id=uid, jar_type="knowledge", change_amount=-MAX_BALANCE,
            before_balance=MAX_BALANCE, after_balance=0,
            op_type="double_withdraw",
            reason="双罐同满兑换：并列奖励两罐同兑得400元",
            related_withdrawal_id=wid,
        )
        _write_transaction(
            conn, user_id=uid, jar_type="energy", change_amount=-MAX_BALANCE,
            before_balance=MAX_BALANCE, after_balance=0,
            op_type="double_withdraw",
            reason="双罐同满兑换：并列奖励两罐同兑得400元",
            related_withdrawal_id=wid,
        )
    return True, "双罐提现成功：同时兑换获得400元"


# ---------------- 权限装饰器 ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def parent_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session or session.get("role") != "parent":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def child_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session or session.get("role") != "child":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ---------------- 应用工厂 ----------------
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("JAR_SECRET_KEY", "dev-secret-key-please-change"),
        DATABASE=os.path.join(BASE_DIR, "jar.db"),
    )
    if test_config:
        app.config.update(test_config)
    app.secret_key = app.config["SECRET_KEY"]

    if not test_config or test_config.get("INIT_DB", True):
        init_db(app.config["DATABASE"])
    app.teardown_appcontext(close_db)

    # ---- CSRF：GET 时保证存在 token，POST 统一校验 ----
    @app.before_request
    def csrf_protect():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        if request.method == "POST":
            token = session.get("_csrf", "")
            form_token = request.form.get("_csrf", "")
            if not token or not hmac.compare_digest(token, form_token):
                abort(403)

    # ---------- 认证 ----------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        conn = get_db()
        user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        no_user = user_count == 0

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            if no_user:
                # 初始化家长账号
                pwd2 = request.form.get("password2") or ""
                if not username or not password:
                    flash("请填写用户名与密码", "error")
                elif password != pwd2:
                    flash("两次输入的密码不一致", "error")
                else:
                    phash = generate_password_hash(password)
                    with conn:
                        conn.execute(
                            "INSERT INTO users (username, password_hash, role) VALUES (?,?,'parent')",
                            (username, phash),
                        )
                    flash(f"家长账号「{username}」创建成功，请登录", "success")
                    return redirect(url_for("login"))
            else:
                user = conn.execute(
                    "SELECT * FROM users WHERE username=?", (username,)
                ).fetchone()
                if user and check_password_hash(user["password_hash"], password):
                    session.clear()
                    session["user_id"] = user["id"]
                    session["role"] = user["role"]
                    session["username"] = user["username"]
                    session["_csrf"] = secrets.token_hex(16)
                    return redirect(url_for("index"))
                flash("用户名或密码错误", "error")
        return render_template("login.html", no_user=no_user)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # ---------- 页面 ----------
    @app.route("/")
    def index():
        # 允许匿名查看：若为家长/孩子按原逻辑显示；匿名时展示第一位孩子（本地家庭单机场景）
        conn = get_db()
        role = session.get("role")
        if role == "parent":
            child_row = conn.execute(
                "SELECT * FROM users WHERE role='child' ORDER BY id LIMIT 1"
            ).fetchone()
            target = child_row
        elif role == "child":
            target = _load_user(session["user_id"])
        else:
            target = conn.execute(
                "SELECT * FROM users WHERE role='child' ORDER BY id LIMIT 1"
            ).fetchone()
        content = build_home(target)
        return render_template("index.html", **content)

    @app.route("/rules")
    def rules():
        return render_template("rules.html")

    @app.route("/history")
    def history():
        conn = get_db()
        role = session.get("role")
        if role == "parent":
            users = conn.execute(
                "SELECT * FROM users WHERE role='child' ORDER BY id"
            ).fetchall()
            uid_list = [u["id"] for u in users] or [0]
        elif role == "child":
            uid_list = [session["user_id"]]
        else:
            # 匿名查看历史：展示第一位孩子的历史（单机家庭常见需求）
            child = conn.execute(
                "SELECT * FROM users WHERE role='child' ORDER BY id LIMIT 1"
            ).fetchone()
            uid_list = [child["id"]] if child else [0]

        placeholders = ",".join("?" * len(uid_list))
        tasks = conn.execute(
            f"""
            SELECT t.*, u.username FROM daily_tasks t
            JOIN users u ON u.id = t.user_id
            WHERE t.user_id IN ({placeholders})
            ORDER BY t.task_date DESC, t.id DESC
            """,
            uid_list,
        ).fetchall()
        txns = conn.execute(
            f"""
            SELECT x.*, u.username FROM transactions x
            JOIN users u ON u.id = x.user_id
            WHERE x.user_id IN ({placeholders})
            ORDER BY x.id DESC LIMIT 500
            """,
            uid_list,
        ).fetchall()
        withs = conn.execute(
            f"""
            SELECT w.*, u.username FROM withdrawals w
            JOIN users u ON u.id = w.user_id
            WHERE w.user_id IN ({placeholders})
            ORDER BY w.id DESC
            """,
            uid_list,
        ).fetchall()

        return render_template(
            "history.html", tasks=tasks, txns=txns, withdrawals=withs,
            op_names=OP_NAMES, status_names=STATUS_NAMES,
            task_meta=TASK_META, jar_names=JAR_NAMES,
        )

    @app.route("/parent")
    @parent_required
    def parent():
        conn = get_db()
        children = conn.execute(
            "SELECT * FROM users WHERE role='child' ORDER BY id"
        ).fetchall()
        rows = []
        date = today_str()
        for c in children:
            entry = {"child": c, "jars": {}, "tasks": {}}
            for jt in JAR_TYPES:
                entry["jars"][jt] = get_jar(c["id"], jt)
                t = conn.execute(
                    "SELECT * FROM daily_tasks WHERE user_id=? AND task_date=? AND task_type=?",
                    (c["id"], date, jt),
                ).fetchone()
                entry["tasks"][jt] = t
            rows.append(entry)
        return render_template("parent.html", children=children, rows=rows,
                               today=date, task_meta=TASK_META,
                               status_names=STATUS_NAMES, max_balance=MAX_BALANCE)

    @app.route("/parents/child", methods=["GET", "POST"])
    @parent_required
    def create_child():
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            pwd2 = request.form.get("password2") or ""
            conn = get_db()
            if not username or not password:
                flash("请填写用户名与密码", "error")
            elif password != pwd2:
                flash("两次输入的密码不一致", "error")
            elif conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
                flash("用户名已存在", "error")
            else:
                phash = generate_password_hash(password)
                with conn:
                    conn.execute(
                        "INSERT INTO users (username, password_hash, role) VALUES (?,?,'child')",
                        (username, phash),
                    )
                flash(f"孩子账号「{username}」创建成功", "success")
                return redirect(url_for("parent"))
        return redirect(url_for("parent"))

    @app.route("/withdraw")
    def withdraw():
        # GET: 允许匿名/孩子/家长查看当前罐子余额（但仅孩子登录时可执行 POST 提现）
        conn = get_db()
        role = session.get("role")
        if role == "parent":
            # 家长查看时跳回家长管理页
            return redirect(url_for("parent"))
        if role == "child":
            uid = session["user_id"]
        else:
            child = conn.execute(
                "SELECT * FROM users WHERE role='child' ORDER BY id LIMIT 1"
            ).fetchone()
            uid = child["id"] if child else None
        if uid is None:
            return render_template("withdraw.html", knowledge=None, energy=None,
                                   max_balance=MAX_BALANCE)
        k = get_jar(uid, "knowledge")
        e = get_jar(uid, "energy")
        return render_template("withdraw.html", knowledge=k, energy=e,
                               max_balance=MAX_BALANCE)

    # ---------- 写操作（家长）----------
    @app.route("/task/confirm", methods=["POST"])
    @parent_required
    def task_confirm():
        try:
            uid = int(request.form.get("user_id"))
        except (TypeError, ValueError):
            abort(400)
        task_type = request.form.get("task_type")
        result = request.form.get("result")
        if task_type not in JAR_TYPES or result not in ("completed", "failed"):
            abort(400)
        if get_target_child(uid) is None:
            abort(403)
        ok, msg = settle_task(uid, task_type, result == "completed", session["user_id"])
        flash(msg, "success" if ok else "error")
        return redirect(request.referrer or url_for("parent"))

    @app.route("/task/modify", methods=["POST"])
    @parent_required
    def task_modify():
        try:
            uid = int(request.form.get("user_id"))
        except (TypeError, ValueError):
            abort(400)
        task_type = request.form.get("task_type")
        to_status = request.form.get("to")
        if task_type not in JAR_TYPES or to_status not in ("completed", "failed"):
            abort(400)
        if get_target_child(uid) is None:
            abort(403)
        ok, msg = modify_task(uid, task_type, to_status, session["user_id"])
        flash(msg, "success" if ok else "error")
        return redirect(url_for("parent"))

    @app.route("/jar/adjust", methods=["POST"])
    @parent_required
    def jar_adjust():
        try:
            uid = int(request.form.get("user_id"))
        except (TypeError, ValueError):
            abort(400)
        jar_type = request.form.get("jar_type")
        action = request.form.get("action")
        amount = request.form.get("amount")
        reason = request.form.get("reason")
        if jar_type not in JAR_TYPES or action not in ("add", "sub"):
            abort(400)
        if get_target_child(uid) is None:
            abort(403)
        ok, msg = adjust_jar(uid, jar_type, amount, action, reason, session["user_id"])
        flash(msg, "success" if ok else "error")
        return redirect(url_for("parent"))

    @app.route("/parent/backfill", methods=["POST"])
    @parent_required
    def parent_backfill():
        try:
            uid = int(request.form.get("user_id"))
        except (TypeError, ValueError):
            abort(400)
        task_date = request.form.get("task_date")
        task_type = request.form.get("task_type")
        result = request.form.get("result")
        if task_type not in JAR_TYPES or result not in ("completed", "failed"):
            abort(400)
        if get_target_child(uid) is None:
            abort(403)
        ok, msg = backfill_task(uid, task_date, task_type, result == "completed", session["user_id"])
        flash(msg, "success" if ok else "error")
        return redirect(request.referrer or url_for("parent"))

    # ---------- 写操作（孩子：仅提现）----------
    @app.route("/withdraw", methods=["POST"])
    @child_required
    def withdraw_post():
        action = request.form.get("action")
        uid = session["user_id"]
        if action in ("single_knowledge", "single_energy"):
            jar_type = action.split("_", 1)[1]
            ok, msg = single_withdraw(uid, jar_type)
        elif action == "double":
            ok, msg = double_withdraw(uid)
        else:
            abort(400)
        flash(msg, "success" if ok else "error")
        return redirect(url_for("withdraw"))

    # ---------- 错误页 ----------
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403,
                               message="没有权限执行该操作（孩子不能修改任何数据）"), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="页面不存在"), 404

    return app


def build_home(target):
    """构造首页模板数据。target 为 None（无孩子）时显示空态。"""
    conn = get_db()
    if target is None:
        return {"target": None, "jars": None, "tasks": None,
                "today": today_str(), "status_names": STATUS_NAMES,
                "task_meta": TASK_META, "max_balance": MAX_BALANCE,
                "is_parent": session.get("role") == "parent"}
    date = today_str()
    jars = {jt: get_jar(target["id"], jt) for jt in JAR_TYPES}
    tasks = {}
    for jt in JAR_TYPES:
        t = conn.execute(
            "SELECT * FROM daily_tasks WHERE user_id=? AND task_date=? AND task_type=?",
            (target["id"], date, jt),
        ).fetchone()
        tasks[jt] = t
    return {
        "target": target, "jars": jars, "tasks": tasks, "today": date,
        "status_names": STATUS_NAMES, "task_meta": TASK_META,
        "max_balance": MAX_BALANCE,
        "is_parent": session.get("role") == "parent",
    }


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=False)
