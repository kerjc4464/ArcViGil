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

# 确保目录存在
os.makedirs(SOULS_DIR, exist_ok=True)

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
    # 迁移旧表：tasks 补充 chat_context_past 列（决定发信时的冻结上下文，动态同步不覆盖它）
    try:
        c.execute("ALTER TABLE tasks ADD COLUMN chat_context_past TEXT DEFAULT ''")
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
    """接收前端发送的发信任务"""
    data = await request.json()
    tasks = data.get("tasks", [])
    chat_context = data.get("chat_context", "")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = time.time()
    
    for t in tasks:
        participants = json.dumps(t.get("participants", []))
        delay_hours = float(t.get("delay_hours", 0))
        topic = t.get("topic", "")
        trigger_at = now + (delay_hours * 3600)
        
        c.execute('''INSERT INTO tasks 
                     (participants, delay_hours, topic, chat_context, chat_context_past, created_at, trigger_at, status) 
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (participants, delay_hours, topic, chat_context, chat_context, now, trigger_at, "pending"))
    
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/tasks")
async def get_tasks():
    """供前端获取当前任务雷达（包含 pending 与 paused 暂停状态）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, participants, delay_hours, topic, trigger_at, status FROM tasks WHERE status IN ('pending', 'paused') ORDER BY trigger_at ASC")
    rows = c.fetchall()
    conn.close()
    
    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0],
            "participants": r[1],
            "delay_hours": r[2],
            "topic": r[3],
            "trigger_at": r[4],
            "status": r[5]
        })
    return {"status": "ok", "tasks": tasks}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """手动删除指定的待执行计划任务"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM tasks WHERE id = ? AND status IN ('pending', 'paused')", (task_id,))
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        return {"status": "ok", "message": "Task deleted"}
    else:
        return {"status": "error", "message": "Task not found or already completed"}

@app.post("/api/tasks/{task_id}/toggle")
async def toggle_task_status(task_id: int):
    """在 pending (启用中) 与 paused (禁用/暂停) 之间切换任务状态"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    if not row or row[0] not in ('pending', 'paused'):
        conn.close()
        return {"status": "error", "message": "Task not found or completed"}
    
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
    不修改任何字段，幂等安全。
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, subject, content, participants, created_at, summary FROM messages "
        "WHERE created_at > ? ORDER BY created_at DESC LIMIT ?",
        (since, limit)
    )
    rows = c.fetchall()
    conn.close()
    # 按时间正序返回（最旧的在前，方便前端 chunk1/chunk2 递推）
    messages = [
        {"id": r[0], "subject": r[1], "content": r[2],
         "participants": r[3], "created_at": r[4], "summary": r[5] or ""}
        for r in reversed(rows)
    ]
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
    """
    import scheduler
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT participants, topic, chat_context, chat_context_past FROM tasks WHERE id=? AND status IN ('pending', 'paused')", (task_id,))
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

    # 在主题和正文开头打上醒目的测试标记
    test_topic = f"[测试/TEST RUN] 来自 {' & '.join(participants)} 的一封信：{topic}"
    test_content = f"⚠️ [测试邮件 / TEST RUN] 此邮件为测试触发，内容不会记入对话记忆。\n{'='*40}\n\n{content}"

    success = scheduler.send_email(test_content, test_topic, participants, config)
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
