# 灵枢 Nodus — 统一智能体

将 Hermes（思维）、NanoBot（通讯）、OpenClaw（执行）三方核心能力合并为单一 Python 进程的统一智能体。

## 架构

```
用户消息 → gateway（纯路由）
              │
              ▼
           brain（一条 while 循环）
              │
    ┌─────────┼─────────┐
    ▼         ▼         ▼
  LLM调用  → ACK确认  → 工具执行 → 回复
```

## 快速开始

### Windows

```
install.bat                     # 一键安装
venv\Scripts\python main.py     # 启动
```

### 其他系统

```
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env              # 编辑填入 DEEPSEEK_API_KEY
cp config.example.json config.json # 编辑填入通道凭证
python main.py
```

## 配置

| 文件 | 用途 |
|------|------|
| `.env` | `DEEPSEEK_API_KEY` |
| `config.json` | 模型参数、通道凭证（钉钉/微信/飞书） |

## 命令

| 命令 | 说明 |
|------|------|
| `python main.py` | 启动（CLI + 通道） |
| `python cli.py doctor` | 系统诊断 |
| `python cli.py version` | 版本信息 |

## 项目结构

```
qiyue-heyi/
├── main.py              # 入口
├── cli.py               # CLI 工具
├── config/              # 默认配置
├── brain/               # 思维层（LLM 循环 + 工具调用）
├── gateway/             # 网关层（钉钉/微信/飞书适配器）
├── executor/            # 执行层（Shell/文件/搜索/浏览器）
├── shared/              # 共享层（LLM 客户端 + 事件总线）
├── skills/              # 技能目录
├── data/                # 运行时数据
├── SOUL.md              # 灵魂定义
├── MEMORY.md            # 长期记忆
├── RULES.md             # 行为红线
└── USER.md              # 用户画像
```
