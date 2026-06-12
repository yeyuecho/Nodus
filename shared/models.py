"""
多模型适配器 — openai SDK 统一 LLM 调用
等价于 OpenClaw 的多 provider 支持

⚠️ 仅供思维层（Brain）使用。网关层和执行层不引入此模块。

支持: DeepSeek / OpenAI / 任何兼容 OpenAI API 的服务
"""

from openai import AsyncOpenAI
from typing import Optional
from dataclasses import dataclass


@dataclass
class ModelConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-pro"
    api_key: str = ""
    api_base: str = "https://api.deepseek.com/v1"
    max_tokens: int = 8192
    temperature: float = 0.1


@dataclass
class ChatMessage:
    role: str       # system | user | assistant
    content: str


@dataclass
class ChatResponse:
    content: str
    model: str
    usage: dict     # {prompt_tokens, completion_tokens, total_tokens}
    tool_calls: list = None  # [{id, type, function: {name, arguments}}]


class ModelAdapter:
    """多模型适配器 — 兼容 OpenAI API 格式的任何服务"""

    def __init__(self, config: ModelConfig = None):
        self.config = config or ModelConfig()
        self._client: Optional[AsyncOpenAI] = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.api_base,
            )
        return self._client

    async def chat(
        self,
        messages: list[dict],
        model: str = None,
        max_tokens: int = None,
        temperature: float = None,
        json_mode: bool = False,
        tools: list[dict] = None,
        tool_choice: str = None,
    ) -> ChatResponse:
        """发送对话请求"""
        kwargs = {
            "model": model or self.config.model,
            "messages": messages,
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
        }

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        resp = await self.client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        response = ChatResponse(
            content=choice.message.content or "",
            model=resp.model,
            usage={
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
                "total_tokens": resp.usage.total_tokens if resp.usage else 0,
            },
        )

        # 附加 tool_calls（如果有）—— 兼容 DeepSeek 返回格式
        tc = getattr(choice.message, 'tool_calls', None) or []
        if tc:
            response.tool_calls = []
            for t in tc:
                # 兼容对象格式和字典格式
                if hasattr(t, 'function'):
                    response.tool_calls.append({
                        "id": getattr(t, 'id', ''),
                        "type": "function",
                        "function": {
                            "name": t.function.name,
                            "arguments": t.function.arguments,
                        },
                    })
                elif isinstance(t, dict):
                    response.tool_calls.append(t)

        return response

    async def chat_json(
        self,
        messages: list[dict],
        model: str = None,
        schema: dict = None,
    ) -> dict:
        """发送对话，强制返回 JSON"""
        import json

        if schema:
            messages.insert(0, {
                "role": "system",
                "content": f"Output valid JSON matching this schema:\n{json.dumps(schema)}"
            })

        resp = await self.chat(messages, model=model, json_mode=True)
        try:
            return json.loads(resp.content)
        except json.JSONDecodeError:
            return {"raw": resp.content, "error": "JSON parse failed"}

    async def stream(self, messages: list[dict], model: str = None):
        """流式对话（生成器）"""
        stream = await self.client.chat.completions.create(
            model=model or self.config.model,
            messages=messages,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    @classmethod
    def from_env(cls, env_prefix: str = "DEEPSEEK") -> "ModelAdapter":
        """从环境变量创建"""
        import os
        return cls(ModelConfig(
            api_key=os.getenv(f"{env_prefix}_API_KEY", ""),
            api_base=os.getenv(f"{env_prefix}_API_BASE", "https://api.deepseek.com/v1"),
            model=os.getenv(f"{env_prefix}_MODEL", "deepseek-v4-pro"),
        ))


# ─── 便捷函数 ───

def create_deepseek_adapter(api_key: str, model: str = "deepseek-v4-pro") -> ModelAdapter:
    """快速创建 DeepSeek 适配器"""
    return ModelAdapter(ModelConfig(
        provider="deepseek",
        model=model,
        api_key=api_key,
        api_base="https://api.deepseek.com/v1",
    ))


# ─── 使用示例 ───
async def _demo():
    import os
    adapter = ModelAdapter.from_env("DEEPSEEK")
    if adapter.config.api_key:
        resp = await adapter.chat([
            {"role": "user", "content": "Say hello in one word."}
        ])
        print(f"Response: {resp.content}")
        print(f"Tokens: {resp.usage}")
    else:
        print("Set DEEPSEEK_API_KEY to test")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_demo())
