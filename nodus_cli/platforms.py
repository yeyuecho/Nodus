"""
Shared platform registry for Nodus.

Single source of truth for platform metadata consumed by both
skills_config (label display) and tools_config (default toolset
resolution).  Import ``PLATFORMS`` from here instead of maintaining
duplicate dicts in each module.
"""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    ("cli",            PlatformInfo(label="🖥️  CLI",            default_toolset="nodus-cli")),
    ("telegram",       PlatformInfo(label="📱 Telegram",        default_toolset="nodus-telegram")),
    ("discord",        PlatformInfo(label="💬 Discord",         default_toolset="nodus-discord")),
    ("slack",          PlatformInfo(label="💼 Slack",           default_toolset="nodus-slack")),
    ("whatsapp",       PlatformInfo(label="📱 WhatsApp",        default_toolset="nodus-whatsapp")),
    ("signal",         PlatformInfo(label="📡 Signal",          default_toolset="nodus-signal")),
    ("bluebubbles",    PlatformInfo(label="💙 BlueBubbles",     default_toolset="nodus-bluebubbles")),
    ("email",          PlatformInfo(label="📧 Email",           default_toolset="nodus-email")),
    ("homeassistant",  PlatformInfo(label="🏠 Home Assistant",  default_toolset="nodus-homeassistant")),
    ("mattermost",     PlatformInfo(label="💬 Mattermost",      default_toolset="nodus-mattermost")),
    ("matrix",         PlatformInfo(label="💬 Matrix",          default_toolset="nodus-matrix")),
    ("dingtalk",       PlatformInfo(label="💬 DingTalk",        default_toolset="nodus-dingtalk")),
    ("feishu",         PlatformInfo(label="🪽 Feishu",          default_toolset="nodus-feishu")),
    ("wecom",          PlatformInfo(label="💬 WeCom",           default_toolset="nodus-wecom")),
    ("wecom_callback", PlatformInfo(label="💬 WeCom Callback",  default_toolset="nodus-wecom-callback")),
    ("weixin",         PlatformInfo(label="💬 Weixin",          default_toolset="nodus-weixin")),
    ("qqbot",          PlatformInfo(label="💬 QQBot",           default_toolset="nodus-qqbot")),
    ("yuanbao",        PlatformInfo(label="🤖 Yuanbao",         default_toolset="nodus-yuanbao")),
    ("webhook",        PlatformInfo(label="🔗 Webhook",         default_toolset="nodus-webhook")),
    ("api_server",     PlatformInfo(label="🌐 API Server",      default_toolset="nodus-api-server")),
    ("cron",           PlatformInfo(label="⏰ Cron",            default_toolset="nodus-cron")),
])


def platform_label(key: str, default: str = "") -> str:
    """Return the display label for a platform key, or *default*.

    Checks the static PLATFORMS dict first, then the plugin platform
    registry for dynamically registered platforms.
    """
    info = PLATFORMS.get(key)
    if info is not None:
        return info.label
    # Check plugin registry
    try:
        from nodus.gateway.platform_registry import platform_registry
        entry = platform_registry.get(key)
        if entry:
            return f"{entry.emoji}  {entry.label}" if entry.emoji else entry.label
    except Exception:
        pass
    return default


def get_all_platforms() -> "OrderedDict[str, PlatformInfo]":
    """Return PLATFORMS merged with any plugin-registered platforms.

    Plugin platforms are appended after builtins.  This is the function
    that tools_config and skills_config should use for platform menus.
    """
    merged = OrderedDict(PLATFORMS)
    try:
        from nodus.gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.name not in merged:
                merged[entry.name] = PlatformInfo(
                    label=f"{entry.emoji}  {entry.label}" if entry.emoji else entry.label,
                    default_toolset=f"nodus-{entry.name}",
                )
    except Exception:
        pass
    return merged
