"""
浏览器引擎 — Playwright (Python 官方绑定)
等价于 OpenClaw 的 CDP / Playwright 能力

依赖: pip install playwright && playwright install chromium
"""

import asyncio
from pathlib import Path
from typing import Optional


class BrowserEngine:
    """Playwright 浏览器引擎 — 等价于 OpenClaw 的 CDP 客户端"""

    def __init__(self, headless: bool = True, data_dir: str = "data/browser_profile"):
        self.headless = headless
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._browser = None
        self._context = None
        self._page = None

    async def launch(self):
        """启动浏览器（首次自动下载 Chromium）"""
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        # 反检测：隐藏 webdriver 标记
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = {runtime: {}};
        """)
        self._page = await self._context.new_page()
        return self

    async def navigate(self, url: str, timeout: int = 30_000) -> str:
        """导航到 URL，返回页面文本"""
        if not self._page:
            await self.launch()
        await self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return await self._page.content()

    async def get_text(self) -> str:
        """获取页面纯文本"""
        return await self._page.inner_text("body")

    async def click(self, selector: str, timeout: int = 5_000):
        """点击元素"""
        await self._page.click(selector, timeout=timeout)

    async def type(self, selector: str, text: str):
        """在输入框中输入文字"""
        await self._page.fill(selector, text)

    async def screenshot(self, path: Optional[str] = None) -> bytes:
        """截图"""
        return await self._page.screenshot(path=path, full_page=True)

    async def evaluate(self, js: str) -> any:
        """执行 JavaScript 并返回结果"""
        return await self._page.evaluate(js)

    async def console_logs(self) -> list[str]:
        """获取控制台日志（需要提前监听）"""
        return self._console_msgs if hasattr(self, "_console_msgs") else []

    async def close(self):
        """关闭浏览器"""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if hasattr(self, "_pw") and self._pw:
            await self._pw.stop()

    # ─── 快捷方法 ───

    async def fetch_page_text(self, url: str) -> str:
        """快速获取页面文本（一次性导航+提取+关闭）"""
        await self.navigate(url)
        text = await self.get_text()
        return text

    async def search_and_extract(self, url: str, extract_js: str) -> any:
        """导航到页面并执行 JS 提取"""
        await self.navigate(url)
        return await self.evaluate(extract_js)


# ─── 使用示例 ───
async def _demo():
    browser = BrowserEngine(headless=True)
    try:
        await browser.launch()
        await browser.navigate("https://www.baidu.com")
        print(await browser.get_text()[:500])
        await browser.screenshot("demo.png")
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(_demo())
