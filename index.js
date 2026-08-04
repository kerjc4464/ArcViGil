// ArcViGil - ST 前端扩展入口
// 路径层级：public/scripts/extensions/third-party/ArcViGil/index.js
import { extension_settings, getContext } from "../../../extensions.js";
import { eventSource, event_types, setExtensionPrompt, extension_prompt_roles, saveSettingsDebounced } from "../../../../script.js";

const extensionName = "ArcViGil";
// ST 静态文件服务的根目录是 public/, 所以路径从 public/ 开始
const extensionFolderPath = "scripts/extensions/third-party/ArcViGil";

// ========== 内置默认提示词模板 ==========
// 与 scheduler.py 的 DEFAULT_PROMPT_TEMPLATE 保持同步
const DEFAULT_PROMPT_TEMPLATE = `你现在是以下角色：
{souls}

【决定发信时的聊天记录】（你当时发信的动机，此后不再变化）：
{chat_context_past}

你定下的发信主题是：【{topic}】。

{chat_context_now_block}

现在时间已到，请结合以上所有信息——角色设定、发信动机、主题，以及发信前的最新对话——用最符合你身份的语气，直接写出邮件的正文。只输出邮件正文，不要有多余的格式和废话。`;

const defaultSettings = {
    backendUrl:         "http://127.0.0.1:9000",
    apiKey:             "",
    apiUrl:             "https://api.openai.com/v1",
    apiModel:           "gpt-4o",
    // LLM 生成参数
    temperature:        0.7,
    topP:               1.0,
    topK:               0,
    maxTokens:          1024,
    // 发信模式
    emailMethod:        "resend",
    // Resend
    resendApiKey:       "",
    resendFrom:         "ArcViGil <onboarding@resend.dev>",
    // SMTP
    smtpServer:         "",
    smtpPort:           465,
    smtpUser:           "",
    smtpPass:           "",
    targetEmail:        "",
    // 提示词模板 (空字符串 = 使用后端内置默认)
    promptTemplate:     "",
    // 系统提示词
    systemPrompt:       "",
    // 行为
    contextDepthPast:       20,     // History(past) 滑动窗口：决定发信时携带的记录条数（冻结不变）
    contextDepthNow:        20,     // History(now) 滑动窗口：动态同步携带的记录条数
    enable:             true,
    // 离线邮件注入设置
    injectEnabled:      true,     // 是否启用历史信件注入到上下文
    maxInjectMessages:  5,        // 最多注入几封信件
    injectMode:         "full",   // full=完整正文 summary=摘要 title=仅标题
    injectAckedAt:      0,        // 清空的时间戳（只展示此时间之后的信件）
    injectPosition:     0,        // 0=对话顶部 2=对话底部
    injectDepth:        0,        // 注入深度，0=顶部
    // 摘要生成设置
    summaryEnabled:     true,     // 是否在生成邮件时自动产出摘要
    summaryUseMainLLM:  true,     // true=复用主LLM false=自定义
    summaryApiUrl:      "",       // 自定义摘要API地址
    summaryApiKey:      "",       // 自定义摘要API Key
    summaryApiModel:    "gpt-4o-mini",
    summaryTemperature: 0.5,
    summaryMaxTokens:   200,
    summaryPromptTemplate: "",    // 空=使用后端内置默认
    // 动态同步上下文设置
    autoSyncContext:    false,    // 动态同步最新上下文到待执行任务
};

let heartbeatInterval = null;
let autoSaveTimeout = null;

function autoSaveSettings() {
    clearTimeout(autoSaveTimeout);
    autoSaveTimeout = setTimeout(async () => {
        saveSettings();
        await pushConfigToBackend();
    }, 1500);
}

// ========== 角色调度系统提示词 ==========
const DEFAULT_SYSTEM_PROMPT = `
[System Note: ArcViGil Offline Messaging System]
You have the ability to send emails to the user while they are away.
If you decide in the roleplay that you (or your group) want to send a message, output the following JSON block at the very END of your response:
<ArcViGil>
{"ArcViGil_Tasks": [{"participants": ["YourName"], "delay_hours": 2, "topic": "Your subject here"}]}
</ArcViGil>
You can schedule multiple tasks at once. Do NOT wrap this block in markdown code fences.
`;

// ========== 设置管理 ==========
function loadSettings() {
    extension_settings[extensionName] = extension_settings[extensionName] || {};
    // 迁移旧的单一 contextDepth 到新的 past/now 双滑动窗口
    if (extension_settings[extensionName].contextDepth !== undefined) {
        if (extension_settings[extensionName].contextDepthPast === undefined) {
            extension_settings[extensionName].contextDepthPast = extension_settings[extensionName].contextDepth;
        }
        if (extension_settings[extensionName].contextDepthNow === undefined) {
            extension_settings[extensionName].contextDepthNow = extension_settings[extensionName].contextDepth;
        }
        delete extension_settings[extensionName].contextDepth;
    }
    for (const key in defaultSettings) {
        if (extension_settings[extensionName][key] === undefined) {
            extension_settings[extensionName][key] = defaultSettings[key];
        }
    }
}

