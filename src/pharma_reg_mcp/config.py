"""集中放設定：資料存哪裡、連線時怎麼自我介紹、最多等幾秒。"""

from __future__ import annotations

import os
from pathlib import Path

# 向官方網站發出請求時的自我介紹。只寫工具名稱，不放任何個人或單位資訊。
USER_AGENT = "pharma-reg-mcp/0.1 (public regulation lookup prototype)"

# 一般查詢最多等 25 秒（逾時會自動重試）；下載整包法規檔（數十 MB）最多等 180 秒。
HTTP_TIMEOUT_SECONDS = 25.0
BULK_TIMEOUT_SECONDS = 180.0

# 台灣法規：查條文或搜尋時，如果 10 分鐘內才向官網確認過修正日期，就不重複確認
# （回傳裡會如實寫出上次確認的時間）。check_updates 不受此限，永遠即時查。
TW_RECHECK_AFTER_SECONDS = 600


def data_dir() -> Path:
    """本機資料夾的位置。

    預設是家目錄下的 `.pharma-reg-mcp`；
    想換地方（例如測試時用暫存資料夾）就設定環境變數 `PHARMA_REG_MCP_HOME`。
    """
    raw = os.environ.get("PHARMA_REG_MCP_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".pharma-reg-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """SQLite 資料庫檔案的位置。"""
    return data_dir() / "regulations.db"
