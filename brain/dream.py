"""
Dream 路由学习引擎 — 自动归纳会话模式
来源: nanobot Dream 引擎 (intervalH=2)

功能:
1. 分析最近会话 → 提取模式
2. 生成路由优化建议 → 写入 MEMORY.md
3. 更新技能优先级
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("qiyue.dream")


@dataclass
class RoutePattern:
    """识别的路由模式"""
    intent: str
    count: int = 0
    avg_confidence: float = 0.0
    action_distribution: dict = field(default_factory=dict)  # {action: count}
    last_seen: float = 0.0


class DreamEngine:
    """
    路由学习引擎

    每 2 小时运行一次，分析最近会话数据，生成路由优化建议。
    """

    def __init__(self, sessions, llm_client=None, memory_file: str = "data/memory/MEMORY.md"):
        self.sessions = sessions          # SessionStore
        self.llm = llm_client             # LLMClient (可选，用于深度分析)
        self.memory_file = Path(memory_file)
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)

        # 学习状态
        self.last_run: float = 0.0
        self.patterns: dict[str, RoutePattern] = {}
        self.total_sessions_analyzed: int = 0

    async def run(self):
        """
        执行一轮 Dream 分析

        1. 扫描最近 24 小时的会话
        2. 提取意图路由模式
        3. 写入 MEMORY.md
        """
        logger.info("[Dream] Starting analysis cycle...")
        start = time.time()

        try:
            # 1. 获取最近活跃会话
            recent_sessions = self.sessions.list_sessions(limit=50)
            if not recent_sessions:
                logger.info("[Dream] No sessions to analyze")
                return

            # 2. 提取路由模式
            new_patterns: dict[str, RoutePattern] = {}

            for session in recent_sessions:
                # 只分析最近 24 小时的会话
                if time.time() - session.last_active > 86400:
                    continue

                messages = self.sessions.get_messages(session.id, limit=50)
                if not messages:
                    continue

                # 从消息中提取意图模式（基于内容关键词）
                for msg in messages:
                    if msg.role != "user":
                        continue
                    intent = self._classify_intent(msg.content)
                    if intent == "unknown":
                        continue

                    if intent not in new_patterns:
                        new_patterns[intent] = RoutePattern(intent=intent)

                    p = new_patterns[intent]
                    p.count += 1
                    p.last_seen = max(p.last_seen, msg.timestamp)

            # 3. 合并到已知模式
            for intent, pattern in new_patterns.items():
                if intent in self.patterns:
                    existing = self.patterns[intent]
                    existing.count += pattern.count
                    existing.last_seen = max(existing.last_seen, pattern.last_seen)
                else:
                    self.patterns[intent] = pattern

            self.total_sessions_analyzed += len(recent_sessions)

            # 4. 生成报告 → 写入 MEMORY.md
            report = self._generate_report(new_patterns)
            self._write_report(report)

            elapsed = time.time() - start
            self.last_run = time.time()
            logger.info(f"[Dream] Analysis complete in {elapsed:.1f}s "
                        f"({len(new_patterns)} patterns, {len(recent_sessions)} sessions)")

        except Exception as e:
            logger.error(f"[Dream] Analysis failed: {e}", exc_info=True)

    def _classify_intent(self, content: str) -> str:
        """基于关键词分类意图（不调 LLM，快速规则匹配）"""
        content_lower = content.lower()

        rules = [
            ("code", ["代码", "bug", "修复", "报错", "error", "写一个", "实现", "重构"]),
            ("file", ["文件", "读取", "写入", "搜索", "查找", "目录"]),
            ("shell", ["运行", "执行", "命令", "启动", "停止", "重启", "安装"]),
            ("browser", ["打开网页", "浏览器", "截图", "网页"]),
            ("search", ["搜索", "查一下", "百度", "谷歌", "查询"]),
            ("config", ["配置", "设置", "修改配置", "config"]),
            ("chat", ["你好", "谢谢", "帮助", "怎么样", "如何"]),
        ]

        for intent, keywords in rules:
            for kw in keywords:
                if kw in content_lower:
                    return intent

        return "unknown"

    def _generate_report(self, new_patterns: dict) -> str:
        """生成 Dream 报告"""
        now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

        report = f"""## Dream 分析报告 — {now}

> 自动生成的路由学习数据

### 本次分析

| 指标 | 值 |
|------|-----|
| 分析会话数 | {sum(1 for p in new_patterns.values())} 活跃 |
| 识别模式数 | {len(new_patterns)} 种 |
| 累计会话 | {self.total_sessions_analyzed} |

### 路由模式分布

| 意图 | 频次 |
|------|------|
"""
        # 按频次排序
        sorted_patterns = sorted(
            new_patterns.values(), key=lambda p: p.count, reverse=True
        )
        for p in sorted_patterns[:10]:
            report += f"| {p.intent} | {p.count} |\n"

        report += f"""
### 全局统计

| 意图 | 累计频次 | 最后出现 |
|------|----------|----------|
"""
        all_sorted = sorted(
            self.patterns.values(), key=lambda p: p.count, reverse=True
        )
        for p in all_sorted[:10]:
            last = time.strftime("%m-%d %H:%M", time.localtime(p.last_seen))
            report += f"| {p.intent} | {p.count} | {last} |\n"

        report += f"""
---
*Dream engine last run: {now}*
"""
        return report

    def _write_report(self, report: str):
        """写入 MEMORY.md（追加到 Dream 专区）"""
        marker = "## 🧠 Dream 路由学习"
        end_marker = "---"

        try:
            if self.memory_file.exists():
                content = self.memory_file.read_text(encoding="utf-8")

                # 替换已有的 Dream 专区
                if marker in content:
                    before = content.split(marker)[0]
                    after_parts = content.split(marker)[1].split(end_marker, 1)
                    after = end_marker + after_parts[1] if len(after_parts) > 1 else ""
                    new_content = before + report
                else:
                    new_content = content.rstrip() + "\n\n" + report
            else:
                new_content = report

            self.memory_file.write_text(new_content, encoding="utf-8")
            logger.debug(f"[Dream] Report written to {self.memory_file}")

        except Exception as e:
            logger.error(f"[Dream] Failed to write report: {e}")


# ═══ 便捷函数 ═══

async def dream_task(dream_engine: DreamEngine):
    """作为 cron job 运行的 Dream 任务"""
    await dream_engine.run()
