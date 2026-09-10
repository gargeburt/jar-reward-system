# 亲子双罐激励系统 🏠

一个供家庭成员（家长 + 孩子）共同使用的本地 Web 应用：通过「知识罐（阅读）」与「能量罐（运动）」两个罐子，
用每日任务 + 积分 + 提现的方式激励孩子坚持阅读和运动。单人家庭本地部署，数据存本地 SQLite，不上传任何云端。

## 核心规则（需求对照速览）

| 编号 | 需求 | 实现 |
|------|------|------|
| 6/12 | 两罐各 0~100 元 | `balance` 字段 + `clamp(0,100)` 封顶保底 |
| 7/13 | 每日任务家长确认，完成 +1，未完成 -1 | 首页/管理页家长一键确认，写 `daily_tasks` + 流水 |
| 14/15 | 防重复结算 | `UNIQUE(user_id, task_date, task_type)` + 状态机（非 pending 不可再确认） |
| 8 | 修改任务算差额 | 状态名义差额（completed +1 / failed -1），修改直接施加 ±2 并 clamp，写 `task_modify` 流水 |
| 16/17 | 单罐满 100 提现 100 | `single_withdraw`：余额恰好 100 才允许，清零 + 流水 + 提现记录 |
| 18/19 | 双罐同满提现 400 | `double_withdraw`：两罐都得恰为 100，双双清零，事务原子 |
| 20 | 家长手工调整 | 原因必填、0~100 封顶，写 `parent_add`/`parent_sub` 流水 |
| 21 | 孩子不能改数据 | 服务端 `parent_required/child_required`，孩子账号任何写接口 403 |
| 22 | 孩子多处查看 | 孩子登录首页只看自己的两罐/任务/提现，全站只读自己 |

## 技术栈

- Python 3.9+，唯一第三方依赖 Flask
- SQLite（Python 内置 `sqlite3`），无需额外数据库
- 原生 HTML/CSS/JS（Jinja2 模板，手机优先响应式），无前端构建
- 密码 `werkzeug.security` pbkdf2 哈希；CSRF token 防护

## 目录结构

```
jar-reward-system/
├── app.py            # Flask 主应用：路由 + 权限 + 全部核心业务逻辑
├── database.py       # SQLite 连接管理与 schema 初始化
├── manage.py         # 命令行工具：init-db / create-user / run / list-users
├── schema.sql        # 表结构：users / jars / daily_tasks / transactions / withdrawals
├── requirements.txt  # flask>=2.2
├── static/           # style.css / app.js（原生 JS，二次确认 + 快捷金额）
├── templates/        # base / login / index / rules / history / parent / withdraw / error
└── tests/
    └── test_app.py   # 业务规则自动化测试（pytest）
```

## 快速开始

```bash
cd jar-reward-system
pip install -r requirements.txt

# 1. 初始化数据库（幂等，可重复执行）
python manage.py init-db

# 2. 创建家长账号
python manage.py create-user --username 爸爸 --role parent

# 3. 创建孩子账号
python manage.py create-user --username 小明 --role child

# 4. 启动服务（默认 0.0.0.0:5000，局域网手机可直接访问）
python manage.py run

# 查看账号
python manage.py list-users
```

浏览器访问 `http://127.0.0.1:5000`。
首次运行也可直接打开 `http://127.0.0.1:5000/login`，在页面引导下创建家长账号（零账号状态自动进入初始化向导）。

### 手机 / Android 模拟器访问

- **局域网**：手机与电脑同一 Wi-Fi，访问 `http://<电脑IP>:5000`（Windows 防火墙需放行 5000 端口，管理页可查询 IP）。
- **Android 模拟器（adb）**：执行一次 `adb reverse tcp:5000 tcp:5000`，模拟器内访问 `http://127.0.0.1:5000`。

## 主要页面

| 页面 | 角色 | 说明 |
|------|------|------|
| 首页 `/` | 全体 | 两罐余额/进度 + 今日任务确认（家长视角）/ 查看（孩子视角） |
| 规则 `/rules` | 全体 | 激励机制说明 |
| 历史 `/history` | 全体 | 任务记录 / 金额流水 / 提现记录（家长看全部孩子，孩子只看自己） |
| 管理 `/parent` | 家长 | 多孩子管理：每日确认/修改任务、手工调整（原因必填）、创建孩子账号 |
| 提现 `/withdraw` | 孩子 | 单罐提现 100 / 双罐同时提现 400 |

## 运行测试

```bash
pip install pytest
python -m pytest tests/test_app.py -v
```

覆盖：+1/-1 结算、99→100 封顶、0 保底、重复结算拒绝、修改任务差额（含封顶边界）、单罐/双罐提现、非法提现拒绝、
原因必填、孩子账号 403、CSRF 拦截、事务回滚等。

## 常见问题

- **端口被占用**：`python manage.py run --port 5001`
- **重置数据**：停服务，删除 `jar.db` 后重新 `python manage.py init-db` 并建账号
- **多孩子与首页**：家长首页默认展示第一位孩子，多孩子完整管理在「家长管理」页

> 数据仅存于本机 `jar.db`（`app.py` 同级目录）。定期备份该文件即可备份全部数据。