function saveSettings() {
    const s = extension_settings[extensionName];
    s.backendUrl     = $("#arcvigil-backend-url").val();
    s.apiKey         = $("#arcvigil-api-key").val();
    s.apiUrl         = $("#arcvigil-api-url").val();
    s.apiModel       = $("#arcvigil-api-model").val();
    // LLM 生成参数
    s.temperature    = Number($("#arcvigil-temperature").val());
    s.topP           = Number($("#arcvigil-top-p").val());
    s.topK           = Number($("#arcvigil-top-k").val());
    s.maxTokens      = Number($("#arcvigil-max-tokens").val());
    // 发信模式
    s.emailMethod    = $(".arcvigil-mode-tab.active").data("mode") || "resend";
    
    // Resend
    s.resendApiKey   = $("#arcvigil-resend-api-key").val();
    s.resendFrom     = $("#arcvigil-resend-from").val();
    
    // SMTP
    s.smtpServer     = $("#arcvigil-smtp-server").val();
    s.smtpPort       = Number($("#arcvigil-smtp-port").val());
    s.smtpUser       = $("#arcvigil-smtp-user").val();
    s.smtpPass       = $("#arcvigil-smtp-pass").val();
    
    // 共用
    s.targetEmail    = s.emailMethod === "resend" ? $("#arcvigil-target-email-resend").val() : $("#arcvigil-target-email").val();
    // 提示词
    s.promptTemplate = $("#arcvigil-prompt-template").val();
    s.systemPrompt   = $("#arcvigil-system-prompt").val();
    // 行为
    s.contextDepthPast  = Number($("#arcvigil-context-depth-past").val());
    s.contextDepthNow   = Number($("#arcvigil-context-depth-now").val());
    s.enable         = $("#arcvigil-enable").prop("checked");
    // 离线信件注入设置
    s.maxInjectMessages = Math.max(1, Number($("#arcvigil-max-inject").val()) || 5);
    s.injectPosition    = Number($("#arcvigil-inject-position").val());
    s.injectDepth       = Math.max(0, Number($("#arcvigil-inject-depth").val()) || 0);
    s.injectMode         = $("#arcvigil-inject-mode").val();
    // 摘要生成设置
    s.summaryEnabled     = $("#arcvigil-summary-enabled").prop("checked");
    s.summaryUseMainLLM  = $("#arcvigil-summary-use-main-llm").prop("checked");
    s.summaryApiUrl      = $("#arcvigil-summary-api-url").val();
    s.summaryApiKey      = $("#arcvigil-summary-api-key").val();
    s.summaryApiModel    = $("#arcvigil-summary-api-model").val();
    s.summaryTemperature = Number($("#arcvigil-summary-temperature").val());
    s.summaryMaxTokens   = Number($("#arcvigil-summary-max-tokens").val());
    s.summaryPromptTemplate = $("#arcvigil-summary-prompt-template").val();
    // 动态同步上下文
    s.injectEnabled     = $("#arcvigil-inject-enabled").prop("checked");
    s.autoSyncContext   = $("#arcvigil-auto-sync-context").prop("checked");
    // 持久化到磁盘，页面刷新后设置不丢失
    saveSettingsDebounced();
}

