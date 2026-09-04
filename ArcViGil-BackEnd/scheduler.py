import sqlite3
import time
import json
import os
import re
import string
import base64
import mimetypes
import smtplib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
import requests

DB_PATH = "database.db"
SOULS_DIR = "souls"
STICKERS_DIR = "stickers"
MAX_STICKERS_PER_EMAIL = 10  # 每封邮件表情附件软上限（邮箱服务商普遍有附件体积限制）

os.makedirs(STICKERS_DIR, exist_ok=True)

def connect_db():
    """统一建连：busy 超时 + WAL，避免单任务异常泄漏连接后整库 'database is locked' 卡死。

    调用方仍需在 finally 里 close()，本函数只保证等待锁而不是立刻抛错。
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn

# 支持的图片格式（附件型，电子邮件客户端普遍兼容 png/jpg/gif）
STICKER_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
# 同名多格式时的优先级（数字越小越优先，png/jpg 优先于 gif/webp）
STICKER_EXT_PRIORITY = {'.png': 0, '.jpg': 0, '.jpeg': 0, '.gif': 1, '.webp': 1, '.bmp': 2}

def get_config():
    """获取所有前端配置"""
    conn = connect_db()
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
    """万能 Soul 加载器：支持 json, yaml, md, txt

    增强：若 participants 使用简称（如 "朱月"），而文件为 "朱月·布伦史塔德.txt"，
    会做模糊匹配（前缀/包含），避免“未找到设定的默认角色”导致角色失真。
    """
    soul_texts = []
    # 预扫描一次 souls 目录，建立 name -> path 映射（去扩展名）
    available = {}
    if os.path.isdir(SOULS_DIR):
        for fname in os.listdir(SOULS_DIR):
            fpath = os.path.join(SOULS_DIR, fname)
            if os.path.isfile(fpath):
                name, ext = os.path.splitext(fname)
                if ext.lower() in ('.json', '.yaml', '.yml', '.md', '.txt'):
                    # 同名多格式时保留第一个
                    if name not in available:
                        available[name] = fpath

    def _fuzzy_find(p):
        # 1) 精确
        if p in available:
            return available[p], p
        # 2) 前缀 / 包含（处理 "爱尔奎特" -> "爱尔奎特·布伦史塔德"）
        # 优先前缀匹配，其次包含匹配，取最短的那个（最贴近）
        candidates = []
        for name, fpath in available.items():
            if name.startswith(p) or p.startswith(name) or p in name or name in p:
                candidates.append((len(name), name, fpath))
        if candidates:
            candidates.sort()
            _, best_name, best_path = candidates[0]
            return best_path, best_name
        return None, None

    for p in participants:
        found = False
        # 先按原逻辑精确扩展名匹配（兼容全名）
        for ext in ['.json', '.yaml', '.yml', '.md', '.txt']:
            path = os.path.join(SOULS_DIR, f"{p}{ext}")
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        soul_texts.append(f"--- 角色设定 [{p}] ---\n{f.read()}")
                    print(f"[Scheduler] 精确加载 Soul: {p} -> {path}")
                    found = True
                    break
                except Exception as e:
                    print(f"[Scheduler] 加载 {path} 失败: {e}")
        if found:
            continue
        # 模糊匹配
        fpath, real_name = _fuzzy_find(p)
        if fpath and real_name:
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    soul_texts.append(f"--- 角色设定 [{p} / {real_name}] ---\n{f.read()}")
                print(f"[Scheduler] 模糊加载 Soul: 请求={p} 命中={real_name} -> {fpath}")
                found = True
            except Exception as e:
                print(f"[Scheduler] 模糊加载 {fpath} 失败: {e}")
        if not found:
            print(f"[Scheduler] 警告: Soul 未找到: {p} (可用: {list(available.keys())})")
            soul_texts.append(f"--- 角色设定 [{p}] ---\n(未找到设定的默认角色，系统已提示可用列表: {', '.join(available.keys()) or '空'})")
    
    return "\n".join(soul_texts)


def scan_stickers():
    """扫描 stickers/ 下的表情图片（子文件夹按组展示，根目录直接放置的归入「未分组」）。

    返回 (stem_map, catalog)：
    - stem_map: {文件名去扩展名: 相对路径} 全局查找表（跨文件夹平铺，重名时 png/jpg 优先，其余取首个 + 警告）
    - catalog: 按文件夹分组 [{folder, stickers: [{name, filename, size_bytes, modified_at}]}]
    """
    stem_map = {}
    catalog = []
    if not os.path.isdir(STICKERS_DIR):
        return stem_map, catalog

    def register(fpath, folder_stickers):
        fname = os.path.basename(fpath)
        stem, ext = os.path.splitext(fname)
        if ext.lower() not in STICKER_EXTENSIONS:
            return
        stat = os.stat(fpath)
        entry = {"name": stem, "filename": fname,
                 "size_bytes": stat.st_size, "modified_at": stat.st_mtime}
        if stem in stem_map:
            existing = stem_map[stem]
            old_pri = STICKER_EXT_PRIORITY.get(os.path.splitext(existing)[1].lower(), 1)
            new_pri = STICKER_EXT_PRIORITY.get(ext.lower(), 1)
            if new_pri < old_pri:
                print(f"[Sticker] 提示: [{stem}] 存在多个格式，优先使用 {fpath}")
                stem_map[stem] = fpath
                folder_stickers[stem] = entry
            else:
                print(f"[Sticker] 警告: 表情 [{stem}] 重名（{existing} 与 {fpath}），仅使用第一个")
            return
        stem_map[stem] = fpath
        folder_stickers[stem] = entry

    # 根目录直接放置的图片 → 未分组收藏
    root_stickers = {}
    for fname in sorted(os.listdir(STICKERS_DIR)):
        fpath = os.path.join(STICKERS_DIR, fname)
        if os.path.isfile(fpath):
            register(fpath, root_stickers)
    if root_stickers:
        stickers = [root_stickers[s] for s in sorted(root_stickers)]
        catalog.append({"folder": "未分组", "stickers": stickers})

    # 子文件夹分组
    for folder in sorted(os.listdir(STICKERS_DIR)):
        folder_path = os.path.join(STICKERS_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        folder_stickers = {}
        for fname in sorted(os.listdir(folder_path)):
            register(os.path.join(folder_path, fname), folder_stickers)
        stickers = [folder_stickers[s] for s in sorted(folder_stickers)]
        if stickers:
            catalog.append({"folder": folder, "stickers": stickers})
    return stem_map, catalog


def load_sticker_catalog():
    """生成注入提示词的 表情目录 + 使用规则 文本块（无表情时返回空串）"""
    _, catalog = scan_stickers()
    if not catalog:
        return ""
    lines = ["【表情】以下表情按文件夹分组展示。你可以不用，也可以在一封信里用多个（允许跨组混用）："]
    for group in catalog:
        markers = " ".join(f"[{s['name']}]" for s in group["stickers"])
        lines.append(f"[{group['folder']}] {markers}")
    lines.append("需要附表情时，在正文合适位置直接输出 [表情名] 标记（如 [Doro-happy]），系统会自动把对应图片作为邮件附件。")
    return "\n".join(lines)


STICKER_MARKER_RE = re.compile(r"\[([^\[\]]+)\]")

def resolve_stickers(content):
    """解析正文中的 [表情名] 标记。

    - 只剥离能匹配到已注册表情的标记，其余 [xxx] 原样保留（防误伤普通文本）
    - 返回 (清洗后的正文, 附件路径列表)
    - 超过 MAX_STICKERS_PER_EMAIL 的部分只剥离不附加，并在日志提示
    """
    stem_map, _ = scan_stickers()
    if not stem_map:
        return content, []
    attachments = []

    def replace(m):
        stem = m.group(1).strip()
        path = stem_map.get(stem)
        if path is None:
            return m.group(0)
        if len(attachments) < MAX_STICKERS_PER_EMAIL:
            attachments.append(path)
        else:
            print(f"[Sticker] 提示: 表情数量超过上限 {MAX_STICKERS_PER_EMAIL}，[{stem}] 未附加")
        return ""

    cleaned = STICKER_MARKER_RE.sub(replace, content)
    return cleaned, attachments

DEFAULT_PROMPT_TEMPLATE = """你现在是以下角色：
{souls}

