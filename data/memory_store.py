"""
持久记忆文件 — 跨会话事实存储
来源: Hermes MEMORY.md + Hermes memory tool

存储:
- 用户偏好/规则
- 踩坑记录
- 环境配置事实
- Dream 路由学习数据

格式: Markdown 分区，每区有标记分隔。
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("qiyue.memory")


class MemoryStore:
    """
    持久记忆管理器

    读写 MEMORY.md，支持分区追加和搜索。
    分区用 `## 🧠 分区名` 标记分隔。
    """

    SECTION_MARKERS = {
        "preferences": "## 👤 用户偏好",
        "rules": "## 📋 系统规则",
        "pitfalls": "## 🔥 踩坑记录",
        "env": "## ⚙️ 环境信息",
        "dream": "## 🧠 Dream 路由学习",
        "tasks": "## 📋 任务看板",
        "learned": "## 💡 学到的经验",
    }

    def __init__(self, file_path: str = "data/memory/MEMORY.md"):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # 确保文件存在
        if not self.file_path.exists():
            self._init_file()

    def _init_file(self):
        """初始化记忆文件"""
        content = """# MEMORY.md — 柒月·合一 共享记忆

> 跨会话持久存储。由 Dream 引擎和 Brain 自动维护。

## 👤 用户偏好

（自动学习）
- 语言：中文
- 时区：Asia/Shanghai

## 📋 系统规则

（由用户设定或系统自动生成）

## 🔥 踩坑记录

（执行过程中发现的坑和教训）

## ⚙️ 环境信息

（系统环境配置事实）

## 🧠 Dream 路由学习

（Dream 引擎自动生成的路由分析数据）

## 💡 学到的经验

（从成功/失败中自动提取的经验）
"""
        self.file_path.write_text(content, encoding="utf-8")
        logger.info(f"[Memory] Initialized {self.file_path}")

    # ═══ 读取 ═══

    def read(self, section: str = None) -> str:
        """读取整个文件或指定分区"""
        if not self.file_path.exists():
            return ""

        content = self.file_path.read_text(encoding="utf-8")

        if section:
            marker = self.SECTION_MARKERS.get(section)
            if not marker:
                return ""
            parts = content.split(marker, 1)
            if len(parts) < 2:
                return ""
            # 提取到下一个 ## 标记
            section_content = parts[1]
            next_marker = re.search(r"\n## ", section_content)
            if next_marker:
                section_content = section_content[:next_marker.start()]
            return section_content.strip()

        return content

    def read_section(self, name: str) -> list[str]:
        """读取分区内容，返回条目列表"""
        text = self.read(name)
        if not text:
            return []
        # 提取列表项 (以 - 或数字开头的行)
        lines = text.strip().split("\n")
        items = [l.strip().lstrip("- ").strip() for l in lines
                 if l.strip().startswith("-")]
        return items

    def get(self, key: str) -> Optional[str]:
        """按 key 读取值（key: value 格式）"""
        text = self.read()
        pattern = re.compile(rf"^- \*\*{re.escape(key)}\*\*:?\s*(.+)$", re.MULTILINE)
        match = pattern.search(text)
        return match.group(1).strip() if match else None

    # ═══ 写入 ═══

    def append(self, section: str, entry: str):
        """向指定分区追加一条记录"""
        marker = self.SECTION_MARKERS.get(section)
        if not marker:
            logger.warning(f"[Memory] Unknown section: {section}")
            return

        content = self.file_path.read_text(encoding="utf-8") if self.file_path.exists() else ""

        if marker not in content:
            # 分区不存在 → 创建
            content += f"\n\n{marker}\n\n"
            self.file_path.write_text(content, encoding="utf-8")
            content = self.file_path.read_text(encoding="utf-8")

        # 在分区标记后插入
        parts = content.split(marker, 1)
        before = parts[0] + marker

        after = parts[1] if len(parts) > 1 else ""
        # 找到下一个 ## 标记
        next_section = re.search(r"\n## ", after)
        if next_section:
            after_first = after[:next_section.start()]
            after_rest = after[next_section.start():]
            new_content = before + after_first.rstrip() + f"\n- {entry}\n" + after_rest
        else:
            new_content = before + after.rstrip() + f"\n- {entry}\n"

        self.file_path.write_text(new_content, encoding="utf-8")
        logger.debug(f"[Memory] Appended to {section}: {entry[:80]}")

    def set(self, section: str, key: str, value: str):
        """设置键值对（覆盖同名 key）"""
        old_value = self.get(key)
        if old_value:
            self._replace_line(section, key, value)
        else:
            self.append(section, f"**{key}**: {value}")

    def add_pitfall(self, title: str, description: str):
        """添加踩坑记录"""
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        entry = f"**{title}** ({timestamp}): {description}"
        self.append("pitfalls", entry)

    def add_preference(self, key: str, value: str):
        """添加用户偏好"""
        self.set("preferences", key, value)

    def add_rule(self, rule: str):
        """添加系统规则"""
        self.append("rules", rule)

    def add_learned(self, lesson: str):
        """添加学到的经验"""
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        self.append("learned", f"({timestamp}) {lesson}")

    # ═══ 内部 ═══

    def _replace_line(self, section: str, key: str, new_value: str):
        """替换分区中指定 key 的行"""
        content = self.file_path.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"^(- \*\*{re.escape(key)}\*\*:?\s*).*$",
            re.MULTILINE
        )
        new_content = pattern.sub(rf"\1{new_value}", content)
        self.file_path.write_text(new_content, encoding="utf-8")


# ═══ 便捷函数 ═══

def load_memory(file_path: str = "data/memory/MEMORY.md") -> MemoryStore:
    """快速加载记忆存储"""
    return MemoryStore(file_path)
