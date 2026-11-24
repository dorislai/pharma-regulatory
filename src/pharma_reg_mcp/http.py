"""對外連線的小幫手：所有「向官方網站要資料」的動作都經過這裡。

這裡把結果分成三種，後面的程式才能誠實回報：
1. 成功            → 回傳內容
2. 來源明確說沒有  → 丟出 NotFound（例如查了不存在的條號）
3. 連不上或回應異常 → 丟出 SourceUnavailable（此時「不知道」答案，不能當作「沒有」）
"""

from __future__ import annotations

import json
import logging
import time

import httpx

from .config import HTTP_TIMEOUT_SECONDS, USER_AGENT

# 每一次對外連線都記一行（寫到 stderr），出問題時看得到「連了哪裡、花了多久」。
logger = logging.getLogger("pharma_reg_mcp.http")

# 這些狀態碼通常是暫時性的（太頻繁、伺服器忙），值得稍等再試一次。
_RETRYABLE = {429, 500, 502, 503, 504}


class SourceUnavailable(Exception):
    """連不上來源，或來源的回應不正常。"""


class NotFound(Exception):
    """來源明確回覆「沒有這筆資料」（HTTP 404）。"""


class HttpClient:
    """只做一件事：GET 一個網址，拿回原始內容（bytes）。"""

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS, retries: int = 2) -> None:
        self._timeout = timeout
        self._retries = retries
        # follow_redirects=False：被轉址（例如被導到驗證頁）就視為失敗，
        # 避免把「不是法規的網頁」誤當成法規內容。
        # httpx 預設會帶 Accept-Encoding（壓縮傳輸），eCFR 的全文端點要求必須如此。
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        )

    def get(self, url: str, timeout: float | None = None) -> bytes:
        last_error = "未知錯誤"
        for attempt in range(self._retries + 1):
            started = time.monotonic()
            try:
                response = self._client.get(url, timeout=timeout or self._timeout)
            except httpx.HTTPError as exc:
                last_error = f"連線失敗（{type(exc).__name__}）"
                logger.info("GET %s → %s（%.1fs）", url, last_error, time.monotonic() - started)
            else:
                logger.info(
                    "GET %s → HTTP %s（%.1fs）", url, response.status_code,
                    time.monotonic() - started,
                )
                if response.status_code == 200:
                    return response.content
                if response.status_code == 404:
                    raise NotFound(_error_message(response))
                last_error = f"來源回應 HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE:
                    break
            if attempt < self._retries:
                time.sleep(1.5 * (attempt + 1))
        raise SourceUnavailable(f"{last_error}：{url}")

    def close(self) -> None:
        self._client.close()


def _error_message(response: httpx.Response) -> str:
    """盡量取出來源自己寫的錯誤說明（eCFR 會回 {"error": "..."}）。"""
    try:
        payload = json.loads(response.text)
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
    except ValueError:
        pass
    return "來源回覆找不到這筆資料（HTTP 404）"
