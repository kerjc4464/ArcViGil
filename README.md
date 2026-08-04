---

# ArcViGil - I'm Here, Always Online

<div align="center">

![SillyTavern Plugin](https://img.shields.io/badge/Frontend-SillyTavern_Plugin-blue?style=for-the-badge)
![Python Backend](https://img.shields.io/badge/Backend-Python_FastAPI-green?style=for-the-badge)
![License](https://img.shields.io/badge/License-CC%20BY--NC%204.0-lightgrey.svg?style=for-the-badge)

**让角色在你下线后，依然记得你们的约定。**

</div>

---

## 这是什么？

ArcViGil 是一款 SillyTavern 扩展：**当你关闭浏览器下线后，角色依然可以在后台运行，
并按照你们在聊天中定下的约定，在特定时间通过真实的电子邮件给你写信。**

你早上起床打开手机邮箱，会看到昨夜角色"趁你睡觉时"写来的信。

*I'm Here, Always Online.*

---

## 工作原理

1. 聊天中，角色回复末尾输出特殊标记（由系统提示词引导）：
   `{"ArcViGil_Tasks": [{"participants": ["爱尔奎特·布伦史塔德"], "delay_hours": 8, "topic": "今晚月色很美"}]}`
2. 前端拦截并隐藏这段标记，将任务推送给独立后端
3. 后端存入 SQLite，调度器每 10 秒检查到期任务
4. 时间到 → 后端加载 Soul 设定 + 最近聊天记录 → 调用 LLM 生成信件 → 真实发送
5. 你回来后，前端自动拉取离线信件注入上下文——角色"记得"自己写过信

---

## 核心功能

| 功能 | 说明 |
|------|------|
| 跨次元真实发信 | 角色通过 SMTP 或 Resend API 把信发到你真实邮箱 |
| 离线调度系统 | 只要后端黑框在跑，关掉 ST 页面角色也会准时发信 |
| 多角色 Soul 设定 | souls/ 目录放角色设定文件（txt/md/json），发信时保持人设 |
| 记忆闭环回流 | 重新上线后，离线期间的信件自动注入对话上下文 |
| 任务雷达 | 面板实时查看待发任务，可暂停/删除/测试发信 |
| 邮件摘要 | 可选自动生成 70-100 字信件摘要，节省注入 Token |
| 动态上下文同步 | 下线前最新聊天内容可选同步给待执行任务 |

---

## 部署指南

### 第一步：安装前端插件
1. 将本仓库放入 `SillyTavern/public/scripts/extensions/third-party/ArcViGil`
2. 重启 SillyTavern，在扩展列表中启用 ArcViGil

### 第二步：启动后端
1. 需要 Python 3.8+
2. 进入 `ArcViGil-BackEnd` 目录，双击 `start.bat`
3. 看到 `[ArcViGil] Backend and Scheduler started.` 即成功，**保持黑框在后台运行**

### 第三步：连接与配置
1. 刷新 SillyTavern 页面，打开 ArcViGil 设置面板
2. 确认后端地址 `http://127.0.0.1:9000` 且状态灯为 Online
3. 配置 LLM API（用于离线生成信件）
4. 选择发信渠道（推荐 Resend，绕过防火墙；或传统 SMTP）
5. 点击「发送测试邮件」验证收件

---

## Soul 设定说明

在 `ArcViGil-BackEnd/souls/` 下放入角色设定文件，**文件名即角色名**：

```
souls/
├── 爱尔奎特·布伦史塔德.txt
├── 朱月·布伦史塔德.txt
└── 薇薇安·布伦史塔德.txt
```

支持 txt / md / json / yaml 格式。角色输出发信标记时使用的名字必须与文件名一致（不含后缀）。

---

## 隐私说明

- **所有数据都在你自己的电脑上**（SQLite 数据库位于 `ArcViGil-BackEnd/database.db`）
- API Key、SMTP 密码等配置仅保存在本地数据库和 ST 设置中，不上传任何服务器
- 邮件内容由你配置的 LLM 服务生成，经你配置的邮件服务发送至你自己的邮箱
- 本扩展没有云端服务，**零数据外传**

---

## 常见问题

**Q: 后端显示连接失败 / 状态灯是红色的？**
检查 `start.bat` 黑框是否还开着，以及设置面板里后端地址是否为 `http://127.0.0.1:9000`。

**Q: 角色不自动输出发信标记？**
确认设置面板中系统提示词包含调度指令（可点「恢复默认」按钮）。也可以手动在角色回复里加上标记块。

**Q: 邮件发不出去？**
- **Resend**：免费账号需确保发件地址为 `onboarding@resend.dev`，目标邮箱已在 Resend 后台验证
- **SMTP**：确认端口 465 或 587 未被防火墙封锁，QQ/163 邮箱需使用授权码而非登录密码

**Q: 重启后信件记录丢了吗？**
不会。所有已发信件存储在数据库中，刷页不丢失。

---

## 协议

本项目采用 **CC-BY-NC 4.0（署名-非商业性使用）** 许可协议。
允许自由下载、修改、个人使用；禁止商业用途；二次分发需保留署名。

---

<div align="center">
Made by Antigravity & Reality
</div>