function renderSettings() {
    const s = extension_settings[extensionName];
    $("#arcvigil-backend-url").val(s.backendUrl);
    $("#arcvigil-api-key").val(s.apiKey);
    $("#arcvigil-api-url").val(s.apiUrl);
    $("#arcvigil-api-model").val(s.apiModel);
    // LLM 生成参数（同步滑块和数字框）
    setSliderPair("arcvigil-temperature", s.temperature);
    setSliderPair("arcvigil-top-p",       s.topP);
    setSliderPair("arcvigil-top-k",       s.topK);
    setSliderPair("arcvigil-max-tokens",  s.maxTokens);
    // 模式
    const mode = s.emailMethod || "resend";
    $(".arcvigil-mode-tab").removeClass("active");
    $(`#arcvigil-mode-tab-${mode}`).addClass("active");
    $("#arcvigil-panel-resend, #arcvigil-panel-smtp").hide();
    $(`#arcvigil-panel-${mode}`).show();

    // Resend
    $("#arcvigil-resend-api-key").val(s.resendApiKey || "");
    $("#arcvigil-resend-from").val(s.resendFrom || "ArcViGil <onboarding@resend.dev>");
    $("#arcvigil-target-email-resend").val(s.targetEmail || "");

    // SMTP
    $("#arcvigil-smtp-server").val(s.smtpServer);
    $("#arcvigil-smtp-port").val(s.smtpPort);
    $("#arcvigil-smtp-user").val(s.smtpUser);
    $("#arcvigil-smtp-pass").val(s.smtpPass);
    $("#arcvigil-target-email").val(s.targetEmail);
    // 提示词
    $("#arcvigil-system-prompt").val(s.systemPrompt || DEFAULT_SYSTEM_PROMPT);
    $("#arcvigil-prompt-template").val(s.promptTemplate || "");
    // 行为
    $("#arcvigil-context-depth-past").val(s.contextDepthPast);
    $("#arcvigil-context-depth-now").val(s.contextDepthNow);
    $("#arcvigil-enable").prop("checked", s.enable);
    // 离线信件注入设置
    $("#arcvigil-inject-enabled").prop("checked", s.injectEnabled !== false);
    $("#arcvigil-max-inject").val(s.maxInjectMessages ?? 5);
    $("#arcvigil-inject-position").val(s.injectPosition ?? 0);
    $("#arcvigil-inject-depth").val(s.injectDepth ?? 0);
    $("#arcvigil-inject-mode").val(s.injectMode || "full");
    // 摘要生成设置
    $("#arcvigil-summary-enabled").prop("checked", s.summaryEnabled !== false);
    $("#arcvigil-summary-use-main-llm").prop("checked", s.summaryUseMainLLM !== false);
    $("#arcvigil-summary-custom-llm").prop("checked", s.summaryUseMainLLM === false);
    $("#arcvigil-summary-api-url").val(s.summaryApiUrl || "");
    $("#arcvigil-summary-api-key").val(s.summaryApiKey || "");
    $("#arcvigil-summary-api-model").val(s.summaryApiModel || "gpt-4o-mini");
    $("#arcvigil-summary-temperature").val(s.summaryTemperature ?? 0.5);
    $("#arcvigil-summary-max-tokens").val(s.summaryMaxTokens ?? 200);
    $("#arcvigil-summary-prompt-template").val(s.summaryPromptTemplate || "");
    // 摘要自定义面板默认折叠 — 由 init 中的 toggleSummaryCustomPanel 统一处理
    // 动态同步上下文
    $("#arcvigil-auto-sync-context").prop("checked", s.autoSyncContext ?? false);
}

// 滑块 & 数字框联动助手 (id 不含 -slider 后缀)
function setSliderPair(id, value) {
    $(`#${id}`).val(value);
    $(`#${id}-slider`).val(value);
}

function bindSliderPair(id) {
    const $num    = $(`#${id}`);
    const $slider = $(`#${id}-slider`);
    $slider.on("input", () => $num.val($slider.val()));
    $num.on("input",    () => $slider.val($num.val()));
}

// ========== 状态指示器 ==========
function setStatus(online) {
    const $dot = $("#arcvigil-status-indicator");
    const $text = $("#arcvigil-status-text");
    if (online) {
        $dot.removeClass("offline").addClass("online").attr("title", "后端已连接");
        $text.text("Online").css("color", "#44ff88");
    } else {
        $dot.removeClass("online").addClass("offline").attr("title", "后端未连接");
        $text.text("Offline").css("color", "#ff4444");
    }
}

// ========== 后端通信 ==========
async function pushConfigToBackend() {
    const url = extension_settings[extensionName].backendUrl;
    if (!url) return;
    try {
        const res = await fetch(`${url}/api/config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(extension_settings[extensionName]),
        });
        if (res.ok) toastr.success("ArcViGil: 配置已同步至后端");
        else toastr.warning("ArcViGil: 后端接收配置失败");
    } catch {
        toastr.error("ArcViGil: 无法连接到后端，请检查是否已启动");
    }
}

async function sendHeartbeat() {
    if (!extension_settings[extensionName].enable) return;
    try {
        const res = await fetch(`${extension_settings[extensionName].backendUrl}/api/heartbeat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ status: "online", timestamp: Date.now() }),
        });
        setStatus(res.ok);
        // 故意不打日志，避免每15秒刷屏控制台
    } catch {
        setStatus(false);
        // 连接失败也静默处理，状态指示灯已经变红，无需重复报错
    }
}

