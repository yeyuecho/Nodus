"""
Web 搜索 — 多引擎搜索 + 结果去重 + 代理支持
来源: Hermes tools/web_tools.py

支持的引擎:
- DuckDuckGo (免费，HTML 解析)
- Bing (需要 BING_API_KEY)
- Google (需要 GOOGLE_API_KEY + GOOGLE_CSE_ID)

功能:
- 多引擎并行搜索
- 结果去重（URL 归一化）
- HTTP/SOCKS 代理支持
- 页面内容抓取 + 纯文本提取
"""

import asyncio
import hashlib
import logging
import os
import re
from html.parser import HTMLParser
from typing import List, Optional
from dataclasses import dataclass
from urllib.parse import quote, urlparse, urlunparse, parse_qs

logger = logging.getLogger("qiyue.search")


# ═══════════════════════════════════════════
# 类型
# ═══════════════════════════════════════════

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str = ""        # 来源引擎
    relevance: float = 0.0  # 相关性评分


# ═══════════════════════════════════════════
# URL 归一化（去重用）
# ═══════════════════════════════════════════

def normalize_url(url: str) -> str:
    """
    URL 归一化

    处理:
    - 移除 fragment (#...)
    - 统一 scheme 大小写
    - 移除 www 前缀
    - 排序查询参数
    """
    try:
        parsed = urlparse(url)

        # 移除 fragment
        parsed = parsed._replace(fragment="")

        # 统一 scheme
        scheme = parsed.scheme.lower()

        # 移除 www 前缀
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]

        # 排序查询参数
        if parsed.query:
            params = parse_qs(parsed.query, keep_blank_values=True)
            sorted_params = sorted(params.items())
            query = "&".join(
                f"{k}={v[0]}" for k, v in sorted_params if v
            )
        else:
            query = ""

        return urlunparse((scheme, netloc, parsed.path, parsed.params, query, ""))

    except Exception:
        return url.lower().strip()


def url_hash(url: str) -> str:
    """URL 哈希（用于快速去重）"""
    normalized = normalize_url(url)
    return hashlib.md5(normalized.encode()).hexdigest()


# ═══════════════════════════════════════════
# 结果去重器
# ═══════════════════════════════════════════

class ResultDeduplicator:
    """搜索结果去重"""

    def __init__(self):
        self._seen_urls: set = set()
        self._seen_hashes: set = set()

    def is_duplicate(self, url: str) -> bool:
        """检查 URL 是否已出现过"""
        normalized = normalize_url(url)
        url_h = url_hash(url)

        if url_h in self._seen_hashes or normalized in self._seen_urls:
            return True

        self._seen_urls.add(normalized)
        self._seen_hashes.add(url_h)
        return False

    def filter(self, results: List[SearchResult]) -> List[SearchResult]:
        """过滤重复结果"""
        unique = []
        for r in results:
            if not self.is_duplicate(r.url):
                unique.append(r)
        return unique


# ═══════════════════════════════════════════
# 代理管理器
# ═══════════════════════════════════════════

class ProxyManager:
    """HTTP/SOCKS 代理配置"""

    @staticmethod
    def get_proxy_config() -> Optional[str]:
        """
        获取代理配置

        优先级:
        1. HTTP_PROXY / HTTPS_PROXY 环境变量
        2. ALL_PROXY 环境变量
        3. SEARCH_PROXY 环境变量（自定义）
        """
        for var in ("SEARCH_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            proxy = os.getenv(var)
            if proxy:
                return proxy
        return None

    @staticmethod
    def get_proxies_dict() -> Optional[dict]:
        """获取 httpx 格式的代理字典"""
        proxy = ProxyManager.get_proxy_config()
        if proxy:
            return {"http://": proxy, "https://": proxy}
        return None


# ═══════════════════════════════════════════
# DuckDuckGo 搜索
# ═══════════════════════════════════════════

class DuckDuckGoSearcher:
    """DuckDuckGo HTML 搜索"""

    BASE_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, http_client):
        self._http = http_client

    async def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """执行 DuckDuckGo 搜索"""
        url = f"{self.BASE_URL}?q={quote(query)}"

        try:
            resp = await self._http.get(url)
            resp.raise_for_status()

            parser = _DDGParser()
            parser.feed(resp.text)

            results = []
            for item in parser.results[:max_results]:
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    engine="duckduckgo",
                ))

            return results

        except Exception as e:
            logger.warning(f"[Search] DuckDuckGo error: {e}")
            return []


