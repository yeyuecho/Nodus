"""
技能加载器 — SKILL.md YAML frontmatter 解析
等价于 OpenClaw 的 SkillHub / ClawHub 技能加载

SKILL.md 格式:
---
name: my-skill
version: 1.0.0
description: ...
triggers: [...]
---
# markdown body
"""

import yaml
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class Skill:
    """技能定义"""
    name: str
    slug: str
    version: str = "0.1.0"
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    source: str = "local"
    body: str = ""            # SKILL.md 的 Markdown 正文
    path: Optional[Path] = None

    def match_intent(self, intent: str) -> bool:
        """检查意图是否匹配此技能的触发条件"""
        intent_lower = intent.lower()
        for trigger in self.triggers:
            if trigger.lower() in intent_lower:
                return True
        return intent_lower in self.description.lower()


class SkillLoader:
    """技能加载器 — 兼容 SkillHub/ClawHub 的 SKILL.md 格式"""

    FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._registry: dict[str, Skill] = {}

    def load_all(self) -> dict[str, Skill]:
        """扫描 skills/ 目录，加载所有技能"""
        self._registry.clear()
        if not self.skills_dir.exists():
            return self._registry

        for skill_dir in self.skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                skill = self.load_from_file(skill_md)
                if skill:
                    self._registry[skill.slug] = skill
        return self._registry

    def load_from_file(self, path: Path) -> Optional[Skill]:
        """从单个 SKILL.md 文件加载技能"""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return None

        # 解析 YAML frontmatter
        match = self.FRONTMATTER_RE.match(content)
        if not match:
            return None

        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None

        if not isinstance(frontmatter, dict):
            return None

        body = content[match.end():].strip()

        return Skill(
            name=frontmatter.get("name", path.parent.name),
            slug=frontmatter.get("slug", path.parent.name),
            version=str(frontmatter.get("version", "0.1.0")),
            description=frontmatter.get("description", ""),
            triggers=frontmatter.get("triggers", []),
            tools=frontmatter.get("tools", []),
            source=frontmatter.get("source", "local"),
            body=body,
            path=path,
        )

    def match(self, intent: str, description: str = "") -> Optional[Skill]:
        """根据意图匹配技能"""
        for skill in self._registry.values():
            if skill.match_intent(intent):
                return skill
            if description and skill.match_intent(description):
                return skill
        return None

    def get(self, slug: str) -> Optional[Skill]:
        return self._registry.get(slug)

    def list_all(self) -> list[Skill]:
        return list(self._registry.values())

    def register(self, skill: Skill):
        """手动注册技能"""
        self._registry[skill.slug] = skill

    def create_skeleton(self, name: str, intent: str,
                        tools: list[str] = None,
                        workflow: str = "") -> Skill:
        """创建技能骨架（Self-Skill Engine 使用）"""
        slug = name.lower().replace(" ", "-")
        return Skill(
            name=name,
            slug=slug,
            version="0.1.0",
            description=f"自动生成的技能 - 意图: {intent}",
            triggers=[intent],
            tools=tools or [],
            source="self_generated",
            body=f"## Workflow\n\n{workflow}\n\n"
                 f"> 此技能由 Self-Skill Engine 自动生成 (v0.1.0)\n"
                 f"> 待实际执行 3 次后升级为 1.0.0",
        )

    def save(self, skill: Skill):
        """保存技能到文件"""
        skill_dir = self.skills_dir / skill.slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        frontmatter = {
            "name": skill.name,
            "slug": skill.slug,
            "version": skill.version,
            "description": skill.description,
            "triggers": skill.triggers,
            "tools": skill.tools,
            "source": skill.source,
        }

        content = "---\n" + yaml.dump(frontmatter, allow_unicode=True) + "---\n\n" + skill.body
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        skill.path = skill_dir / "SKILL.md"


# ─── 使用示例 ───
def _demo():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        loader = SkillLoader(d)
        # 创建示例技能
        skill = loader.create_skeleton(
            name="Web Research",
            intent="information_retrieval",
            tools=["web_search", "browser"],
            workflow="1. 搜索\n2. 抓取页面\n3. 汇总结果"
        )
        loader.save(skill)
        loader.load_all()
        matched = loader.match("帮我查一下")
        print(f"Loaded: {len(loader.list_all())} skills")
        print(f"Matched: {matched.name if matched else 'none'}")


if __name__ == "__main__":
    _demo()
