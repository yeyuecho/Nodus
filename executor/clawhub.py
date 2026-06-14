"""
ClawHub Client — 技能生态客户端
直接从 OpenClaw clawhub-eHNqclD3.js 翻译，不改逻辑

API:
  search(query, limit)                → GET /api/v1/search
  get_skill(slug)                     → GET /api/v1/skills/{slug}
  get_skill_card(slug, version, tag)  → GET /api/v1/skills/{slug}/card
  download_skill(slug, version, tag)  → GET /api/v1/download
  get_security_verdicts(items)        → POST /api/v1/skills/-/security-verdicts
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError


DEFAULT_CLAWHUB_URL = "https://clawhub.ai"
DEFAULT_FETCH_TIMEOUT_S = 30


class ClawHubRequestError(Exception):
    def __init__(self, path: str, status: int, body: str):
        super().__init__(f"ClawHub {path} failed ({status}): {body}")
        self.status = status
        self.request_path = path
        self.response_body = body


def _normalize_base_url(base_url: Optional[str] = None) -> str:
    env = os.getenv("CLAWHUB_URL") or os.getenv("OPENCLAW_CLAWHUB_URL") or DEFAULT_CLAWHUB_URL
    return (base_url or env).rstrip("/") or DEFAULT_CLAWHUB_URL


def _resolve_auth_token() -> Optional[str]:
    token = os.getenv("CLAWHUB_TOKEN") or os.getenv("OPENCLAW_CLAWHUB_TOKEN") or os.getenv("CLAWHUB_AUTH_TOKEN")
    if token:
        return token
    # try config file
    config_home = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    config_path = Path(config_home) / "clawhub" / "config.json"
    try:
        cfg = json.loads(config_path.read_text())
        for key in ("accessToken", "authToken", "apiToken", "token"):
            if cfg.get(key):
                return cfg[key]
        for sub in ("auth", "session", "credentials", "user"):
            if isinstance(cfg.get(sub), dict):
                for key in ("accessToken", "authToken", "apiToken", "token"):
                    if cfg[sub].get(key):
                        return cfg[sub][key]
    except Exception:
        pass
    return None


def _clawhub_request(
    path: str,
    method: str = "GET",
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    timeout_s: int = DEFAULT_FETCH_TIMEOUT_S,
) -> tuple[Any, str, bool]:
    base = _normalize_base_url()
    url = urljoin(base + "/", path.lstrip("/"))
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urlencode(filtered)

    auth_token = token or _resolve_auth_token()
    has_token = bool(auth_token)

    headers = {"User-Agent": "Nodus/1.0"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    else:
        data = None

    req = Request(url, data=data, headers=headers, method=method)
    resp = urlopen(req, timeout=timeout_s)
    return resp, url, has_token


def _fetch_json(
    path: str,
    method: str = "GET",
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    timeout_s: int = DEFAULT_FETCH_TIMEOUT_S,
) -> Any:
    resp, url, has_token = _clawhub_request(path, method, params, body, token, timeout_s)
    try:
        return json.loads(resp.read())
    except HTTPError as e:
        msg = e.read().decode("utf-8", errors="replace").strip() or str(e)
        raise ClawHubRequestError(path, e.code, msg)
    except Exception as e:
        raise ClawHubRequestError(path, 0, str(e))


def search_skills(query: str, limit: Optional[int] = None, token: Optional[str] = None) -> list[dict]:
    result = _fetch_json(
        "/api/v1/search",
        params={"q": query, **({"limit": str(limit)} if limit else {})},
        token=token,
    )
    return result.get("results", [])


def get_skill_detail(slug: str, token: Optional[str] = None) -> dict:
    return _fetch_json(f"/api/v1/skills/{slug}", token=token)


def get_skill_card(slug: str, version: Optional[str] = None, tag: Optional[str] = None, token: Optional[str] = None) -> str:
    resp, url, has_token = _clawhub_request(
        f"/api/v1/skills/{slug}/card",
        params={k: v for k, v in {"version": version, "tag": tag}.items() if v},
        token=token,
    )
    return resp.read().decode("utf-8")


def download_skill(slug: str, version: Optional[str] = None, tag: Optional[str] = None, token: Optional[str] = None) -> dict:
    resp, url, has_token = _clawhub_request(
        "/api/v1/download",
        params={k: v for k, v in {"slug": slug, "version": version, "tag": tag}.items() if v},
        token=token,
    )
    data = resp.read()
    sha256 = hashlib.sha256(data).hexdigest()
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", prefix="nodus-skill-", delete=False)
    tmp.write(data)
    tmp.close()
    return {
        "archive_path": tmp.name,
        "sha256": sha256,
        "size": len(data),
    }


def get_security_verdicts(items: list[dict], token: Optional[str] = None) -> dict:
    return _fetch_json(
        "/api/v1/skills/-/security-verdicts",
        method="POST",
        body={"items": items},
        token=token,
    )
