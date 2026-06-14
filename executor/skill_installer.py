"""
ClawHub Skill Installer — 技能安装与管理
直接从 OpenClaw clawhub-CZ8cBKOU.js 翻译

流程: search → download → extract → validate SKILL.md → security scan → install
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from executor.clawhub import (
    download_skill,
    get_security_verdicts,
    get_skill_detail,
    search_skills,
)


VALID_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", re.IGNORECASE)
SKILL_ROOT_MARKERS = ["SKILL.md", "skill.md", "skills.md", "SKILL.MD"]
DOT_DIR = ".clawhub"
ORIGIN_FILE = f"{DOT_DIR}/origin.json"
LOCK_FILE = f"{DOT_DIR}/lock.json"
SKILL_CARD_FILE = "skill-card.md"


class SkillInstallError(Exception):
    def __init__(self, message: str, kind: str = "invalid-request"):
        super().__init__(message)
        self.kind = kind


def _validate_slug(raw: str) -> str:
    slug = raw.strip()
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise SkillInstallError(f"Invalid skill slug: {raw}")
    if not VALID_SLUG_PATTERN.match(slug):
        raise SkillInstallError(f"Invalid skill slug: {raw}")
    return slug


def _resolve_install_dir(workspace: Path, slug: str) -> Path:
    target = workspace / "skills" / slug
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _has_skill_root(extract_dir: Path) -> bool:
    for marker in SKILL_ROOT_MARKERS:
        if (extract_dir / marker).exists():
            return True
    return False


def _read_lockfile(workspace: Path) -> dict:
    lock_path = workspace / LOCK_FILE
    try:
        raw = json.loads(lock_path.read_text())
        if raw.get("version") == 1 and isinstance(raw.get("skills"), dict):
            return raw
    except Exception:
        pass
    return {"version": 1, "skills": {}}


def _write_lockfile(workspace: Path, lock: dict) -> None:
    lock_path = workspace / LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")


def _write_origin(skill_dir: Path, origin: dict) -> None:
    origin_path = skill_dir / ORIGIN_FILE
    origin_path.parent.mkdir(parents=True, exist_ok=True)
    origin_path.write_text(json.dumps(origin, indent=2) + "\n")


def _extract_zip(archive_path: str, extract_to: Path) -> None:
    extract_to.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(extract_to)


def install_skill(
    slug: str,
    workspace: Path,
    version: Optional[str] = None,
    tag: Optional[str] = None,
    force: bool = False,
    token: Optional[str] = None,
) -> dict:
    """从 ClawHub 安装技能"""
    slug = _validate_slug(slug)
    target_dir = _resolve_install_dir(workspace, slug)

    mode = "update" if force else "install"
    if mode == "install" and target_dir.exists():
        return {"ok": False, "error": f"Skill {slug} already installed. Use force to reinstall."}

    # 1. 下载
    dl = download_skill(slug, version, tag, token)

    # 2. 解压
    with tempfile.TemporaryDirectory(prefix="nodus-skill-extract-") as tmp:
        extract_dir = Path(tmp)
        _extract_zip(dl["archive_path"], extract_dir)

        # 找 SKILL.md 所在根目录
        skill_root = extract_dir
        if not _has_skill_root(skill_root):
            # 可能多了一层目录
            subs = list(extract_dir.iterdir())
            if len(subs) == 1 and subs[0].is_dir():
                skill_root = subs[0]
        if not _has_skill_root(skill_root):
            return {"ok": False, "error": "archive is missing SKILL.md"}

        # 3. 安全扫描
        detail = get_skill_detail(slug, token)
        verdicts = get_security_verdicts(
            [{"slug": slug, "version": detail.get("version", "unknown")}], token
        )

        # 4. 安装
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(skill_root, target_dir)

        # 5. 记录 origin 和 lockfile
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        origin = {
            "version": 1,
            "registry": "https://clawhub.ai",
            "slug": slug,
            "installedVersion": detail.get("version", "0.0.0"),
            "installedAt": now,
        }
        _write_origin(target_dir, origin)

        lock = _read_lockfile(workspace)
        lock["skills"][slug] = {
            "version": detail.get("version", "0.0.0"),
            "installedAt": now,
            "registry": "https://clawhub.ai",
        }
        _write_lockfile(workspace, lock)

        # 6. 缓存 skill card
        card_path = target_dir / SKILL_CARD_FILE
        if not card_path.exists():
            card_path.write_text(detail.get("description", ""))

    # 清理临时文件
    try:
        Path(dl["archive_path"]).unlink()
    except Exception:
        pass

    return {
        "ok": True,
        "target_dir": str(target_dir),
        "version": detail.get("version", "0.0.0"),
    }


def list_installed(workspace: Path) -> dict:
    lock = _read_lockfile(workspace)
    return lock.get("skills", {})


def uninstall_skill(slug: str, workspace: Path) -> dict:
    slug = _validate_slug(slug)
    target_dir = _resolve_install_dir(workspace, slug)
    if not target_dir.exists():
        return {"ok": False, "error": f"Skill {slug} not found"}
    shutil.rmtree(target_dir)
    lock = _read_lockfile(workspace)
    lock["skills"].pop(slug, None)
    _write_lockfile(workspace, lock)
    return {"ok": True}
