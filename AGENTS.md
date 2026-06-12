# AGENTS.md — Nodus 架构与能力

> Nodus 是单一统一智能体，包含三个原始 agent 的全部能力。

---

## 架构

```
用户消息 → gateway（情绪感知 + ACK）→ brain（推理循环 + 工具调用）→ executor（执行）
```

| 层 | 模块 | 来源 |
|----|------|------|
| 网关 | `gateway/` — 钉钉/微信/飞书 Stream 适配器 + 情绪检测 | NanoBot |
| 思维 | `brain/` — persona 人设 + 推理循环 + 工具调用 | Hermes |
| 执行 | `executor/` — Shell/File/Search/Browser | OpenClaw |
| 共享 | `shared/` — LLM 客户端 + 事件总线 | 三者合并 |

---

## 核心能力

### 网关层
- 三通道消息收发（钉钉 Stream Mode / 微信 / 飞书）
- 情绪感知（关键词匹配，μs 级）
- 智能 ACK（按意图选模板）
- 会话持久化

### 思维层
- Persona 统一人设（柒月）
- 推理循环：LLM 自行决定调用工具 → 看到结果 → 回复
- 系统 prompt 启动时一次性构建（含路径、工具、核心文件）
- 情绪应对策略

### 执行层
- `shell_exec` — 命令执行（10s 超时）
- `file_read/write/patch/search/find` — 文件操作
- `web_search/fetch` — 网络

---

## 启动方式

```cmd
python cli.py start          # 测试模式（控制台交互）
python cli.py start --serve  # 服务模式（连接真实平台）
```

---

## 开发规则

1. VM 先测再上线 — 宿主机文件不允许直接改动
2. 只改用户指定的地方 — 用户说改哪就改哪
3. 禁止向生产通道发测试消息
4. "别动" = 立即停止一切操作