class _DDGParser(HTMLParser):
    """DuckDuckGo HTML 解析器"""

    def __init__(self):
        super().__init__()
        self.results = []
        self._current = {}
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._text_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")

        if tag == "div" and "result" in cls:
            self._in_result = True
            self._current = {}
        elif self._in_result and tag == "a" and "result__a" in cls:
            self._in_title = True
            self._current["url"] = attrs.get("href", "")
        elif self._in_result and tag == "a" and "result__snippet" in cls:
            self._in_snippet = True

    def handle_data(self, data):
        if self._in_title:
            self._current["title"] = self._current.get("title", "") + data.strip()
        elif self._in_snippet:
            self._current["snippet"] = self._current.get("snippet", "") + data.strip()

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            self._in_title = False
        elif tag == "a" and self._in_snippet:
            self._in_snippet = False
        elif tag == "div" and self._in_result:
            self._in_result = False
            if self._current.get("title"):
                self.results.append(self._current)


# ═══════════════════════════════════════════
# Bing 搜索
# ═══════════════════════════════════════════

class BingSearcher:
    """Bing Web Search API V7"""

    BASE_URL = "https://api.bing.microsoft.com/v7.0/search"

    def __init__(self, api_key: str, http_client):
        self.api_key = api_key
        self._http = http_client

    async def search(self, query: str, max_results: int = 10,
                     market: str = "zh-CN") -> List[SearchResult]:
        """执行 Bing 搜索"""
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
        }
        params = {
            "q": query,
            "count": min(max_results, 50),
            "mkt": market,
            "textFormat": "Raw",
        }

        try:
            resp = await self._http.get(self.BASE_URL, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("webPages", {}).get("value", []):
                results.append(SearchResult(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    engine="bing",
                ))

            return results[:max_results]

        except Exception as e:
            logger.warning(f"[Search] Bing error: {e}")
            return []


# ═══════════════════════════════════════════
# Google 搜索（Custom Search JSON API）
# ═══════════════════════════════════════════

class GoogleSearcher:
    """Google Custom Search JSON API"""

    BASE_URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cse_id: str, http_client):
        self.api_key = api_key
        self.cse_id = cse_id
        self._http = http_client

    async def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """执行 Google 搜索"""
        params = {
            "key": self.api_key,
            "cx": self.cse_id,
            "q": query,
            "num": min(max_results, 10),
        }

        try:
            resp = await self._http.get(self.BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("items", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    engine="google",
                ))

            return results[:max_results]

        except Exception as e:
            logger.warning(f"[Search] Google error: {e}")
            return []


# ═══════════════════════════════════════════
# Web 搜索引擎（主入口）
# ═══════════════════════════════════════════