async function pullOfflineMessages() {
    // 刷新注入即可（数据库是实砖，刷页也不会丢）
    const url = extension_settings[extensionName].backendUrl;
    if (!url) return;
    try {
        const limit = extension_settings[extensionName].maxInjectMessages || 5;
        const since = extension_settings[extensionName].injectAckedAt || 0;
        const res = await fetch(`${url}/api/messages?limit=${limit}&since=${since}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.messages && data.messages.length > 0) {
            toastr.success(`ArcViGil: 有 ${data.messages.length} 封离线信件已在上下文中就绪`);
            refreshInjection();
            updateTaskRadar();
        } else {
            toastr.info("ArcViGil: 暂无未读离线邮件");
        }
    } catch (e) {
        console.error("[ArcViGil] pullOfflineMessages failed:", e);
    }
}

// ========== Soul 注册中心 ==========
async function refreshSoulList() {
    const url = extension_settings[extensionName].backendUrl;
    if (!url) {
        toastr.warning("ArcViGil: 请先填写并保存后端地址");
        return;
    }
    const $list = $("#arcvigil-soul-list");
    $list.html('<div class="arcvigil-empty-msg"><i class="fa-solid fa-spinner fa-spin"></i> 加载中...</div>');
    try {
        const res = await fetch(`${url}/api/souls`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        $list.empty();
        if (!data.souls || data.souls.length === 0) {
            $list.append('<div class="arcvigil-empty-msg">souls/ 目录为空，暂无注册角色</div>');
        } else {
            data.souls.forEach((s) => {
                const kb = (s.size_bytes / 1024).toFixed(1);
                $list.append(`
                    <div class="arcvigil-soul-item">
                        <i class="fa-solid fa-user-astronaut" style="color:#34d399;"></i>
                        <span class="soul-name">${s.name}</span>
                        <span class="soul-format-badge">${s.format}</span>
                        <span class="soul-size">${kb} KB</span>
                    </div>`);
            });
        }
    } catch (e) {
        $list.html('<div class="arcvigil-empty-msg" style="color:#ff6b6b;">读取失败，请确认后端已启动</div>');
        console.error("[ArcViGil] refreshSoulList failed:", e);
    }
}

// ========== 记忆注入 ==========
async function buildPrompt() {
    const s = extension_settings[extensionName];
    if (!s.enable) return "";

    let prompt = s.systemPrompt || DEFAULT_SYSTEM_PROMPT;

    // 历史信件注入：由 injectEnabled 独立控制，关闭时只保留调度 prompt
    if (s.injectEnabled !== false && s.backendUrl) {
        try {
            const limit = s.maxInjectMessages || 5;
            const since = s.injectAckedAt || 0;
            const res = await fetch(`${s.backendUrl}/api/messages?limit=${limit}&since=${since}`);
            if (res.ok) {
                const data = await res.json();
                const msgs = data.messages || [];
                if (msgs.length > 0) {
                    const mode = s.injectMode || "full";
                    prompt += "\n\n[System Note: 以下是系统在离线期间代为发出的信件内容（已发送至你邮箱）：]";
                    msgs.forEach((msg, i) => {
                        const dt = new Date(msg.created_at * 1000).toLocaleString("zh-CN", {
                            year: "numeric", month: "2-digit", day: "2-digit",
                            hour: "2-digit", minute: "2-digit"
                        });
                        if (mode === "title") {
                            prompt += `\nchunk${i + 1}：【${dt}】${msg.subject}`;
                        } else if (mode === "summary") {
                            const body = msg.summary || msg.subject;
                            prompt += `\nchunk${i + 1}：【${dt}】${msg.subject}\n${body}`;
                        } else {
                            prompt += `\nchunk${i + 1}：【${dt}】${msg.subject}\n${msg.content}`;
                        }
                    });
                    prompt += "\n[End System Note]";
                }
            }
        } catch (e) {
            console.warn("[ArcViGil] 拉取历史信件失败:", e);
        }
    }

    return prompt;
}

async function refreshInjection() {
    const s = extension_settings[extensionName];
    const pos = s.injectPosition ?? 0;
    const depth = s.injectDepth ?? 0;
    const prompt = await buildPrompt();
    setExtensionPrompt(extensionName, prompt, pos, depth, false, extension_prompt_roles.SYSTEM);
}

// ========== 信件存档列表 ==========
async function refreshLetterList() {
    const url = extension_settings[extensionName].backendUrl;
    const $list = $("#arcvigil-letter-list");
    const $btn  = $("#arcvigil-letters-refresh-btn");

    if (!url) {
        $list.html('<div class="arcvigil-empty-msg">请先填写后端地址</div>');
        return;
    }

    $btn.addClass("spinning");

    try {
        // 拉全量历史（最多 100 封，since=0 取所有）
        const res = await fetch(`${url}/api/messages?limit=100&since=0`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const msgs = data.messages || [];

        $list.empty();

        if (msgs.length === 0) {
            $list.html('<div class="arcvigil-empty-msg">暂无历史信件记录</div>');
            return;
        }

        const ackedAt = extension_settings[extensionName].injectAckedAt || 0;
        const maxInject = extension_settings[extensionName].maxInjectMessages || 5;
        const injectEnabled = extension_settings[extensionName].injectEnabled !== false;
        // 最近 maxInject 封是当前正在注入的
        const injectedIds = injectEnabled ? new Set(msgs.slice(-maxInject).map(m => m.id)) : new Set();

        // 倒序排列（最新的在上）
        [...msgs].reverse().forEach((msg, i) => {
            const isInjected = injectedIds.has(msg.id) && msg.created_at > ackedAt;
            const dt = new Date(msg.created_at * 1000).toLocaleString("zh-CN", {
                year: "numeric", month: "2-digit", day: "2-digit",
                hour: "2-digit", minute: "2-digit"
            });
            const subject = msg.subject || `信件 #${msg.id}`;
            const summary = msg.summary || "";
            const badge = injectEnabled ? (isInjected
                ? `<span class="arcvigil-letter-injected-badge">已注入</span>`
                : `<span class="arcvigil-letter-injected-badge not-injected">未注入</span>`) : "";
            const summaryHtml = summary
                ? `<div class="arcvigil-letter-summary">摘要: ${summary}</div>`
                : "";

            const $item = $(`
                <div class="arcvigil-letter-item" data-id="${msg.id}">
                    <div class="arcvigil-letter-header">
                        <i class="fa-solid fa-chevron-right arcvigil-letter-chevron"></i>
                        <div class="arcvigil-letter-dot"></div>
                        <div class="arcvigil-letter-meta">
                            <div class="arcvigil-letter-subject" title="${subject}">${subject}</div>
                            <div class="arcvigil-letter-time">${dt}</div>
                        </div>
                        ${badge}
                    </div>
                    <div class="arcvigil-letter-body">
                        ${summaryHtml}
                        <div class="arcvigil-letter-content-box">${msg.content}</div>
                    </div>
                </div>
            `);

            // 点击标题行展开/收起
            $item.find(".arcvigil-letter-header").on("click", () => {
                $item.toggleClass("open");
            });

            $list.append($item);
        });

    } catch (e) {
        $list.html('<div class="arcvigil-empty-msg" style="color:#ff6b6b;">读取失败，请确认后端已启动</div>');
        console.error("[ArcViGil] refreshLetterList failed:", e);
    } finally {
        $btn.removeClass("spinning");
    }
}

