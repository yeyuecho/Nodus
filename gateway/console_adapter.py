"""
Console Adapter — 测试模式下打印到终端，不连接真实平台
"""
from shared.core import OutgoingMessage


class ConsoleAdapter:
    """控制台适配器 — 不连外部平台，直接打印到 stdout"""
    platform = None

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, msg: OutgoingMessage):
        """直接打印回复到终端"""
        prefix = "[ACK]" if msg.is_ack else "[REPLY]"
        print(f"\n{prefix} {msg.content}", flush=True)
