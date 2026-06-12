"""
网关适配器 — DingTalk / WeChat / Feishu
来源: Hermes gateway/platforms/

三平台消息收发实现 + 共享适配器基础设施。

适配器职责:
- 接收平台消息 → 转换为 IncomingMessage
- 发送 OutgoingMessage → 平台格式
- 处理平台特定认证/加密
- 消息去重、重试、连接管理

共享基础设施:
- MessageDeduplicator: 基于 sliding window 的消息去重
- RetryableHTTPClient: 带重试的 HTTP 客户端
- ConnectionManager: WebSocket/长连接生命周期管理
- AdapterRegistry: 统一适配器注册与发现
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("qiyue.gateway")

# 导入各平台适配器
from gateway.adapters.dingtalk import (
    DingTalkAdapter,
    DingTalkStreamAdapter,
    MessageDeduplicator as DingTalkMessageDeduplicator,
    MentionConfig,
    MentionFilter,
)
from gateway.adapters.wechat import (
    WeChatAdapter, WeChatCrypto, WeChatXMLParser, WeChatMediaDownloader,
    verify_webhook_signature, verify_msg_signature,
)
from gateway.adapters.feishu import (
    FeishuAdapter, FeishuTokenManager, FeishuCardBuilder,
    FeishuChallengeVerifier,
)

__all__ = [
    # DingTalk
    "DingTalkAdapter",
    "DingTalkStreamAdapter",
    # WeChat
    "WeChatAdapter",
    "WeChatCrypto",
    "WeChatXMLParser",
    "WeChatMediaDownloader",
    "verify_webhook_signature",
    "verify_msg_signature",
    # Feishu
    "FeishuAdapter",
    "FeishuTokenManager",
    "FeishuCardBuilder",
    "FeishuChallengeVerifier",
    # 共享基础设施
    "MessageDeduplicator",
    "RetryableHTTPClient",
    "ConnectionManager",
    "AdapterRegistry",
    "HealthChecker",
    "create_adapters_from_config",
]


# ═══════════════════════════════════════════
# 消息去重器 (跨平台通用)
# ═══════════════════════════════════════════

class MessageDeduplicator:
    """
    基于滑动窗口的消息去重器

    特性:
    - UUID/MsgId 去重
    - TTL 自动过期
    - 窗口大小限制 (LRU 驱逐)
    - 线程安全
    """

    def __init__(
        self,
        window_size: int = 2000,
        ttl_seconds: int = 600,
        namespace: str = "default",
    ):
        self._window_size = window_size
        self._ttl = ttl_seconds
        self._namespace = namespace
        self._seen: Dict[str, float] = {}
        self._stats = {"total": 0, "duplicates": 0, "expired": 0}

    def is_duplicate(self, msg_id: str) -> bool:
        """检查消息是否已处理过。False = 新消息, True = 重复"""
        if not msg_id:
            return False
        key = f"{self._namespace}:{msg_id}"
        now = time.time()

        # 批量清理过期
        self._maybe_cleanup(now)

        self._stats["total"] += 1

        if key in self._seen:
            self._stats["duplicates"] += 1
            return True

        # 维护窗口大小
        if len(self._seen) >= self._window_size:
            oldest = min(self._seen, key=self._seen.get)
            del self._seen[oldest]
            self._stats["expired"] += 1

        self._seen[key] = now
        return False

    def _maybe_cleanup(self, now: float) -> None:
        """定期清理过期条目（每 60 秒最多一次）"""
        if not hasattr(self, "_last_cleanup"):
            self._last_cleanup = 0.0
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        expired = [k for k, v in self._seen.items() if now - v > self._ttl]
        for k in expired:
            del self._seen[k]

    def clear(self) -> None:
        """清空去重缓存"""
        self._seen.clear()

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)


# ═══════════════════════════════════════════
# 可重试 HTTP 客户端
# ═══════════════════════════════════════════

class RetryableHTTPClient:
    """
    带重试逻辑的 HTTP 客户端

    特性:
    - 自动重试 (指数退避 + jitter)
    - 可配置的重试条件
    - 请求超时
    - 连接池复用
    - 响应日志
    """

    DEFAULT_RETRY_STATUSES: Set[int] = {429, 500, 502, 503, 504}
    DEFAULT_BACKOFF: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(
        self,
        max_retries: int = 3,
        timeout: float = 30.0,
        retry_statuses: Set[int] = None,
        backoff: Tuple[float, ...] = None,
    ):
        self.max_retries = max_retries
        self.timeout = timeout
        self.retry_statuses = retry_statuses or self.DEFAULT_RETRY_STATUSES
        self.backoff = backoff or self.DEFAULT_BACKOFF
        self._http: Optional[Any] = None
        self._stats = {"requests": 0, "retries": 0, "errors": 0}

    async def _ensure_client(self):
        """确保 HTTP 客户端已初始化"""
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=30,
                ),
            )
        return self._http

    async def get(self, url: str, **kwargs) -> Any:
        """GET 请求 (带重试)"""
        return await self._request_with_retry("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> Any:
        """POST 请求 (带重试)"""
        return await self._request_with_retry("POST", url, **kwargs)

    async def put(self, url: str, **kwargs) -> Any:
        """PUT 请求 (带重试)"""
        return await self._request_with_retry("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs) -> Any:
        """DELETE 请求 (带重试)"""
        return await self._request_with_retry("DELETE", url, **kwargs)

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> Any:
        """执行 HTTP 请求（带重试逻辑）"""
        client = await self._ensure_client()
        self._stats["requests"] += 1

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code < 500 or resp.status_code not in self.retry_statuses:
                    return resp
                if attempt < self.max_retries:
                    delay = self._get_delay(attempt)
                    logger.debug(
                        "[HTTP] %s %s -> %d, retry in %.1fs (attempt %d/%d)",
                        method, url[:80], resp.status_code, delay,
                        attempt + 1, self.max_retries + 1,
                    )
                    self._stats["retries"] += 1
                    await asyncio.sleep(delay)
                else:
                    return resp
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    delay = self._get_delay(attempt)
                    logger.warning(
                        "[HTTP] %s %s failed: %s, retry in %.1fs",
                        method, url[:80], str(e)[:100], delay,
                    )
                    self._stats["retries"] += 1
                    await asyncio.sleep(delay)

        self._stats["errors"] += 1
        raise last_error

    def _get_delay(self, attempt: int) -> float:
        """计算退避延迟"""
        if attempt < len(self.backoff):
            base = self.backoff[attempt]
        else:
            base = self.backoff[-1]
        jitter = base * 0.2 * random.random()
        return base + jitter

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)


# ═══════════════════════════════════════════
# 连接管理器 (WebSocket/长连接)
# ═══════════════════════════════════════════

@dataclass
class ConnectionState:
    """连接状态"""
    connected: bool = False
    last_connected: float = 0.0
    reconnect_count: int = 0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    messages_received: int = 0
    messages_sent: int = 0


class ConnectionManager:
    """
    WebSocket/长连接管理器

    特性:
    - 自动重连 (指数退避)
    - 连接健康检查
    - 最大重连次数限制
    - 连接状态追踪
    """

    MAX_RECONNECT_ATTEMPTS: int = 100
    HEALTH_CHECK_INTERVAL: float = 30.0

    def __init__(
        self,
        name: str = "adapter",
        max_reconnect_attempts: int = None,
        backoff_sequence: Tuple[float, ...] = (2, 5, 10, 30, 60),
    ):
        self.name = name
        self.max_reconnect_attempts = max_reconnect_attempts or self.MAX_RECONNECT_ATTEMPTS
        self.backoff_sequence = backoff_sequence
        self.state = ConnectionState()
        self._running = False

    async def maintain_connection(
        self,
        connect_fn: Callable[[], Any],
        disconnect_fn: Callable[[], Any] = None,
        health_check_fn: Callable[[], bool] = None,
    ) -> None:
        """
        维护长连接：自动重连循环。

        Args:
            connect_fn: async 连接函数
            disconnect_fn: async 断开函数（可选）
            health_check_fn: 健康检查函数，返回 True 表示连接正常
        """
        self._running = True

        while self._running:
            try:
                logger.info("[Connection:%s] Connecting...", self.name)
                await connect_fn()
                self.state.connected = True
                self.state.last_connected = time.time()
                self.state.consecutive_failures = 0
                logger.info("[Connection:%s] Connected ✓", self.name)

                # 健康检查循环
                while self._running and self.state.connected:
                    if health_check_fn:
                        try:
                            healthy = await health_check_fn()
                            if not healthy:
                                logger.warning(
                                    "[Connection:%s] Health check failed, reconnecting...",
                                    self.name,
                                )
                                self.state.connected = False
                                break
                        except Exception:
                            pass

                    await asyncio.sleep(self.HEALTH_CHECK_INTERVAL)

            except Exception as e:
                self.state.connected = False
                self.state.consecutive_failures += 1
                self.state.last_error = str(e)[:200]

                if not self._running:
                    break

                if self.state.reconnect_count >= self.max_reconnect_attempts:
                    logger.error(
                        "[Connection:%s] Max reconnect attempts (%d) reached. Stopping.",
                        self.name, self.max_reconnect_attempts,
                    )
                    break

                self.state.reconnect_count += 1
                delay = self._get_backoff_delay(self.state.reconnect_count)

                logger.warning(
                    "[Connection:%s] Connection lost: %s. Reconnecting in %.1fs (attempt %d)",
                    self.name, str(e)[:100], delay, self.state.reconnect_count,
                )
                await asyncio.sleep(delay)

        # 清理
        if disconnect_fn:
            try:
                await disconnect_fn()
            except Exception:
                pass

        logger.info("[Connection:%s] Connection loop ended", self.name)

    def _get_backoff_delay(self, attempt: int) -> float:
        """获取退避延迟"""
        idx = min(attempt - 1, len(self.backoff_sequence) - 1)
        base = self.backoff_sequence[idx]
        jitter = base * 0.3 * random.random()
        return base + jitter

    def stop(self) -> None:
        """停止连接维护"""
        self._running = False

    def reset_reconnect_count(self) -> None:
        """重置重连计数"""
        self.state.reconnect_count = 0
        self.state.consecutive_failures = 0


# ═══════════════════════════════════════════
# 适配器注册表
# ═══════════════════════════════════════════

@dataclass
class AdapterInfo:
    """适配器元信息"""
    name: str
    platform: str
    adapter_class: type
    description: str = ""
    required_env: List[str] = field(default_factory=list)
    required_packages: List[str] = field(default_factory=list)


class AdapterRegistry:
    """
    统一适配器注册与发现

    用法:
        registry = AdapterRegistry()
        registry.register("dingtalk", DingTalkStreamAdapter, ...)
        adapter = registry.create("dingtalk", config={})
    """

    def __init__(self):
        self._adapters: Dict[str, AdapterInfo] = {}
        self._instances: Dict[str, Any] = {}

    def register(
        self,
        name: str,
        adapter_class: type,
        platform: str = None,
        description: str = "",
        required_env: List[str] = None,
        required_packages: List[str] = None,
    ) -> None:
        """注册适配器"""
        self._adapters[name] = AdapterInfo(
            name=name,
            platform=platform or name,
            adapter_class=adapter_class,
            description=description,
            required_env=required_env or [],
            required_packages=required_packages or [],
        )
        logger.info("[AdapterRegistry] Registered: %s", name)

    def unregister(self, name: str) -> None:
        """取消注册"""
        self._adapters.pop(name, None)
        self._instances.pop(name, None)

    def create(
        self,
        name: str,
        config: Dict[str, Any] = None,
        **kwargs,
    ) -> Optional[Any]:
        """
        创建适配器实例

        Args:
            name: 适配器名称
            config: 平台配置 dict
            **kwargs: 传递给适配器构造函数的额外参数

        Returns:
            适配器实例 或 None (如果 name 不存在或依赖缺失)
        """
        info = self._adapters.get(name)
        if not info:
            logger.error("[AdapterRegistry] Unknown adapter: %s", name)
            return None

        # 检查环境变量
        missing_env = [e for e in info.required_env if not os.getenv(e)]
        if missing_env:
            logger.warning(
                "[AdapterRegistry] %s: missing env vars: %s. Adapter may not work.",
                name, ", ".join(missing_env),
            )

        # 创建实例
        try:
            instance = info.adapter_class(config=config, **kwargs)
            self._instances[name] = instance
            logger.info("[AdapterRegistry] Created adapter: %s", name)
            return instance
        except Exception as e:
            logger.error("[AdapterRegistry] Failed to create %s: %s", name, e)
            return None

    def get(self, name: str) -> Optional[Any]:
        """获取已创建的适配器实例"""
        return self._instances.get(name)

    def list_adapters(self) -> List[Dict[str, Any]]:
        """列出所有注册的适配器"""
        return [
            {
                "name": info.name,
                "platform": info.platform,
                "description": info.description,
                "required_env": info.required_env,
                "created": info.name in self._instances,
            }
            for info in self._adapters.values()
        ]

    async def start_all(self) -> Dict[str, bool]:
        """启动所有已创建的适配器"""
        results = {}
        for name, adapter in self._instances.items():
            try:
                if hasattr(adapter, "start"):
                    await adapter.start()
                results[name] = True
            except Exception as e:
                logger.error("[AdapterRegistry] Failed to start %s: %s", name, e)
                results[name] = False
        return results

    async def stop_all(self) -> Dict[str, bool]:
        """停止所有适配器"""
        results = {}
        for name, adapter in self._instances.items():
            try:
                if hasattr(adapter, "stop"):
                    await adapter.stop()
                results[name] = True
            except Exception as e:
                logger.error("[AdapterRegistry] Failed to stop %s: %s", name, e)
                results[name] = False
        return results


# ═══════════════════════════════════════════
# 健康检查器
# ═══════════════════════════════════════════

class HealthChecker:
    """
    适配器健康检查器

    定期检查所有适配器的连接状态，报告异常。
    """

    def __init__(self, registry: AdapterRegistry, check_interval: float = 30.0):
        self.registry = registry
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._on_unhealthy: Optional[Callable[[str, str], None]] = None

    async def start(self) -> None:
        """启动健康检查"""
        self._running = True
        self._task = asyncio.create_task(self._run_checks())
        logger.info("[HealthCheck] Started (interval=%.1fs)", self.check_interval)

    async def stop(self) -> None:
        """停止健康检查"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def on_unhealthy(self, callback: Callable[[str, str], None]) -> None:
        """注册不健康回调: callback(adapter_name, reason)"""
        self._on_unhealthy = callback

    async def _run_checks(self) -> None:
        """运行健康检查循环"""
        while self._running:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[HealthCheck] Error: %s", e)
            await asyncio.sleep(self.check_interval)

    async def _check_all(self) -> None:
        """检查所有适配器"""
        for name, adapter in list(self.registry._instances.items()):
            try:
                if hasattr(adapter, "state"):
                    state = adapter.state
                    if not state.connected and state.consecutive_failures > 2:
                        reason = state.last_error or "unknown"
                        logger.warning(
                            "[HealthCheck] %s unhealthy: %s (failures=%d)",
                            name, reason, state.consecutive_failures,
                        )
                        if self._on_unhealthy:
                            try:
                                self._on_unhealthy(name, reason)
                            except Exception:
                                pass
            except Exception as e:
                logger.debug("[HealthCheck] Error checking %s: %s", name, e)

    async def check_single(self, name: str) -> Dict[str, Any]:
        """检查单个适配器"""
        adapter = self.registry.get(name)
        if not adapter:
            return {"name": name, "status": "not_found"}

        result = {"name": name, "status": "unknown"}

        try:
            # 检查连接状态
            if hasattr(adapter, "state"):
                state = adapter.state
                result.update({
                    "connected": state.connected,
                    "reconnect_count": state.reconnect_count,
                    "consecutive_failures": state.consecutive_failures,
                    "last_error": state.last_error,
                    "messages_received": state.messages_received,
                    "messages_sent": state.messages_sent,
                })
                result["status"] = "healthy" if state.connected else "unhealthy"
            elif hasattr(adapter, "_running"):
                result["status"] = "healthy" if adapter._running else "stopped"
            else:
                result["status"] = "unknown"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

        return result