class WebSearch:
    """Web 搜索引擎（多引擎 + 去重 + 代理）"""

    def __init__(self,
                 engine: str = "duckduckgo",
                 bing_api_key: str = None,
                 google_api_key: str = None,
                 google_cse_id: str = None,
                 proxy: str = None):
        self.engine = engine
        self.proxy = proxy

        # API keys
        self.bing_api_key = bing_api_key or os.getenv("BING_API_KEY", "")
        self.google_api_key = google_api_key or os.getenv("GOOGLE_API_KEY", "")
        self.google_cse_id = google_cse_id or os.getenv("GOOGLE_CSE_ID", "")

        import httpx
        self._client = httpx.AsyncClient(
            timeout=30.0,
            proxy=self.proxy,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/json",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

        # 引擎实例
        self._ddg = DuckDuckGoSearcher(self._client)
        self._bing = BingSearcher(self.bing_api_key, self._client) if self.bing_api_key else None
        self._google = GoogleSearcher(self.google_api_key, self.google_cse_id, self._client) if self.google_api_key else None

        # 统计
        self.stats = {
            "searches_performed": 0,
            "results_total": 0,
            "results_deduped": 0,
            "errors": 0,
        }

    # ─── 搜索 ───

    async def search(self, query: str, max_results: int = 5,
                     engines: List[str] = None,
                     deduplicate: bool = True) -> List[SearchResult]:
        """
        执行 Web 搜索

        参数:
            query: 搜索关键词
            max_results: 最大结果数
            engines: 引擎列表 ["duckduckgo", "bing", "google"]，None=使用默认引擎
            deduplicate: 是否去重

        返回: SearchResult 列表
        """
        self.stats["searches_performed"] += 1

        engines = engines or [self.engine]
        tasks = []

        for engine_name in engines:
            if engine_name == "duckduckgo":
                tasks.append(self._ddg.search(query, max_results))
            elif engine_name == "bing" and self._bing:
                tasks.append(self._bing.search(query, max_results))
            elif engine_name == "google" and self._google:
                tasks.append(self._google.search(query, max_results))

        if not tasks:
            logger.warning(f"[Search] No engines available for query: {query[:50]}")
            return []

        # 并行搜索
        try:
            results_lists = await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"[Search] Parallel search failed: {e}")
            self.stats["errors"] += 1
            return []

        # 合并结果
        all_results = []
        for result in results_lists:
            if isinstance(result, list):
                all_results.extend(result)
            elif isinstance(result, Exception):
                self.stats["errors"] += 1
                logger.warning(f"[Search] Engine error: {result}")

        # 去重
        if deduplicate:
            dedup = ResultDeduplicator()
            all_results = dedup.filter(all_results)
            self.stats["results_deduped"] += (
                sum(len(r) if isinstance(r, list) else 0 for r in results_lists) -
                len(all_results)
            )

        # 按引擎优先级排序 + 截断
        engine_order = {e: i for i, e in enumerate(engines)}
        all_results.sort(key=lambda r: (engine_order.get(r.engine, 99), -len(r.snippet)))

        self.stats["results_total"] += len(all_results)
        return all_results[:max_results]

    async def search_single(self, query: str, engine: str = "duckduckgo",
                            max_results: int = 10) -> List[SearchResult]:
        """单引擎搜索"""
        return await self.search(query, max_results=max_results, engines=[engine])

    async def search_all(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """所有可用引擎并行搜索"""
        available = ["duckduckgo"]
        if self._bing:
            available.append("bing")
        if self._google:
            available.append("google")
        return await self.search(query, max_results=max_results, engines=available)

    # ─── 页面抓取 ───

    async def fetch(self, url: str, max_size: int = 5 * 1024 * 1024) -> str:
        """抓取页面原始 HTML"""
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            content = resp.text
            if len(content) > max_size:
                content = content[:max_size]
                logger.debug(f"[Search] Fetch truncated to {max_size} bytes")
            return content
        except Exception as e:
            logger.warning(f"[Search] Fetch error for {url[:60]}: {e}")
            return ""

    async def fetch_text(self, url: str, max_size: int = 500 * 1024) -> str:
        """抓取页面并提取纯文本"""
        html = await self.fetch(url, max_size)
        if not html:
            return ""
        return _html_to_text(html)

    async def fetch_json(self, url: str) -> Optional[dict]:
        """抓取 JSON API"""
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[Search] JSON fetch error: {e}")
            return None

    # ─── 资源管理 ───

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


# ═══════════════════════════════════════════
# HTML → 纯文本
# ═══════════════════════════════════════════

def _html_to_text(html: str) -> str:
    """HTML → 纯文本提取"""
    import re as _re

    # 移除 script/style
    html = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<style[^>]*>.*?</style>', '', html, flags=_re.DOTALL | _re.IGNORECASE)
    html = _re.sub(r'<noscript[^>]*>.*?</noscript>', '', html, flags=_re.DOTALL | _re.IGNORECASE)

    # 移除 HTML 标签
    text = _re.sub(r'<[^>]+>', ' ', html)

    # 解码 HTML 实体
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'")
    text = text.replace('&nbsp;', ' ')

    # 规范化空白
    text = _re.sub(r'\s+', ' ', text)
    text = _re.sub(r'\n\s*\n', '\n\n', text)

    return text.strip()


# ─── 使用示例 ───
async def _demo():
    ws = WebSearch()
    results = await ws.search("Python asyncio tutorial", max_results=3)
    for r in results:
        print(f"  [{r.engine}] {r.title}")
        print(f"  {r.url}")
        print(f"  {r.snippet[:120]}...\n")
    await ws.close()

if __name__ == "__main__":
    asyncio.run(_demo())
