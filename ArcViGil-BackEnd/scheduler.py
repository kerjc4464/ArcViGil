import sqlite3
import time
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

DB_PATH = "database.db"
SOULS_DIR = "souls"

def get_config():
    """获取所有前端配置"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT key, value FROM config")
        rows = c.fetchall()
        config = {}
        for r in rows:
            config[r[0]] = r[1]
        return config
    except:
        return {}
    finally:
        conn.close()

def load_souls(participants):
    """万能 Soul 加载器：支持 json, yaml, md, txt"""
    soul_texts = []
    for p in participants:
        found = False
        for ext in ['.json', '.yaml', '.yml', '.md', '.txt']:
            path = os.path.join(SOULS_DIR, f"{p}{ext}")
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        soul_texts.append(f"--- 角色设定 [{p}] ---\\n{f.read()}")
                    found = True
                    break
                except Exception as e:
                    print(f"[Scheduler] 加载 {path} 失败: {e}")
        if not found:
            soul_texts.append(f"--- 角色设定 [{p}] ---\\n(未找到设定的默认角色)")
    
    return "\\n".join(soul_texts)

DEFAULT_PROMPT_TEMPLATE = """你现在是以下角色：
{souls}

【决定发信时的聊天记录】（你当时发信的动机，此后不再变化）：
{chat_context_past}

你定下的发信主题是：【{topic}】。

{chat_context_now_block}

现在时间已到，请结合以上所有信息——角色设定、发信动机、主题，以及发信前的最新对话——用最符合你身份的语气，直接写出邮件的正文。只输出邮件正文，不要有多余的格式和废话。"""

def normalize_api_base(url: str) -> str:
    """
    规范化 API 基础 URL：
    如果用户填的是完整的 .../chat/completions 地址，自动剪掉后缀，
    避免后端拼接时出现 /chat/completions/chat/completions 的问题。
    """
    stripped = url.rstrip('/')
    # 兵兼用户扫尾 /v1/chat/completions 或 /chat/completions 的情况
    for suffix in ['/chat/completions', '/completions']:
        if stripped.endswith(suffix):
            stripped = stripped[:-len(suffix)]
            break
    return stripped

def generate_email_content(topic, participants, chat_context_past, chat_context_now, config):
    """独立调用大模型生成信件内容，支持从 config 读取提示词模板和所有生成参数"""
    api_url   = normalize_api_base(config.get("apiUrl", "https://api.openai.com/v1"))
    api_key   = config.get("apiKey",   "")
    api_model = config.get("apiModel", "gpt-4o")

    # 从 DB 读取提示词模板，若未设置则用内置默认值
    prompt_template = config.get("promptTemplate", "").strip() or DEFAULT_PROMPT_TEMPLATE

    souls_content = load_souls(participants)

    # History(now) 总开关：关闭动态同步时，既不更新也不注入最新聊天记录
    auto_sync = config.get("autoSyncContext", "false") == "true"
    now_context = chat_context_now if (auto_sync and chat_context_now) else ""
    now_block = f"【从决定发信到现在的聊天记录】（这段时间发生了什么）：\n{now_context}" if now_context else ""

    # 用占位符渲染最终提示词
    # {chat_context} 保留为 History(now) 的别名，兼容旧的自定义模板
    system_prompt = prompt_template.format(
        souls=souls_content,
        chat_context_past=chat_context_past or "",
        chat_context=now_context,
        chat_context_now_block=now_block,
        topic=topic,
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 组装生成参数，从 config 读取，提供合理默认值
    payload = {
        "model": api_model,
        "messages": [{"role": "system", "content": system_prompt}],
        "temperature": float(config.get("temperature", 0.7)),
        "top_p":       float(config.get("topP",        1.0)),
        "max_tokens":  int(config.get("maxTokens",     1024)),
    }

    # top_k 仅在非零时加入（OpenAI 不支持，Ollama/KoboldCPP 等本地推理支持）
    top_k = int(config.get("topK", 0))
    if top_k > 0:
        payload["top_k"] = top_k

    print(f"[Scheduler] 为 {participants} 调用模型生成关于 '{topic}' 的邮件 "
          f"(temp={payload['temperature']}, top_p={payload['top_p']}, max_tokens={payload['max_tokens']})...")
    try:
        res = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Scheduler] 大模型调用失败: {e}")
        return f"(由于系统异常，这封关于 {topic} 的信件丢失在了虚空中...)"


DEFAULT_SUMMARY_PROMPT = """请用70到100字概括以下邮件内容，保留发信人身份和核心信息，
仅输出摘要文本，不要任何额外说明：