// ========== 动态同步聊天上下文到待执行任务 (History-now) ==========
async function syncContextToPendingTasks() {
    const s = extension_settings[extensionName];
    if (!s.autoSyncContext || !s.backendUrl) return;

    const context = getContext();
    const depth = s.contextDepthNow || 20; // History(now) 滑动窗口条数
    const recentChat = (context.chat || [])
        .slice(-depth)
        .map((m) => `${m.name}: ${m.mes}`)
        .join("\n");

    try {
        await fetch(`${s.backendUrl}/api/tasks/context`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_context: recentChat }),
        });
    } catch (e) {
        console.warn("[ArcViGil] syncContextToPendingTasks failed:", e);
    }
}

// ========== 任务提交 ==========
async function submitTasks(tasksJsonStr) {
    try {
        // 清理由于 ST 的 Markdown/HTML 渲染混入的杂质
        let cleanJson = tasksJsonStr
            .replace(/<br\s*\/?>/gi, "") // 移除换行标签
            .replace(/<[^>]*>/g, "") // 移除所有其他 HTML 标签
            .replace(/[\u200B-\u200D\uFEFF]/g, ''); // 移除零宽字符
            
        const obj = JSON.parse(cleanJson);
        if (!obj.ArcViGil_Tasks) return;

        const context = getContext();
        const depth = extension_settings[extensionName].contextDepthPast || 20;
        const recentChat = (context.chat || [])
            .slice(-depth)
            .map((m) => `${m.name}: ${m.mes}`)
            .join("\n");

        const res = await fetch(`${extension_settings[extensionName].backendUrl}/api/schedule`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tasks: obj.ArcViGil_Tasks, chat_context: recentChat }),
        });
        if (res.ok) {
            toastr.success("📨 ArcViGil: 发信计划已部署！角色将在预定时间自动发出信件。");
            updateTaskRadar();
        } else {
            toastr.error("ArcViGil: 任务部署失败");
        }
    } catch (e) {
        console.error("[ArcViGil] submitTasks error:", e, tasksJsonStr);
        toastr.warning("ArcViGil: 提取到任务内容但解析失败，格式有误");
    }
}

// ========== 消息拦截 ==========
function interceptMessage(text) {
    if (!extension_settings[extensionName].enable) return text;
    // 兼容带或不带 <ArcViGil> 标签的 JSON，且考虑到可能被包裹在 Markdown 代码块里
    // 使用非贪婪匹配到最后一个右大括号（因为可能有多层嵌套结构）
    const regex = /(?:```[a-z]*\n)?(?:<ArcViGil>\s*)?(\{\s*(?:"|&quot;|)ArcViGil_Tasks(?:"|&quot;|)[\s\S]*\})(?:\s*<\/ArcViGil>)?(?:\n```)?/i;
    const match = text.match(regex);
    if (match) {
        let jsonStr = match[1].trim();
        // 如果 HTML 实体被转义，反转义一下，防止 JSON.parse 报错
        jsonStr = jsonStr.replace(/&quot;/g, '"');
        submitTasks(jsonStr);
        return text.replace(match[0], "").trim();
    }
    return text;
}

