"""
Pre-ACK module.
"""

import logging, os, time
logger = logging.getLogger(__name__)

SYSTEM = "Reply in Chinese with ONE short natural acknowledgment, max 15 chars."
MODEL = os.environ.get("PRE_ACK_MODEL", "deepseek-chat")
URL = os.environ.get("PRE_ACK_BASE_URL", "https://api.deepseek.com/v1")
TOKENS = 30
TIMEOUT = 10
ENABLED = os.environ.get("PRE_ACK_ENABLED", "true").lower() == "true"

async def send_pre_ack(msg, adapter, chat_id, metadata=None):
    if not ENABLED or not msg or not os.environ.get("DEEPSEEK_API_KEY"):
        return True
    try:
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(URL + "/chat/completions", json={
                "model": MODEL, "temperature": 0.7, "max_tokens": TOKENS,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": msg[:500]}]
            }, headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
                        "Content-Type": "application/json"})
        if r.status_code == 200:
            txt = r.json()["choices"][0]["message"]["content"].strip()
            if txt:
                logger.info("pre_ack: %r (%ss)", txt[:60], time.monotonic())
                await adapter.send(chat_id=chat_id, content=txt, metadata=metadata)
    except Exception:
        logger.debug("pre_ack error", exc_info=True)
    return True