【决定发信时的聊天记录】（你当时发信的动机，此后不再变化）：
{chat_context_past}

你定下的发信主题是：【{topic}】。

{chat_context_now_block}

{stickers}

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

def _get_timeout(config, key, default):
    """从 config 读取超时，兼容字符串/缺失/非法值，返回 int 秒"""
    try:
        v = config.get(key, str(default))
        # 允许 "300" 或 300
        iv = int(float(str(v).strip()))
        # 钳位 10~600s，避免误填 0 导致立即超时
        if iv < 10:
            iv = 10
        if iv > 600:
            iv = 600
        return iv
    except Exception:
        return default

def _get_int(config, key, default, min_v=None, max_v=None):
    try:
        v = config.get(key, str(default))
        iv = int(float(str(v).strip()))
        if min_v is not None and iv < min_v:
            iv = min_v
        if max_v is not None and iv > max_v:
            iv = max_v
        return iv
    except Exception:
        return default

def _is_failure_content(content, topic=""):
    """判断是否为占位失败内容：空、或包含‘虚空中’/‘系统异常’"""
    if not content or not content.strip():
        return True, "empty_content"
    if "虚空中" in content or "系统异常" in content:
        return True, "placeholder_虚空中"
    # 有时模型返回过短（<5字）也视为异常，可按需放宽
    if len(content.strip()) < 5:
        return True, "too_short"
    return False, ""