发信人：{participants}
邮件正文：
{content}"""

def generate_summary(content, participants_str, config):
    """生成邮件摘要（70-100字）。摘要LLM可独立配置或复用主LLM。"""
    if config.get("summaryEnabled", "true") != "true":
        return ""

    # 选择 LLM 来源
    if config.get("summaryUseMainLLM", "true") == "true":
        api_url = normalize_api_base(config.get("apiUrl", "https://api.openai.com/v1"))
        api_key = config.get("apiKey", "")
        api_model = config.get("apiModel", "gpt-4o")
    else:
        api_url = normalize_api_base(config.get("summaryApiUrl", ""))
        api_key = config.get("summaryApiKey", "")
        api_model = config.get("summaryApiModel", "gpt-4o-mini")

    if not api_key:
        print("[Scheduler] 摘要生成跳过：缺少 API Key")
        return ""

    prompt_template = config.get("summaryPromptTemplate", "").strip() or DEFAULT_SUMMARY_PROMPT
    system_prompt = prompt_template.format(participants=participants_str, content=content)

    temperature = float(config.get("summaryTemperature", 0.5))
    max_tokens  = int(config.get("summaryMaxTokens", 200))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": api_model,
        "messages": [{"role": "user", "content": system_prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    print(f"[Scheduler] 为 {participants_str} 生成摘要 (model={api_model})...")
    try:
        res = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        res.raise_for_status()
        summary = res.json()["choices"][0]["message"]["content"].strip()
        print(f"[Scheduler] 摘要生成完成 ({len(summary)}字): {summary[:60]}...")
        return summary
    except Exception as e:
        print(f"[Scheduler] 摘要生成失败: {e}")
        return ""


def send_smtp_email(content, topic, participants, config):
    """跨次元 SMTP 投递"""
    server = config.get("smtpServer", "")
    port = int(config.get("smtpPort", 465))
    user = config.get("smtpUser", "")
    password = config.get("smtpPass", "")
    target = config.get("targetEmail", "")
    
    if not server or not user or not target:
        print("[Scheduler] SMTP 缺少配置，无法发送真实邮件。")
        return False
        
    sender_name = " & ".join(participants)
    subject = f"来自 {sender_name} 的一封信：{topic}"
    
    msg = MIMEMultipart()
    msg['From'] = f"{sender_name} <{user}>"
    msg['To'] = target
    msg['Subject'] = subject
    msg.attach(MIMEText(content, 'plain', 'utf-8'))
    
    try:
        # 兼容 SSL (465) 和 STARTTLS (587)
        if port == 465:
            with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(server, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(user, password)
                smtp.send_message(msg)
        print(f"[Scheduler] SMTP 发送成功至 {target}")
        return True
    except Exception as e:
        print(f"[Scheduler] SMTP 发送失败: {e}")
        return False


def send_resend_email(content, topic, participants, config):
    """通过 Resend HTTP API 发信 (绕过所有 SMTP 端口限制)"""
    api_key = config.get("resendApiKey", "")
    target  = config.get("targetEmail", "")
    from_addr = config.get("resendFrom", "ArcViGil <onboarding@resend.dev>")

    if not api_key or not target:
        print("[Scheduler] Resend 缺少 API Key 或目标邮箱，无法发送。")
        return False

    sender_name = " & ".join(participants)
    subject = f"来自 {sender_name} 的一封信：{topic}"

    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [target],
                "subject": subject,
                "text": content,
            },
            timeout=30,
        )
        if res.status_code in (200, 201):
            print(f"[Scheduler] Resend 发送成功至 {target}")
            return True
        else:
            print(f"[Scheduler] Resend 发送失败: {res.status_code} {res.text}")
            return False
    except Exception as e:
        print(f"[Scheduler] Resend 请求异常: {e}")
        return False


def send_email(content, topic, participants, config):
    """统一发信入口：根据 emailMethod 配置选择 SMTP 或 Resend"""
    method = config.get("emailMethod", "smtp")
    if method == "resend":
        return send_resend_email(content, topic, participants, config)
    else:
        return send_smtp_email(content, topic, participants, config)

def process_due_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = time.time()
    
    # 查找所有到期且未执行的任务
    c.execute("SELECT id, participants, topic, chat_context, chat_context_past FROM tasks WHERE status='pending' AND trigger_at <= ?", (now,))
    due_tasks = c.fetchall()
    
    if not due_tasks:
        conn.close()
        return

    config = get_config()
    
    for row in due_tasks:
        task_id = row[0]
        participants = json.loads(row[1])
        topic = row[2]
        chat_context = row[3]
        chat_context_past = row[4]
        
        print(f"[Scheduler] 开始执行任务 {task_id}: {participants} -> {topic}")
        
        # 1. 生成内容（past 冻结 + now 动态，now 受 autoSyncContext 总开关控制）
        content = generate_email_content(topic, participants, chat_context_past, chat_context, config)
        
        # 2. 发送邮件 (自动根据 emailMethod 配置选择 SMTP 或 Resend)
        send_email(content, topic, participants, config)
        
        # 3. 生成摘要（70-100字），失败静默留空
        sender_name = " & ".join(participants)
        summary = generate_summary(content, sender_name, config) if content else ""

        # 4. 保存到消息库以供 ST 记忆注入（含主题+参与者+内容+摘要）
        subject = f"来自 {sender_name} 的一封信：{topic}"
        c.execute(
            "INSERT INTO messages (subject, content, participants, created_at, synced_to_st, summary) VALUES (?, ?, ?, ?, 0, ?)",
            (subject, content, sender_name, time.time(), summary)
        )
        
        # 5. 标记任务完成
        c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
        conn.commit()

    conn.close()

def run_scheduler_loop():
    print("[Scheduler] 调度器线程已启动...")
    while True:
        try:
            process_due_tasks()
        except Exception as e:
            print(f"[Scheduler] 调度循环异常: {e}")
        time.sleep(10) # 每10秒检查一次
