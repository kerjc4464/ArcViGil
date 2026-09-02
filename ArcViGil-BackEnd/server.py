import os
import sqlite3
import json
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

import logging

class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 过滤掉 heartbeat 请求，防止在黑框控制台中刷屏
        return "/api/heartbeat" not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

app = FastAPI(title="ArcViGil-BackEnd")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "database.db"
SOULS_DIR = "souls"
STICKERS_DIR = "stickers"

# 确保目录存在
os.makedirs(SOULS_DIR, exist_ok=True)
os.makedirs(STICKERS_DIR, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 配置表
    c.execute('''CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    # 任务表
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        participants TEXT,
        delay_hours REAL,
        topic TEXT,
        chat_context TEXT,
        created_at REAL,
        trigger_at REAL,
        status TEXT
    )''')
    # 消息表
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT DEFAULT '',
        content TEXT,
        participants TEXT DEFAULT '',
        created_at REAL,
        synced_to_st INTEGER DEFAULT 0
    )''')
    # 迁移旧表：补充 subject/participants/summary 列（如已存在则静默跳过）
    for col, default in [("subject", ""), ("participants", ""), ("summary", "")]:
        try:
            c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT DEFAULT '{default}'")
        except Exception:
            pass
    # 迁移旧表：messages 补充 attachments 列（表情附件文件名 JSON 数组）
    try:
        c.execute("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT '[]'")
    except Exception:
        pass
    # 迁移旧表：tasks 补充 chat_context_past 列（决定发信时的冻结上下文，动态同步不覆盖它）
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN chat_context_past TEXT DEFAULT ''")
    except Exception:
        pass
    # 迁移：tasks 新增 Resend 云端定时相关列
    for col, default in [("resend_email_id", "''"), ("schedule_mode", "'local'")]:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    # 迁移：tasks 重试机制相关列
    for col, ddl in [("retry_count", "INTEGER DEFAULT 0"), ("last_error", "TEXT DEFAULT ''"), ("next_retry_at", "REAL DEFAULT 0")]:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    conn.commit()
    conn.close()

init_db()

@app.get("/")
def index():
    return {"status": "ArcViGil Backend is running"}

@app.post("/api/heartbeat")
async def heartbeat(request: Request):
    data = await request.json()
    # TODO: 记录心跳时间，通知 scheduler
    return {"status": "ok"}

@app.post("/api/config")
async def update_config(request: Request):
    """接收前端推送的全量配置（API Key、SMTP、后端地址等）"""
    data = await request.json()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v)
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/schedule")
async def schedule_tasks(request: Request):
    """接收前端发送的发信任务

    双模式：
    - local: 原逻辑，写入 pending，靠 scheduler 线程惰性生成
    - resend_scheduled: 立刻生成内容+附件→调用 Resend 带 scheduled_at→写入 scheduled_remote+messages(created_at=trigger_at)
      若超 30d / SMTP / 缺少 Resend 配置，自动回退 local
    """
    import scheduler
    from datetime import datetime, timezone
    data = await request.json()
    tasks = data.get("tasks", [])
    chat_context = data.get("chat_context", "")

    config = scheduler.get_config()
    # 前端可显式传 scheduleMode，优先级高于 DB 配置
    req_mode = data.get("scheduleMode") or data.get("schedule_mode") or config.get("scheduleMode") or config.get("schedule_mode") or "local"
    # 兼容部分旧前端字段命名
    if isinstance(req_mode, str):
        req_mode = req_mode.strip().lower()
    use_resend_scheduled = (req_mode == "resend_scheduled")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = time.time()

    scheduled_results = []
    fallback_count = 0
    scheduled_count = 0

    for t in tasks:
        participants_raw = t.get("participants", [])
        participants = json.dumps(participants_raw)
        delay_hours = float(t.get("delay_hours", 0))
        topic = t.get("topic", "")
        trigger_at = now + (delay_hours * 3600)

        # 判断是否满足 Resend 云端条件
        should_use_resend = use_resend_scheduled
        fallback_reason = ""
        if should_use_resend:
            email_method = config.get("emailMethod", "resend")
            if email_method != "resend":
                should_use_resend = False
                fallback_reason = "emailMethod非resend，自动回退本地"
            elif delay_hours > 720:  # 30d = 720h
                should_use_resend = False
                fallback_reason = "超过30天上限，自动回退本地"
            elif not config.get("resendApiKey") or not config.get("targetEmail"):
                should_use_resend = False
                fallback_reason = "缺少Resend配置，自动回退本地"

        if should_use_resend:
            # 立刻生成内容（autoSync已禁用，now_block为空）
            try:
                content = scheduler.generate_email_content(topic, participants_raw, chat_context, "", config)
            except Exception as e:
                print(f"[Schedule] 生成内容异常 {e}，回退本地: {topic}")
                should_use_resend = False
                fallback_reason = f"生成失败:{e}"

        if should_use_resend:
            # 解析表情
            try:
                content, attachments = scheduler.resolve_stickers(content)
            except Exception as e:
                print(f"[Schedule] 解析表情异常 {e}")
                attachments = []
            attachment_names = [os.path.basename(p) for p in (attachments or [])]
            # 摘要
            try:
                sender_name_tmp = " & ".join(participants_raw) if participants_raw else "System"
                summary = scheduler.generate_summary(content, sender_name_tmp, config) if content else ""
            except Exception as e:
                print(f"[Schedule] 生成摘要异常 {e}")
                summary = ""
            # ISO8601 UTC
            try:
                scheduled_at_iso = datetime.fromtimestamp(trigger_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            except Exception:
                scheduled_at_iso = datetime.fromtimestamp(trigger_at).isoformat()

            ok, resend_id = scheduler.send_resend_scheduled_email(content, topic, participants_raw, config, scheduled_at_iso, attachments)
            if ok:
                # 任务写入 scheduled_remote
                c.execute('''INSERT INTO tasks 
                             (participants, delay_hours, topic, chat_context, chat_context_past, created_at, trigger_at, status, resend_email_id, schedule_mode) 
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                          (participants, delay_hours, topic, chat_context, chat_context, now, trigger_at, "scheduled_remote", resend_id or "", "resend_scheduled"))
                task_id = c.lastrowid
                # 消息写入，created_at=trigger_at 保证到点才注入
                sender_name = " & ".join(participants_raw) if participants_raw else "System"
                subject = f"来自 {sender_name} 的一封信：{topic}"
                c.execute(
                    "INSERT INTO messages (subject, content, participants, created_at, synced_to_st, summary, attachments) VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (subject, content, sender_name, trigger_at, summary, json.dumps(attachment_names))
                )
                scheduled_results.append({"task_id": task_id, "topic": topic, "trigger_at": trigger_at, "scheduled_at": scheduled_at_iso, "resend_id": resend_id or "", "status": "scheduled_remote"})
                scheduled_count += 1
                print(f"[Schedule] 云端定时成功 task_id={task_id} topic={topic} trigger_at={trigger_at} resend_id={resend_id}")
                continue
            else:
                print(f"[Schedule] Resend 定时失败，回退本地: {topic}")
                fallback_reason = "Resend API失败，自动回退本地"
                # fallthrough to local insert

        # 本地模式插入
        if fallback_reason:
            fallback_count += 1
            print(f"[Schedule] 回退本地[{fallback_reason}] topic={topic}")
            scheduled_results.append({"topic": topic, "trigger_at": trigger_at, "status": "pending_fallback", "reason": fallback_reason})

        mode_val = "local"
        c.execute('''INSERT INTO tasks 
                     (participants, delay_hours, topic, chat_context, chat_context_past, created_at, trigger_at, status, resend_email_id, schedule_mode) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                   (participants, delay_hours, topic, chat_context, chat_context, now, trigger_at, "pending", "", mode_val))

    conn.commit()
    conn.close()
    # 兼容旧前端只判断 status ok，新增字段供新前端使用
    return {"status": "ok", "scheduled": scheduled_results, "scheduled_count": scheduled_count, "fallback_count": fallback_count, "mode": req_mode}

@app.get("/api/tasks")
async def get_tasks():
    """供前端获取当前任务雷达（包含 pending / paused / scheduled_remote 云端 + failed 重试耗尽）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT id, participants, delay_hours, topic, trigger_at, status, resend_email_id, schedule_mode, retry_count, last_error FROM tasks WHERE status IN ('pending', 'paused', 'scheduled_remote', 'failed') ORDER BY trigger_at ASC")
        rows = c.fetchall()
    except Exception:
        # 兼容未迁移的旧表
        try:
            c.execute("SELECT id, participants, delay_hours, topic, trigger_at, status, resend_email_id, schedule_mode FROM tasks WHERE status IN ('pending', 'paused', 'scheduled_remote', 'failed') ORDER BY trigger_at ASC")
            rows = c.fetchall()
            rows = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], 0, "") for r in rows]
        except Exception:
            c.execute("SELECT id, participants, delay_hours, topic, trigger_at, status FROM tasks WHERE status IN ('pending', 'paused', 'scheduled_remote', 'failed') ORDER BY trigger_at ASC")
            rows = c.fetchall()
            rows = [(r[0], r[1], r[2], r[3], r[4], r[5], "", "local", 0, "") for r in rows]
    conn.close()
    
    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0],
            "participants": r[1],
            "delay_hours": r[2],
            "topic": r[3],
            "trigger_at": r[4],
            "status": r[5],
            "resend_email_id": r[6] if len(r) > 6 else "",
            "schedule_mode": r[7] if len(r) > 7 else "local",
            "retry_count": r[8] if len(r) > 8 and r[8] is not None else 0,
            "last_error": r[9] if len(r) > 9 and r[9] else ""
        })
    return {"status": "ok", "tasks": tasks}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """手动删除指定的待执行计划任务（云端任务会同步取消 Resend，failed 也允许删除）"""
    import scheduler
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 先查是否为云端任务
    try:
        c.execute("SELECT status, resend_email_id FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
    except Exception:
        row = None
    if row and row[0] == "scheduled_remote" and row[1]:
        config = scheduler.get_config()
        # 尝试取消 Resend 侧，失败也继续删本地（幂等）
        try:
            scheduler.cancel_resend_scheduled_email(row[1], config)
        except Exception as e:
            print(f"[Delete] 取消 Resend 异常 {e}")

    c.execute("DELETE FROM tasks WHERE id = ? AND status IN ('pending', 'paused', 'scheduled_remote', 'failed')", (task_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        return {"status": "ok", "message": "Task deleted"}
    else:
        return {"status": "error", "message": "Task not found or already completed"}

@app.post("/api/tasks/{task_id}/retry")
async def retry_failed_task(task_id: int):
    """手动重试一个已失败的任务：重置为 pending，1秒后可被调度器重新执行"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"status": "error", "message": "Task not found"}
    if row[0] != "failed":
        conn.close()
        return {"status": "error", "message": f"仅 failed 状态可重试，当前为 {row[0]}"}
    now = time.time()
    try:
        c.execute("UPDATE tasks SET status='pending', trigger_at=?, retry_count=0, last_error='' WHERE id=?", (now, task_id))
    except Exception:
        c.execute("UPDATE tasks SET status='pending', trigger_at=? WHERE id=?", (now, task_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "Task retried", "trigger_at": now}

@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task_status(task_id: int):
    """在 pending (启用中) 与 paused (禁用/暂停) 之间切换任务状态

    云端 scheduled_remote 不支持暂停（Resend 取消后不可恢复），返回错误提示前端。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT status, schedule_mode FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
    except Exception:
        c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        row = c.fetchone()
        if row:
            row = (row[0], "local")
    if not row or row[0] not in ('pending', 'paused'):
        # scheduled_remote 单独提示
        if row and row[0] == 'scheduled_remote':
            conn.close()
            return {"status": "error", "message": "云端定时任务不支持暂停，请直接删除（将同步取消Resend）"}
        conn.close()
        return {"status": "error", "message": "Task not found or completed"}
    
    # 再次拦截云端模式
    schedule_mode = row[1] if len(row) > 1 else "local"
    if schedule_mode == "resend_scheduled" or row[0] == "scheduled_remote":
        conn.close()
        return {"status": "error", "message": "云端定时任务不支持暂停（取消后不可恢复），请删除重建"}

    new_status = 'paused' if row[0] == 'pending' else 'pending'
    c.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "new_status": new_status}

@app.patch("/api/tasks/context")
async def update_tasks_context(request: Request):
    """
    将所有 pending 任务的 chat_context 更新为最新聊天记录（History-now）。
    只更新 chat_context，不动 chat_context_past（发信动机保持冻结）。
    仅在前端启用「动态同步」开关时被调用。
    """
    data = await request.json()
    new_context = data.get("chat_context", "")
    if not new_context:
        return {"status": "error", "message": "chat_context 为空"}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE tasks SET chat_context = ? WHERE status = 'pending'",
        (new_context,)
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    return {"status": "ok", "updated_tasks": updated}

@app.get("/api/souls")
async def list_souls():
    """列出 souls/ 目录下所有已注册的角色 Soul 文件"""
    souls = []
    if os.path.isdir(SOULS_DIR):
        for fname in os.listdir(SOULS_DIR):
            fpath = os.path.join(SOULS_DIR, fname)
            if os.path.isfile(fpath):
                name, ext = os.path.splitext(fname)
                stat = os.stat(fpath)
                souls.append({
                    "name": name,
                    "filename": fname,
                    "format": ext.lstrip(".").upper(),
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                })
    return {"status": "ok", "souls": souls}

def _safe_soul_path(filename: str) -> str:
    # 防止路径穿越，只允许单文件名
    fname = os.path.basename(str(filename or "").strip())
    if not fname:
        raise ValueError("filename 不能为空")
    # 限制扩展名
    base_dir = os.path.abspath(SOULS_DIR)
    os.makedirs(base_dir, exist_ok=True)
    full = os.path.abspath(os.path.join(base_dir, fname))
    if not full.startswith(base_dir + os.sep) and full != base_dir:
        raise ValueError("非法路径")
    return full

class SoulWrite(BaseModel):
    filename: str
    content: str
    name: Optional[str] = None

@app.get("/api/souls/{filename}")
async def read_soul(filename: str):
    """读取单個 Soul 原文 — 供 ArcHarness 单向同步 EX→ViGil"""
    try:
        full = _safe_soul_path(filename)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    if not os.path.isfile(full):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Soul not found")
    from fastapi.responses import PlainTextResponse
    try:
        with open(full, "r", encoding="utf-8") as f:
            txt = f.read()
    except UnicodeDecodeError:
        with open(full, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    return PlainTextResponse(txt, media_type="text/plain; charset=utf-8")

@app.post("/api/souls/write")
async def write_soul(payload: SoulWrite):
    """写入/覆盖单個 Soul — ArcHarness EX→ViGil 单向同步"""
    try:
        full = _safe_soul_path(payload.filename)
    except ValueError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    # 写入
    with open(full, "w", encoding="utf-8") as f:
        f.write(payload.content or "")
    stat = os.stat(full)
    return {"status": "ok", "filename": os.path.basename(full), "size_bytes": stat.st_size, "modified_at": stat.st_mtime}

@app.post("/api/souls")
async def create_soul_alias(payload: SoulWrite):
    """兼容别名: POST /api/souls 同 write"""
    return await write_soul(payload)

@app.put("/api/souls/{filename}")
async def put_soul(filename: str, request: Request):
    """兼容别名: PUT /api/souls/{filename} 支持 text/plain 或 json"""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        data = await request.json()
        content = data.get("content", "")
    else:
        raw = await request.body()
        try:
            content = raw.decode("utf-8")
        except:
            content = raw.decode("utf-8", errors="ignore")
        # 若 body 是 json 字符串包裹
        if content.strip().startswith("{"):
            try:
                j = json.loads(content)
                if isinstance(j, dict) and "content" in j:
                    content = j["content"]
            except:
                pass
    payload = SoulWrite(filename=filename, content=content)
    return await write_soul(payload)

@app.get("/api/stickers")
async def list_stickers():
    """列出 stickers/ 目录下所有表情，按文件夹分组返回"""
    import scheduler
    _, catalog = scheduler.scan_stickers()
    return {"status": "ok", "groups": catalog}

@app.get("/api/stickers/image")
async def sticker_image(folder: str = "", file: str = ""):
    """返回表情图片（前端缩略图预览用），带路径穿越防护。folder 为空表示 stickers/ 根目录（未分组）"""
    from fastapi.responses import FileResponse
    if not file:
        return {"status": "error", "message": "file 参数不能为空"}
    safe_folder = os.path.basename(folder)
    safe_file = os.path.basename(file)
    base = os.path.abspath(STICKERS_DIR)
    target = os.path.abspath(os.path.join(base, safe_folder, safe_file)) if safe_folder else os.path.abspath(os.path.join(base, safe_file))
    if not target.startswith(base + os.sep) or not os.path.isfile(target):
        return {"status": "error", "message": "表情不存在"}
    return FileResponse(target)

@app.get("/api/pull_messages")
async def pull_messages():
    """供前端拉取已发送但尚未投递邮箱的邮件（一次性标记）——已废弃，保留兼容"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, subject, content, participants, created_at FROM messages WHERE synced_to_st=0")
    rows = c.fetchall()
    messages = []
    for r in rows:
        messages.append({
            "id": r[0], "subject": r[1], "content": r[2],
            "participants": r[3], "created_at": r[4]
        })
        c.execute("UPDATE messages SET synced_to_st=1 WHERE id=?", (r[0],))
    conn.commit()
    conn.close()
    return {"status": "ok", "messages": messages}

@app.get("/api/messages")
async def get_messages(limit: int = 5, since: float = 0):
    """
    持久化查询信件历史。
    limit: 最多返回最近几封（对应UI「最大注入信件数」）
    since: 只返回 created_at > since 的记录（对应用户「清空注入记忆」后的时间戳）
    时间门控：只返回 created_at <= now 的记录，确保云端定时（created_at=trigger_at 未来值）到点才注入。
    不修改任何字段，幂等安全。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = time.time()
    c.execute(
        "SELECT id, subject, content, participants, created_at, summary, attachments FROM messages "
        "WHERE created_at > ? AND created_at <= ? ORDER BY created_at DESC LIMIT ?",
        (since, now, limit)
    )
    rows = c.fetchall()
    conn.close()
    # 按时间正序返回（最旧的在前，方便前端 chunk1/chunk2 递推）
    messages = []
    for r in reversed(rows):
        try:
            atts = json.loads(r[6]) if r[6] else []
        except Exception:
            atts = []
        messages.append({"id": r[0], "subject": r[1], "content": r[2],
                         "participants": r[3], "created_at": r[4], "summary": r[5] or "",
                         "attachments": atts})
    return {"status": "ok", "messages": messages}

@app.post("/api/messages/ack")
async def ack_messages():
    """
    用户点击「清空已注入信件」时调用。
    在 config 表里写入当前时间戳作为 inject_acked_at。
    前端下次拉 /api/messages 时带上这个时间戳，只会看到更新的信件。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = time.time()
    c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('inject_acked_at', ?)", (str(now),))
    conn.commit()
    conn.close()
    return {"status": "ok", "acked_at": now}

@app.post("/api/test_email")
async def test_email(request: Request):
    """测试邮件发送配置（连通性测试）"""
    config = await request.json()
    import scheduler
    content = "你好，这是一封来自 ArcViGil 系统的测试邮件。如果你收到这封信，说明你的邮件投递配置完全正确，量子通信网络连接正常！"
    topic = "ArcViGil 系统连通性测试"
    participants = ["System"]
    success = scheduler.send_email(content, topic, participants, config)
    if success:
        return {"status": "ok", "message": "Test email sent successfully"}
    else:
        return {"status": "error", "message": "Failed to send test email"}

@app.post("/api/test_llm")
async def test_llm(request: Request):
    """
    从后端测试 LLM API 连通性（绕过浏览器 CORS 限制）。
    发送一条极短的请求，返回模型回复、延迟和规范化后的实际 URL。
    """
    import scheduler, time as _time
    data = await request.json()
    raw_url = data.get("apiUrl",   "")
    api_key = data.get("apiKey",   "")
    model   = data.get("apiModel", "gpt-4o")

    if not raw_url or not api_key:
        return {"status": "error", "message": "apiUrl 和 apiKey 不能为空"}

    api_base = scheduler.normalize_api_base(raw_url)
    endpoint = f"{api_base}/chat/completions"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi, reply with just one word: OK"}],
        "max_tokens": 10,
        "temperature": 0,
    }

    t0 = _time.time()
    try:
        import requests as _req
        res = _req.post(endpoint, headers=headers, json=payload, timeout=20)
        latency_ms = int((_time.time() - t0) * 1000)
        if res.status_code == 200:
            reply = res.json()["choices"][0]["message"]["content"].strip()
            return {"status": "ok", "message": f"连接成功，延迟 {latency_ms}ms",
                    "reply": reply, "normalized_url": endpoint, "latency_ms": latency_ms}
        else:
            return {"status": "error", "normalized_url": endpoint,
                    "message": f"HTTP {res.status_code}: {res.text[:300]}"}
    except Exception as e:
        latency_ms = int((_time.time() - t0) * 1000)
        return {"status": "error", "normalized_url": endpoint,
                "message": f"请求异常 ({latency_ms}ms): {str(e)[:200]}"}

@app.post("/api/tasks/{task_id}/test")
async def test_task(task_id: int):
    """
    测试执行指定任务：
    - 立即调用 LLM 生成信件内容
    - 在邮件主题和正文开头醒目标出 [测试/TEST RUN]
    - 真实发送邮件（让你在收件箱看到效果）
    - 不修改任务状态，不写入 messages 表（不污染主上下文）
    - 兼容 scheduled_remote 云端任务（同样走即时生成测试）
    """
    import scheduler
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT participants, topic, chat_context, chat_context_past FROM tasks WHERE id=? AND status IN ('pending', 'paused', 'scheduled_remote')", (task_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return {"status": "error", "message": "Task not found or not pending"}

    participants = json.loads(row[0])
    topic = row[1]
    chat_context = row[2]
    chat_context_past = row[3]
    config = scheduler.get_config()

    # 生成内容
    content = scheduler.generate_email_content(topic, participants, chat_context_past, chat_context, config)

    # 解析表情标记 → 剥离正文 + 收集附件
    content, attachments = scheduler.resolve_stickers(content)

    # 在主题和正文开头打上醒目的测试标记
    test_topic = f"[测试/TEST RUN] 来自 {' & '.join(participants)} 的一封信：{topic}"
    test_content = f"⚠️ [测试邮件 / TEST RUN] 此邮件为测试触发，内容不会记入对话记忆。\n{'='*40}\n\n{content}"

    success = scheduler.send_email(test_content, test_topic, participants, config, attachments)
    if success:
        return {"status": "ok", "message": "Test email sent (not saved to DB)", "content": content}
    else:
        return {"status": "error", "message": "Failed to send test email"}

if __name__ == "__main__":
    import uvicorn
    # 启动时可选择一并启动 scheduler 线程
    import threading
    import scheduler
    t = threading.Thread(target=scheduler.run_scheduler_loop, daemon=True)
    t.start()
    print("[ArcViGil] Backend and Scheduler started.")
    # 绑定 0.0.0.0 使局域网内的设备也能访问后端
    # 关闭 uvicorn 颜色输出以防 Windows 终端乱码
    uvicorn.run(app, host="0.0.0.0", port=9000, use_colors=False)