# ═══════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════

# 全局注册表实例
_registry: Optional[AdapterRegistry] = None


def get_registry() -> AdapterRegistry:
    """获取全局适配器注册表"""
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
        _register_builtin_adapters(_registry)
    return _registry


def _register_builtin_adapters(registry: AdapterRegistry) -> None:
    """注册内置适配器"""
    registry.register(
        "dingtalk",
        DingTalkStreamAdapter,
        platform="dingtalk",
        description="钉钉 Stream Mode 适配器 (WebSocket 长连接)",
        required_env=["DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"],
        required_packages=["dingtalk-stream", "httpx"],
    )
    registry.register(
        "wechat",
        WeChatAdapter,
        platform="wechat",
        description="企业微信适配器 (Webhook/Callback 双模式)",
        required_env=[],
        required_packages=["httpx"],
    )
    registry.register(
        "feishu",
        FeishuAdapter,
        platform="feishu",
        description="飞书适配器 (事件订阅 V2 + WebSocket)",
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        required_packages=["httpx"],
    )


async def create_adapters_from_config(
    configs: Dict[str, Dict[str, Any]],
    **shared_kwargs,
) -> Dict[str, Any]:
    """
    从配置创建多个适配器。

    Args:
        configs: {"dingtalk": {platform_config}, "wechat": {...}, ...}
        **shared_kwargs: 传递给所有适配器的共享参数

    Returns:
        {"dingtalk": adapter_instance, ...}
    """
    registry = get_registry()
    adapters = {}

    for name, config in configs.items():
        if not config.get("enabled", True):
            continue
        adapter = registry.create(name, config=config, **shared_kwargs)
        if adapter:
            adapters[name] = adapter

    return adapters
