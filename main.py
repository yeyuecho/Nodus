"""
柒月·合一 — 统一智能体入口

组装三层：gateway → brain → executor
启动 Webhook + Cron
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

# 加载 .env 文件（如果存在）
def _load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                if key and not os.getenv(key):
                    os.environ[key] = val.strip()
_load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from shared.core import (
    EventBus, LLMClient, LLMConfig, OutgoingMessage, Platform,
)
from shared.llm_client import create_llm_client, LLMProvider
from gateway import MessageRouter
from gateway.adapters import DingTalkAdapter, WeChatAdapter, FeishuAdapter
from gateway.console_adapter import ConsoleAdapter
from gateway.server import WebhookServer
from brain import Brain
from brain.cron import CronScheduler, heartbeat_task, session_compact_task
from brain.persona import DEFAULT_PERSONA
from executor import Executor
from executor.shell import ShellExecutor
from executor.files import FileTools
from data.session_store import SessionStore
from data.memory_store import MemoryStore
from brain.dream import DreamEngine, dream_task

# 日志：控制台只显示 WARNING 以上，详细信息写入文件
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "nodus.log", encoding="utf-8"),
    ],
)
# 文件日志记录 INFO+
logging.getLogger().handlers[1].setLevel(logging.INFO)

logger = logging.getLogger("qiyue")


def load_config():
    """从 config.json 和环境变量加载配置"""
    config_path = Path(__file__).parent / "config.json"

    llm_config = LLMConfig(
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    )

    channel_config = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = json.load(f)

        providers = raw.get("providers", {})
        deepseek = providers.get("deepseek", {})
        if not llm_config.api_key:
            llm_config.api_key = deepseek.get("apiKey", "")
        if not llm_config.api_base:
            llm_config.api_base = deepseek.get("apiBase", "https://api.deepseek.com/v1")

        channels = raw.get("channels", {})
        d = channels.get("dingtalk", {})
        w = channels.get("weixin", {})
        f = channels.get("feishu", {})
        channel_config = {
            "dingtalk_client_id": os.getenv("DINGTALK_CLIENT_ID", d.get("clientId", "")),
            "dingtalk_client_secret": os.getenv("DINGTALK_CLIENT_SECRET", d.get("clientSecret", "")),
            "wechat_token": os.getenv("WECHAT_TOKEN", w.get("token", "")),
            "feishu_app_id": os.getenv("FEISHU_APP_ID", f.get("appId", "")),
            "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", f.get("appSecret", "")),
        }

    llm_config.api_key = llm_config.api_key or os.getenv("DEEPSEEK_API_KEY", "")
    llm_config.model = os.getenv("DEEPSEEK_MODEL", llm_config.model)
    return llm_config, channel_config


async def main():
    logger.info("=" * 50)
    logger.info("柒月·合一 启动中...")
    logger.info("=" * 50)

    # 1. 配置
    llm_config, channel_config = load_config()
    if not llm_config.api_key:
        logger.warning("⚠️  DEEPSEEK_API_KEY 未设置！用 export 设置后重启。")

    # 2. 基础设施
    bus = EventBus()
    llm = LLMClient(llm_config)
    sessions = SessionStore("data/sessions.db")
    memory = MemoryStore("data/memory/MEMORY.md")
    dream = DreamEngine(sessions, llm, "data/memory/MEMORY.md")
    logger.info("[Init] EventBus + LLMClient + SessionStore + MemoryStore + Dream OK")

    # 3. 执行层 + 工具
    executor = Executor(skills_dir="skills", browser_headless=True)
    shell = ShellExecutor(timeout=60)
    files = FileTools()

    # 注册更多工具到 executor
    executor._handlers = {
        "shell_exec": lambda p: shell.execute(p["command"]),
        "file_read": lambda p: files.read(p["path"], p.get("offset", 1), p.get("limit", 500)),
        "file_write": lambda p: files.write(p["path"], p["content"]),
        "file_patch": lambda p: files.patch(p["path"], p["old_string"], p["new_string"], p.get("replace_all", False)),
        "file_search": lambda p: files.search(p["pattern"], p.get("path", "."), p.get("file_glob"), p.get("limit", 50)),
        "file_find": lambda p: files.find_files(p["pattern"], p.get("path", ".")),
        "browser_navigate": lambda p: executor.browser.navigate(p["url"]),
        "browser_click": lambda p: executor.browser.click(p["selector"]),
        "web_search": lambda p: executor.search.search(p["query"], p.get("max_results", 5)),
        "web_fetch": lambda p: executor.search.fetch_text(p["url"]),
        "sandbox_exec": lambda p: executor.sandbox.execute_script(p["script"]),
    }

    logger.info(f"[Executor] {len(executor._handlers)} tools registered")
    logger.info(f"[Executor] {len(executor.skills.list_all())} skills loaded")

    # 4. 网关层 — 根据模式选择适配器
    import sys
    is_serve = len(sys.argv) > 1 and sys.argv[1] == "--serve"

    if is_serve:
        # 服务模式：连接真实平台
        adapters = []
        if channel_config.get("dingtalk_client_id"):
            adapters.append(DingTalkAdapter(channel_config["dingtalk_client_id"], channel_config["dingtalk_client_secret"]))
        if channel_config.get("wechat_token"):
            adapters.append(WeChatAdapter(token=channel_config["wechat_token"]))
        if channel_config.get("feishu_app_id"):
            adapters.append(FeishuAdapter(channel_config["feishu_app_id"], channel_config["feishu_app_secret"]))
        logger.info(f"[Gateway] {len(adapters)} real adapters loaded")
        for adapter in adapters:
            asyncio.create_task(adapter.start())
    else:
        # 测试模式：仅控制台输出
        adapters = [ConsoleAdapter()]
        logger.info("[Gateway] Console-only mode (use --serve for real platforms)")

    gateway = MessageRouter(bus, adapters, sessions)
    logger.info(f"[Gateway] {len(adapters)} adapters loaded")

    # 5. Webhook 服务器（仅服务模式）
    if is_serve:
        async def on_webhook_message(msg):
            await gateway.route(msg)
        port = int(os.getenv("GATEWAY_PORT", "18791"))
        webhook = WebhookServer(host="0.0.0.0", port=port, on_message=on_webhook_message)
        await webhook.start()
    else:
        webhook = None
    port = int(os.getenv("GATEWAY_PORT", "18791"))

    # 6. 思维层 — 注入统一人设
    brain = Brain(llm, bus, sessions, executor=executor, memory=memory,
                  persona=DEFAULT_PERSONA)
    brain.register_skill_loader(executor.skills)
    from config.defaults import TOOL_DESCRIPTIONS
    brain.register_tools(TOOL_DESCRIPTIONS)
    logger.info(f"[Brain] {DEFAULT_PERSONA.name} 五大能力 ready (tools injected, persona active)")

    # 7. 定时调度
    cron = CronScheduler()
    cron.add("heartbeat", lambda: heartbeat_task(sessions, gateway), interval_s=1800)
    cron.add("session_compact", lambda: session_compact_task(sessions), interval_s=3600)
    cron.add("dream", lambda: dream_task(dream), interval_s=7200)  # 每 2 小时
    await cron.start()
    logger.info(f"[Cron] {len(cron._jobs)} jobs registered (heartbeat + compact + dream)")

    # 8. 事件连线
    bus.on("message.received", brain.handle)

    async def on_response_ready(message_id, content, session_id=None,
                                platform=None, channel_id=None,
                                is_ack=False, **kwargs):
        await gateway.deliver(OutgoingMessage(
            reply_to=session_id or message_id,
            content=content,
            platform=platform,
            channel_id=channel_id,
            is_ack=is_ack,
            is_final=not is_ack,
        ))

    bus.on("response.ready", on_response_ready)

    logger.info("=" * 50)
    logger.info("✅ 柒月·合一 已就绪")
    logger.info(f"   Webhook: http://0.0.0.0:{port}")
    logger.info(f"   Gateway: {len(adapters)} 通道")
    logger.info(f"   Brain: {DEFAULT_PERSONA.name} @ {llm_config.model}")
    logger.info(f"   Persona: {', '.join(DEFAULT_PERSONA.traits[:3])}")
    logger.info(f"   Executor: {len(executor._handlers)} 工具 + {len(executor.skills.list_all())} 技能")
    logger.info(f"   Cron: {len(cron._jobs)} jobs")
    logger.info("=" * 50)

    # 6. 交互测试模式
    import sys
    print(f"\n  Nodus → http://0.0.0.0:{port}")
    print(f"  通道: {len(adapters)} 个适配器")
    print(f"  输入消息测试 (输入 quit 退出)\n")
    loop = asyncio.get_running_loop()
    try:
        while True:
            user_input = await loop.run_in_executor(None, input, "你> ")
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if not user_input.strip():
                continue
            from shared.core import IncomingMessage
            test_msg = IncomingMessage(
                id=f"test_{int(time.time()*1000)}",
                platform=Platform.DINGTALK,
                channel_id="cli-test",
                sender_id="user",
                content=user_input,
                timestamp=time.time(),
            )
            await gateway.route(test_msg)
    except (KeyboardInterrupt, EOFError):
        print("\n  正在关闭...")

    # 10. 关闭
    logger.info("Shutting down...")
    await cron.stop()
    if webhook:
        await webhook.stop()
    for adapter in adapters:
        try:
            await adapter.stop()
        except Exception:
            pass
    await executor.close()
    sessions.close()
    logger.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