// ========== 任务雷达 UI ==========
async function updateTaskRadar() {
    const url = extension_settings[extensionName].backendUrl;
    if (!url) return;
    try {
        const res = await fetch(`${url}/api/tasks`);
        if (!res.ok) return;
        const data = await res.json();
        const $list = $("#arcvigil-task-list");
        $list.empty();
        if (!data.tasks || data.tasks.length === 0) {
            $list.append('<div class="arcvigil-empty-msg">暂无计划任务</div>');
        } else {
            data.tasks.forEach((t) => {
                const dt = new Date(t.trigger_at * 1000).toLocaleString();
                const names = JSON.parse(t.participants).join(", ");
                const isPaused = t.status === 'paused';
                const opacityStyle = isPaused ? "opacity: 0.55; border-left-color: #6b7280;" : "";
                const badgeHtml = isPaused ? `<span style="font-size:10px; color:#f59e0b; background:rgba(245,158,11,0.15); padding:1px 5px; border-radius:3px; margin-left:6px; font-weight:normal;">已禁用/归档</span>` : "";
                const toggleIcon = isPaused
                    ? `<i class="fa-solid fa-play arcvigil-task-toggle-btn" data-id="${t.id}" title="点击恢复启用此任务" style="color:#34d399; cursor:pointer; padding: 5px;"></i>`
                    : `<i class="fa-solid fa-pause arcvigil-task-toggle-btn" data-id="${t.id}" title="点击禁用/暂停此任务（暂停后不会自动触发发信）" style="color:#f59e0b; cursor:pointer; padding: 5px;"></i>`;

                $list.append(`
                    <div class="arcvigil-task-item" style="display:flex; justify-content:space-between; align-items:center; ${opacityStyle}">
                        <div style="flex:1; min-width:0;">
                            <div class="task-title">${names} → ${t.topic}${badgeHtml}</div>
                            <div class="task-meta">触发时间: ${dt}</div>
                        </div>
                        <div style="display:flex; gap:4px; align-items:center; flex-shrink:0;">
                            ${toggleIcon}
                            <i class="fa-solid fa-flask arcvigil-task-test-btn" data-id="${t.id}" title="[测试] 立即生成并发送测试邮件（不入库，不影响计划）" style="color:#a78bfa; cursor:pointer; padding: 5px;"></i>
                            <i class="fa-solid fa-trash arcvigil-task-delete-btn" data-id="${t.id}" title="删除此任务" style="color:#ef4444; cursor:pointer; padding: 5px;"></i>
                        </div>
                    </div>`);
            });

            // 绑定禁用/恢复状态切换按钮
            $(".arcvigil-task-toggle-btn").on("click", async function() {
                const taskId = $(this).data("id");
                try {
                    const toggleRes = await fetch(`${url}/api/tasks/${taskId}/toggle`, { method: 'POST' });
                    const toggleData = await toggleRes.json();
                    if (toggleRes.ok && toggleData.status === "ok") {
                        const isNowPaused = toggleData.new_status === 'paused';
                        toastr.info(`ArcViGil: 任务已${isNowPaused ? '禁用/暂停' : '恢复启用'}`);
                        updateTaskRadar();
                    } else {
                        toastr.error("ArcViGil: 切换任务状态失败");
                    }
                } catch (e) {
                    console.error("[ArcViGil] toggle task error:", e);
                    toastr.error("ArcViGil: 请求失败，请确认后端已启动");
                }
            });

            // 绑定测试按钮事件
            $(".arcvigil-task-test-btn").on("click", async function() {
                const taskId = $(this).data("id");
                const $icon = $(this);
                $icon.removeClass("fa-flask").addClass("fa-spinner fa-spin");
                toastr.info("ArcViGil: 🧪 正在生成测试信件，请稍候……");
                try {
                    const testRes = await fetch(`${url}/api/tasks/${taskId}/test`, { method: 'POST' });
                    const testData = await testRes.json();
                    if (testRes.ok && testData.status === "ok") {
                        toastr.success("ArcViGil: ✅ 测试邮件已发出！请检查收件箱（主题开头有 [测试] 标记）");
                    } else {
                        toastr.error(`ArcViGil: ❌ 测试发送失败: ${testData.message || '未知错误'}`);
                    }
                } catch (e) {
                    console.error("[ArcViGil] test task error:", e);
                    toastr.error("ArcViGil: ❌ 请求失败，请确认后端已启动");
                } finally {
                    $icon.removeClass("fa-spinner fa-spin").addClass("fa-flask");
                }
            });

            // 绑定删除按钮事件
            $(".arcvigil-task-delete-btn").on("click", async function() {
                const taskId = $(this).data("id");
                if (confirm("确定要删除这个待执行任务吗？")) {
                    try {
                        const delRes = await fetch(`${url}/api/tasks/${taskId}`, { method: 'DELETE' });
                        if (delRes.ok) {
                            toastr.success("ArcViGil: 任务已删除");
                            updateTaskRadar();
                        } else {
                            toastr.error("ArcViGil: 删除任务失败");
                        }
                    } catch (e) {
                        console.error("[ArcViGil] delete task error:", e);
                    }
                }
            });
        }
    } catch (e) {
        console.error("[ArcViGil] updateTaskRadar failed:", e);
    }
}

