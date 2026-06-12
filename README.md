# 柒月·合一 — 统一智能体

> 将 nanobot (网关) + Hermes (思维) + OpenClaw (执行) 三体能力统一到一个 Python 进程中。

## 目录结构

```
qiyue-heyi/
├── main.py                 # 入口：组装三层 + 事件连线
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量模板
│
├── gateway/                # ① 通讯层 (来源: nanobot)
│   ├── SOUL.md             #   网关核心身份
│   ├── AGENTS.md           #   工作流定义
│   ├── USER.md             #   用户画像
│   ├── __init__.py         #   适配器 + 路由 + 会话
│   └── adapters/           #   平台适配器实现
│
├── brain/                  # ② 思维层 (来源: Hermes)
│   ├── SOUL.md             #   五大核心能力
│   ├── AGENTS.md           #   规划 + 调度规范
│   ├── __init__.py         #   IntentParser | TaskPlanner | MemoryManager | TaskRouter | SelfSkillEngine
│   └── prompts/            #   LLM 提示词模板
│
├── executor/               # ③ 执行层 (来源: OpenClaw)
│   ├── __init__.py         #   BrowserEngine | WebSearch | SkillLoader
│   └── ...
│
├── shared/                 # ④ 共享模块
│   └── core.py             #   EventBus | LLMClient | 类型定义
│
├── config/                 # 配置
│   └── config.json         #   通道 + 模型配置
│
├── data/                   # 数据
│   ├── sessions/           #   统一会话持久化
│   └── memory/             #   全局记忆
│
└── skills/                 # 技能库 (SKILL.md)
```

## 数据流

```
用户消息 (钉钉/微信/飞书)
    │
    ▼
Gateway.MessageRouter
    ├── 硬编码 ACK 秒回 (~50ms)
    └── EventBus → "message.received"
        │
        ▼
Brain.handle()
    ├── IntentParser.parse()          — 意图识别
    ├── TaskPlanner.plan()            — 任务规划
    ├── MemoryManager.search()        — 查历史经验
    ├── TaskRouter.route()            — 自执行 vs 派发
    ├── SelfSkillEngine.try_generate()— 新技能生成
    └── LLM 推理 → 回复生成
        │
        ├── 代码/修复/系统 → Brain 亲自执行
        └── 浏览器/搜索 → Executor 执行
            │
            ▼
        EventBus → "response.ready"
            │
            ▼
Gateway.MessageRouter.deliver()
    └── 推送给用户
```

## 状态

- [x] 框架骨架搭建
- [x] 接口定义
- [x] 事件总线设计
- [ ] VM 环境测试
- [ ] 适配器实现
- [ ] 上线切换