def _ensure_task_retry_columns(conn):
    """确保 tasks 表包含重试相关列，旧库自动迁移"""
    c = conn.cursor()
    for col, ddl in [
        ("retry_count", "INTEGER DEFAULT 0"),
        ("last_error", "TEXT DEFAULT ''"),
        ("next_retry_at", "REAL DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    # status 列无需迁移，已支持任意文本

def safe_format(template, **kwargs):
    """安全渲染提示词模板：未知 {占位符} 原样保留，不抛 KeyError。

    背景：用户自定义模板里一旦出现示例 JSON 的 {…}，str.format 会 KeyError，
    而该异常在调度循环里未被隔离，会 abort 整批任务并泄漏 SQLite 连接。
    """
    fmt = string.Formatter()
    out = []
    for literal, field, spec, conv in fmt.parse(template):
        out.append(literal)
        if field is None:
            continue
        if field == "":
            out.append("{}")
            continue
        if field in kwargs:
            v = kwargs[field]
            try:
                if conv:
                    v = fmt.convert_field(v, conv)
                out.append(fmt.format_field(v, spec))
            except Exception:
                out.append(str(v))
        else:
            piece = "{" + field
            if conv:
                piece += "!" + conv
            if spec:
                piece += ":" + spec
            piece += "}"
            out.append(piece)
    return "".join(out)


def _placeholder(topic):
    return f"(由于系统异常，这封关于 {topic} 的信件丢失在了虚空中...)"


def generate_email_content_detailed(topic, participants, chat_context_past, chat_context_now, config):
    """带精确错误码的生成函数，返回 (content, error)。

    error 为空字符串表示成功；失败时 content 为占位文本或空串，
    error 形如 LLM_HTTP401 / LLM_TIMEOUT_300s / LLM_CONN / LLM_EMPTY /
    TEMPLATE_FORMAT / PARAM_INVALID / UNEXPECTED，一眼可区分 key 问题与模型问题。
    """
    api_url = normalize_api_base(config.get("apiUrl", "https://api.openai.com/v1") or "https://api.openai.com/v1")
    api_key = config.get("apiKey", "")
    api_model = config.get("apiModel", "gpt-4o")

    # 从 DB 读取提示词模板，若未设置则用内置默认值
    prompt_template = (config.get("promptTemplate", "") or "").strip() or DEFAULT_PROMPT_TEMPLATE

    try:
        souls_content = load_souls(participants)
    except Exception as e:
        return ("", f"SOUL_LOAD:{type(e).__name__}:{e}"[:300])
    try:
        stickers_block = load_sticker_catalog()
    except Exception:
        stickers_block = ""

    # History(now) 总开关：关闭动态同步时，既不更新也不注入最新聊天记录
    auto_sync = config.get("autoSyncContext", "false") == "true"
    now_context = chat_context_now if (auto_sync and chat_context_now) else ""
    now_block = f"【从决定发信到现在的聊天记录】（这段时间发生了什么）：\n{now_context}" if now_context else ""

    # 用占位符渲染最终提示词（安全版：未知占位符保留原文）
    # {chat_context} 保留为 History(now) 的别名，兼容旧的自定义模板
    try:
        system_prompt = safe_format(
            prompt_template,
            souls=souls_content,
            chat_context_past=chat_context_past or "",
            chat_context=now_context,
            chat_context_now_block=now_block,
            topic=topic,
            stickers=stickers_block,
        )
    except Exception as e:
        return ("", f"TEMPLATE_FORMAT:{type(e).__name__}:{e}"[:300])
    # 自定义模板若未使用 {stickers} 占位符，自动把表情目录追加到末尾，保证功能可用
    if "{stickers}" not in prompt_template and stickers_block:
        system_prompt += "\n\n" + stickers_block

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 组装生成参数，从 config 读取，提供合理默认值
    try:
        payload = {
            "model": api_model,
            "messages": [{"role": "system", "content": system_prompt}],
            "temperature": float(config.get("temperature", 0.7)),
            "top_p":       float(config.get("topP",        1.0)),
            "max_tokens":  int(float(str(config.get("maxTokens", 1024)))),
        }
        top_k = int(float(str(config.get("topK", 0))))
    except Exception as e:
        return ("", f"PARAM_INVALID:{type(e).__name__}:{e}"[:300])

    # top_k 仅在非零时加入（OpenAI 不支持，Ollama/KoboldCPP 等本地推理支持）
    if top_k > 0:
        payload["top_k"] = top_k

    timeout = _get_timeout(config, "requestTimeout", 300)
    print(f"[Scheduler] 为 {participants} 调用模型生成关于 '{topic}' 的邮件 "
          f"(temp={payload['temperature']}, top_p={payload['top_p']}, max_tokens={payload['max_tokens']}, timeout={timeout}s)...")
    try:
        res = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        res.raise_for_status()
    except requests.Timeout:
        print(f"[Scheduler] 大模型调用超时 ({timeout}s)")
        return (_placeholder(topic), f"LLM_TIMEOUT_{timeout}s")
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        try:
            body = (getattr(e, "response", None).text or "")[:150]
        except Exception:
            body = ""
        print(f"[Scheduler] 大模型调用失败 HTTP {code}: {body}")
        return (_placeholder(topic), f"LLM_HTTP{code}:{body}"[:300])
    except requests.ConnectionError as e:
        print(f"[Scheduler] 大模型连接失败: {e}")
        return (_placeholder(topic), f"LLM_CONN:{e}"[:300])
    except Exception as e:
        print(f"[Scheduler] 大模型调用失败: {e}")
        return (_placeholder(topic), f"LLM_REQ:{type(e).__name__}:{e}"[:300])
    try:
        content = res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return (_placeholder(topic), f"LLM_PARSE:{type(e).__name__}:{e}"[:300])
    if content is None or not str(content).strip():
        return ("", "LLM_EMPTY:模型返回空内容")
    return (str(content).strip(), "")


def generate_email_content(topic, participants, chat_context_past, chat_context_now, config):
    """独立调用大模型生成信件内容（兼容 wrapper，精确错误请用 detailed 版）"""
    content, _ = generate_email_content_detailed(topic, participants, chat_context_past, chat_context_now, config)
    if not content:
        return _placeholder(topic)
    return content


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

    prompt_template = (config.get("summaryPromptTemplate", "") or "").strip() or DEFAULT_SUMMARY_PROMPT
    system_prompt = safe_format(prompt_template, participants=participants_str, content=content)

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

    # 摘要超时：优先使用独立配置 summaryTimeout，否则复用 requestTimeout，再兜底 60s
    summary_timeout = _get_timeout(config, "summaryTimeout", _get_timeout(config, "requestTimeout", 300) if config.get("summaryUseMainLLM", "true") == "true" else 60)
    # 若 summaryTimeout 未单独配置且复用主 LLM，适当钳位避免摘要也占满 300s
    if "summaryTimeout" not in config and config.get("summaryUseMainLLM", "true") == "true":
        # 摘要通常短，给主超时的 1/3，但不少于 60
        summary_timeout = max(60, min(summary_timeout, 120))
    print(f"[Scheduler] 为 {participants_str} 生成摘要 (model={api_model}, timeout={summary_timeout}s)...")
    try:
        res = requests.post(
            f"{api_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=summary_timeout,
        )
        res.raise_for_status()
        summary = res.json()["choices"][0]["message"]["content"].strip()
        print(f"[Scheduler] 摘要生成完成 ({len(summary)}字): {summary[:60]}...")
        return summary
    except requests.Timeout:
        print(f"[Scheduler] 摘要生成超时 ({summary_timeout}s)")
        return ""
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"[Scheduler] 摘要生成失败 HTTP {code}: {e}")
        return ""
    except Exception as e:
        print(f"[Scheduler] 摘要生成失败: {type(e).__name__}:{e}")
        return ""


def _build_attachments(attachments):
    """将附件路径列表转换为 [(路径, 文件名, MIME 子类型), ...]，跳过不存在的文件"""
    result = []
    for path in attachments or []:
        if not os.path.isfile(path):
            print(f"[Scheduler] 附件不存在，跳过: {path}")
            continue
        fname = os.path.basename(path)
        mime_type, _ = mimetypes.guess_type(fname)
        subtype = mime_type.split("/")[-1] if mime_type else "octet-stream"
        result.append((path, fname, subtype))
    return result


def send_smtp_email(content, topic, participants, config, attachments=None):
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

    # 附加表情图片
    for path, fname, subtype in _build_attachments(attachments):
        try:
            with open(path, 'rb') as f:
                img = MIMEImage(f.read(), _subtype=subtype)
            img.add_header('Content-Disposition', 'attachment', filename=fname)
            msg.attach(img)
            print(f"[Scheduler] 已附加表情: {fname}")
        except Exception as e:
            print(f"[Scheduler] 附加 {fname} 失败: {e}")
    
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


def send_resend_email(content, topic, participants, config, attachments=None):
    """通过 Resend HTTP API 发信 (绕过所有 SMTP 端口限制)"""
    api_key = config.get("resendApiKey", "")
    target  = config.get("targetEmail", "")
    from_addr = config.get("resendFrom", "ArcViGil <onboarding@resend.dev>")

    if not api_key or not target:
        print("[Scheduler] Resend 缺少 API Key 或目标邮箱，无法发送。")
        return False

    sender_name = " & ".join(participants)
    subject = f"来自 {sender_name} 的一封信：{topic}"

    payload = {
        "from": from_addr,
        "to": [target],
        "subject": subject,
        "text": content,
    }
    # 附加表情图片（Resend API: attachments 数组，content 为 base64 字符串）
    built = _build_attachments(attachments)
    if built:
        payload["attachments"] = []
        for path, fname, _subtype in built:
            try:
                with open(path, 'rb') as f:
                    payload["attachments"].append({
                        "filename": fname,
                        "content": base64.b64encode(f.read()).decode('utf-8'),
                    })
                print(f"[Scheduler] 已附加表情: {fname}")
            except Exception as e:
                print(f"[Scheduler] 附加 {fname} 失败: {e}")

    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
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


def send_resend_scheduled_email(content, topic, participants, config, scheduled_at_iso, attachments=None):
    """通过 Resend HTTP API 定时发信（云端 scheduled 模式）

    - 在任务创建时立刻生成内容，提交 Resend 并带 scheduled_at (ISO8601)
    - 返回 (ok: bool, resend_id: str|None)，resend_id 用于后续取消/改期
    - 复用 _build_attachments / base64 附件逻辑
    """
    api_key = config.get("resendApiKey", "")
    target  = config.get("targetEmail", "")
    from_addr = config.get("resendFrom", "ArcViGil <onboarding@resend.dev>")

    if not api_key or not target:
        print("[Scheduler] Resend 定时发送缺少 API Key 或目标邮箱，无法发送。")
        return False, None

    if not scheduled_at_iso:
        print("[Scheduler] Resend 定时发送缺少 scheduled_at。")
        return False, None

    sender_name = " & ".join(participants)
    subject = f"来自 {sender_name} 的一封信：{topic}"

    payload = {
        "from": from_addr,
        "to": [target],
        "subject": subject,
        "text": content,
        "scheduled_at": scheduled_at_iso,
    }
    built = _build_attachments(attachments)
    if built:
        payload["attachments"] = []
        for path, fname, _subtype in built:
            try:
                with open(path, 'rb') as f:
                    payload["attachments"].append({
                        "filename": fname,
                        "content": base64.b64encode(f.read()).decode('utf-8'),
                    })
                print(f"[Scheduler] 已附加表情: {fname}")
            except Exception as e:
                print(f"[Scheduler] 附加 {fname} 失败: {e}")

    try:
        res = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if res.status_code in (200, 201):
            try:
                rid = res.json().get("id")
            except Exception:
                rid = None
            print(f"[Scheduler] Resend 定时发送成功至 {target} (scheduled_at={scheduled_at_iso}, id={rid})")
            return True, rid
        else:
            print(f"[Scheduler] Resend 定时发送失败: {res.status_code} {res.text}")
            return False, None
    except Exception as e:
        print(f"[Scheduler] Resend 定时请求异常: {e}")
        return False, None


def cancel_resend_scheduled_email(resend_id, config):
    """取消已调度的 Resend 邮件（POST /emails/{id}/cancel）"""
    api_key = config.get("resendApiKey", "")
    if not api_key or not resend_id:
        return False
    try:
        res = requests.post(
            f"https://api.resend.com/emails/{resend_id}/cancel",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if res.status_code in (200, 201):
            print(f"[Scheduler] Resend 取消成功 id={resend_id}")
            return True
        else:
            print(f"[Scheduler] Resend 取消失败 id={resend_id}: {res.status_code} {res.text}")
            return False
    except Exception as e:
        print(f"[Scheduler] Resend 取消异常 id={resend_id}: {e}")
        return False


def send_email(content, topic, participants, config, attachments=None):
    """统一发信入口：根据 emailMethod 配置选择 SMTP 或 Resend"""
    method = config.get("emailMethod", "smtp")
    if method == "resend":
        return send_resend_email(content, topic, participants, config, attachments)
    else:
        return send_smtp_email(content, topic, participants, config, attachments)

def _handle_task_failure(conn, task_id, error_msg, config):
    """通用失败处理：未达最大重试 -> 1min后重试；达上限 -> 标记 failed"""
    _ensure_task_retry_columns(conn)
    c = conn.cursor()
    # 读取当前重试次数
    try:
        c.execute("SELECT retry_count FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        retry_count = int(row[0]) if row and row[0] is not None else 0
    except Exception:
        retry_count = 0
    max_retries = _get_int(config, "maxRetries", 3, 0, 20)
    retry_delay = _get_int(config, "retryDelaySeconds", 60, 10, 3600)

    if retry_count < max_retries:
        new_count = retry_count + 1
        next_trigger = time.time() + retry_delay
        try:
            c.execute("UPDATE tasks SET retry_count=?, last_error=?, trigger_at=?, next_retry_at=?, status='pending' WHERE id=?",
                      (new_count, str(error_msg)[:500], next_trigger, next_trigger, task_id))
        except Exception as e:
            # 兼容旧表（列缺失时仅更新 trigger_at）
            print(f"[Scheduler] 重试列更新失败，降级仅更新 trigger_at: {e}")
            c.execute("UPDATE tasks SET trigger_at=? WHERE id=?", (next_trigger, task_id))
        conn.commit()
        print(f"[Scheduler] 任务 {task_id} 失败 [{error_msg}]，{retry_delay}s 后重试 ({new_count}/{max_retries}) -> {time.ctime(next_trigger)}")
        return False  # 未彻底失败，等待重试
    else:
        try:
            c.execute("UPDATE tasks SET status='failed', last_error=? WHERE id=?", (str(error_msg)[:500], task_id))
        except Exception:
            c.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
        conn.commit()
        print(f"[Scheduler] 任务 {task_id} 重试 {max_retries} 次仍失败，标记为 failed: {error_msg}。请检查 API/网络或在任务雷达手动重试。")
        return True  # 已标记 failed

def process_due_tasks():
    # 快照到期任务：短连接只读，读完即关，不持有写锁
    try:
        snap = connect_db()
        try:
            _ensure_task_retry_columns(snap)
            snap.commit()
        except Exception:
            pass
        c0 = snap.cursor()
        now = time.time()
        # 查找所有到期且未执行的任务（pending 包含初次及重试）
        c0.execute("SELECT id, participants, topic, chat_context, chat_context_past FROM tasks WHERE status='pending' AND trigger_at <= ?", (now,))
        due_tasks = c0.fetchall()
    except Exception as e:
        print(f"[Scheduler] 快照到期任务失败: {type(e).__name__}:{e}")
        return
    finally:
        try:
            snap.close()
        except Exception:
            pass

    if not due_tasks:
        return

    config = get_config()

    for row in due_tasks:
        task_id = row[0]
        conn = connect_db()
        try:
            _ensure_task_retry_columns(conn)
            try:
                participants = json.loads(row[1])
            except Exception as e:
                _handle_task_failure(conn, task_id, f"TASK_PARSE:participants非JSON:{e}"[:300], config)
                continue
            topic = row[2] or ""
            chat_context = row[3] or ""
            chat_context_past = row[4] or ""

            print(f"[Scheduler] 开始执行任务 {task_id}: {participants} -> {topic}")

            # 1. 生成内容（past 冻结 + now 动态，now 受 autoSyncContext 总开关控制）
            content, llm_err = generate_email_content_detailed(topic, participants, chat_context_past, chat_context, config)

            # 1.5 检测生成失败（占位/空内容） -> 重试（last_error 记录精确错误码）
            is_fail, reason = _is_failure_content(content, topic)
            if is_fail:
                err = (llm_err or f"LLM生成失败:{reason}")[:500]
                print(f"[Scheduler] 任务 {task_id} 内容检测失败 {reason} err={err}: {(content or '')[:80]}")
                _handle_task_failure(conn, task_id, err, config)
                continue

            # 1.6 解析表情标记 → 剥离正文 + 收集附件（防误伤：异常也不应堵住任务）
            try:
                content, attachments = resolve_stickers(content)
            except Exception as e:
                print(f"[Scheduler] 任务 {task_id} 表情解析异常，继续无附件发送: {e}")
                attachments = []
            attachment_names = [os.path.basename(p) for p in (attachments or [])]

            # 2. 发送邮件 (自动根据 emailMethod 配置选择 SMTP 或 Resend) -> 失败则重试
            try:
                ok = send_email(content, topic, participants, config, attachments)
            except Exception as e:
                print(f"[Scheduler] 任务 {task_id} 发信抛异常: {type(e).__name__}:{e}")
                ok = False
            if not ok:
                err = f"邮件发送失败({config.get('emailMethod', 'resend')})"
                _handle_task_failure(conn, task_id, err, config)
                continue

            # 3. 生成摘要（70-100字），失败静默留空（不影响主流程）
            try:
                sender_name = " & ".join(participants)
                summary = generate_summary(content, sender_name, config) if content else ""
            except Exception as e:
                print(f"[Scheduler] 任务 {task_id} 摘要异常，留空继续: {e}")
                sender_name = " & ".join(participants) if isinstance(participants, list) else str(participants)
                summary = ""

            # 4. 保存到消息库以供 ST 记忆注入（含主题+参与者+内容+摘要+附件列表）
            subject = f"来自 {sender_name} 的一封信：{topic}"
            conn.cursor().execute(
                "INSERT INTO messages (subject, content, participants, created_at, synced_to_st, summary, attachments) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (subject, content, sender_name, time.time(), summary, json.dumps(attachment_names))
            )

            # 5. 标记任务完成，并清零重试信息
            try:
                conn.cursor().execute("UPDATE tasks SET status='completed', retry_count=0, last_error='' WHERE id=?", (task_id,))
            except Exception:
                conn.cursor().execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
            conn.commit()
            print(f"[Scheduler] 任务 {task_id} 完成，已入库 messages")
        except Exception as e:
            # 单任务兜底：任何未预料异常只记这一个任务，绝不 abort 整批
            try:
                _handle_task_failure(conn, task_id, f"UNEXPECTED:{type(e).__name__}:{e}"[:500], config)
            except Exception as e2:
                print(f"[Scheduler] 任务 {task_id} 失败处理也异常: {e2}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

def run_scheduler_loop():
    print("[Scheduler] 调度器线程已启动...")
    while True:
        try:
            process_due_tasks()
        except Exception as e:
            print(f"[Scheduler] 调度循环异常: {e}")
        time.sleep(10) # 每10秒检查一次