// ========== 初始化入口 ==========
(async function init() {
    try {
        console.log("[ArcViGil] 开始初始化...");
        loadSettings();

        // 拉取 HTML 模板并插入设置面板
        const html = await $.get(`/${extensionFolderPath}/index.html`);
        $("#extensions_settings").append(html);

        // 渲染已保存的设置到表单
        renderSettings();

        // ---- 模式切换 ----
        $(".arcvigil-mode-tab").on("click", function() {
            $(".arcvigil-mode-tab").removeClass("active");
            $(this).addClass("active");
            const mode = $(this).data("mode");
            $("#arcvigil-panel-resend, #arcvigil-panel-smtp").hide();
            $(`#arcvigil-panel-${mode}`).show();
            autoSaveSettings();
        });

        // ---- 滑块 & 数字框双向联动 ----
        bindSliderPair("arcvigil-temperature");
        bindSliderPair("arcvigil-top-p");
        bindSliderPair("arcvigil-top-k");
        bindSliderPair("arcvigil-max-tokens");

        // ---- 自动保存：监听所有表单控件变化 ----
        $("#arcvigil-settings-container").on("input change", autoSaveSettings);

        // ---- 保存配置 ----
        $("#arcvigil-save-btn").on("click", async () => {
            clearTimeout(autoSaveTimeout);
            saveSettings();
            await pushConfigToBackend();
        });

        // ---- 拉取离线邮件 ----
        $("#arcvigil-sync-btn").on("click", async () => {
            toastr.info("拉取离线邮件中...");
            await pullOfflineMessages();
        });

        // ---- 发送测试邮件 ----
        $("#arcvigil-test-email-btn").on("click", async () => {
            const url = extension_settings[extensionName].backendUrl;
            if (!url) {
                toastr.warning("ArcViGil: 请先填写后端地址");
                return;
            }
            
            // 使用当前输入框里的临时配置发测试，不需要非得点保存
            const mode = $(".arcvigil-mode-tab.active").data("mode") || "resend";
            const testConfig = {
                emailMethod: mode,
                targetEmail: mode === "resend" ? $("#arcvigil-target-email-resend").val() : $("#arcvigil-target-email").val(),
                // Resend
                resendApiKey: $("#arcvigil-resend-api-key").val(),
                resendFrom: $("#arcvigil-resend-from").val(),
                // SMTP
                smtpServer: $("#arcvigil-smtp-server").val(),
                smtpPort: Number($("#arcvigil-smtp-port").val()),
                smtpUser: $("#arcvigil-smtp-user").val(),
                smtpPass: $("#arcvigil-smtp-pass").val(),
            };
            
            if (mode === "resend" && (!testConfig.resendApiKey || !testConfig.targetEmail)) {
                toastr.warning("ArcViGil: 请填完整 Resend 的 API Key 和目标邮箱");
                return;
            }
            if (mode === "smtp" && (!testConfig.smtpServer || !testConfig.smtpUser || !testConfig.smtpPass || !testConfig.targetEmail)) {
                toastr.warning("ArcViGil: 请填完整 SMTP 现实投递配置的所有字段");
                return;
            }

            toastr.info("ArcViGil: 正在发送测试邮件，请稍候...");
            try {
                const res = await fetch(`${url}/api/test_email`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(testConfig)
                });
                
                if (!res.ok) throw new Error("HTTP " + res.status);
                const data = await res.json();
                
                if (data.status === "ok") {
                    toastr.success("ArcViGil: 测试邮件发送成功！请检查你的收件箱。");
                } else {
                    toastr.error("ArcViGil: 测试邮件发送失败，请检查终端日志！");
                }
            } catch (e) {
                console.error("[ArcViGil] Test email failed:", e);
                toastr.error("ArcViGil: 请求失败，请检查后端是否启动或配置是否正确");
            }
        });

        // ---- Soul 列表刷新 ----
        $("#arcvigil-soul-refresh-btn").on("click", refreshSoulList);

        // ---- 信件存档刷新 ----
        $("#arcvigil-letters-refresh-btn").on("click", refreshLetterList);

        // ---- 摘要 LLM 来源切换 ----
        function toggleSummaryCustomPanel() {
            const useMain = $("#arcvigil-summary-use-main-llm").prop("checked");
            $("#arcvigil-summary-custom-panel").toggle(!useMain);
        }
        $("input[name='arcvigil-summary-llm']").on("change", toggleSummaryCustomPanel);
        toggleSummaryCustomPanel();

        // ---- LLM 连通性测试 ----
        $("#arcvigil-test-llm-btn").on("click", async () => {
            const url = extension_settings[extensionName].backendUrl;
            const $result = $("#arcvigil-llm-test-result");
            $result.css("display", "flex");
            if (!url) {
                $result.removeClass("success error").addClass("error")
                    .html('<i class="fa-solid fa-circle-xmark"></i> 请先填写后端地址');
                return;
            }
            $result.removeClass("success error")
                .html('<i class="fa-solid fa-spinner fa-spin"></i> 正在测试，请稍候……');
            try {
                const res = await fetch(`${url}/api/test_llm`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        apiUrl:   $("#arcvigil-api-url").val(),
                        apiKey:   $("#arcvigil-api-key").val(),
                        apiModel: $("#arcvigil-api-model").val(),
                    }),
                });
                const data = await res.json();
                if (data.status === "ok") {
                    $result.addClass("success")
                        .html(`<i class="fa-solid fa-circle-check"></i> ${data.message}，模型回复：<b>${data.reply}</b><br><span style="opacity:0.7;font-size:10px;">实际请求地址: ${data.normalized_url}</span>`);
                } else {
                    $result.addClass("error")
                        .html(`<i class="fa-solid fa-circle-xmark"></i> ${data.message}${data.normalized_url ? `<br><span style="opacity:0.7;font-size:10px;">实际请求地址: ${data.normalized_url}</span>` : ''}`);
                }
            } catch (e) {
                $result.addClass("error")
                    .html('<i class="fa-solid fa-circle-xmark"></i> 无法连接后端，请确认后端已启动');
            }
        });

        // ---- 提示词恢复默认 ----
        $("#arcvigil-sysprompt-reset-btn").on("click", () => {
            if (confirm("确认恢复内置系统调度提示词？当前内容将被覆盖。")) {
                $("#arcvigil-system-prompt").val(DEFAULT_SYSTEM_PROMPT).trigger("change");
            }
        });

        $("#arcvigil-prompt-reset-btn").on("click", () => {
            if (confirm("确认恢复内置默认信件模板？当前内容将被覆盖。")) {
                $("#arcvigil-prompt-template").val(DEFAULT_PROMPT_TEMPLATE).trigger("change");
            }
        });

        // ---- 提示词注入 & 会话切换刷新 ----
        refreshInjection();
        eventSource.on(event_types.CHAT_CHANGED, refreshInjection);

        // ---- 信件存档刷新 ----
        eventSource.on(event_types.MESSAGE_RECEIVED, (messageId) => {
            const mId = typeof messageId === "number" ? messageId : (messageId && messageId.messageId !== undefined ? messageId.messageId : null);
            if (mId !== null) {
                const context = getContext();
                if (context.chat && context.chat[mId]) {
                    const originalMes = context.chat[mId].mes;
                    const newMes = interceptMessage(originalMes);
                    if (originalMes !== newMes) {
                        context.chat[mId].mes = newMes;
                        // 尝试隐藏界面上的 JSON 块
                        const $msgText = $(`#chat .mes[mesid="${mId}"] .mes_text`);
                        if ($msgText.length) {
                            let html = $msgText.html();
                            // 粗略匹配 HTML 里的 JSON 块并移除
                            const regexHTML = /(?:<pre>[\s\S]*?<code[^>]*>)?(?:&lt;ArcViGil&gt;\s*)?(\{\s*(?:&quot;|"|&amp;quot;)ArcViGil_Tasks[\s\S]*?\})(?:\s*&lt;\/ArcViGil&gt;)?(?:<\/code>[\s\S]*?<\/pre>)?/i;
                            $msgText.html(html.replace(regexHTML, ""));
                        }
                    }
                }
            } else if (messageId && messageId.mes) {
                messageId.mes = interceptMessage(messageId.mes);
            }
            // 若开启了动态同步，在收到/渲染消息后自动同步最新聊天上下文给待执行任务
            if (extension_settings[extensionName].autoSyncContext) {
                syncContextToPendingTasks();
            }
        });

        // ---- 心跳 (每 15 秒确认后端在线) ----
        heartbeatInterval = setInterval(sendHeartbeat, 15000);
        sendHeartbeat();

        // ---- 初始刷新任务雷达 + Soul列表 + 信件存档 ----
        setTimeout(() => {
            updateTaskRadar();
            refreshSoulList();
            refreshLetterList();
        }, 1500);

        console.log("[ArcViGil] 初始化完成 ✓");
    } catch (err) {
        console.error("[ArcViGil] 初始化失败:", err);
    }
})();
