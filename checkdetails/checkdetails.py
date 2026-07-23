# ============================================
# 🚀 TON Full Transaction API Server
# ============================================

import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import aiohttp
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
import uvicorn

# ============================================
# 🔧 CONFIG
# ============================================

TONCENTER_API_URL = "https://toncenter.com/api/v3/transactions"

API_KEY = os.getenv("API_KEY")  # Optional

BATCH_LIMIT = 100
REQUEST_DELAY = 0.15
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30

# ============================================
# 🚀 FASTAPI APP
# ============================================

app = FastAPI(
    title="TON Transaction API",
    version="2.0.0"
)

# ============================================
# 💰 NanoTON → TON
# ============================================

def nano_to_ton(value) -> float:
    try:
        return round(int(value) / 1_000_000_000, 9)
    except:
        return 0.0

# ============================================
# 💬 Extract Comment
# ============================================

def extract_comment(msg: Optional[Dict]) -> Optional[str]:
    try:
        if not msg:
            return None

        content = msg.get("message_content")

        if content and "decoded" in content:
            decoded = content["decoded"]

            if decoded and decoded.get("@type") == "text_comment":
                return decoded.get("comment")

        return None

    except:
        return None

# ============================================
# 🔄 Transform Transaction
# ============================================

def transform_transaction(tx: Dict[str, Any]) -> Dict[str, Any]:
    try:
        timestamp = tx.get("now") or tx.get("utime")

        tx_date = None

        if timestamp:
            tx_date = datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            ).isoformat()

        # ====================================
        # INCOMING
        # ====================================

        incoming = None
        in_msg = tx.get("in_msg")

        if in_msg:
            value = int(in_msg.get("value", 0))

            incoming = {
                "from": in_msg.get("source"),
                "to": in_msg.get("destination"),
                "amount_ton": nano_to_ton(value),
                "memo": extract_comment(in_msg),
                "status": "success" if value > 0 else "failed"
            }

        # ====================================
        # OUTGOING
        # ====================================

        outgoing = []

        for msg in tx.get("out_msgs", []):
            value = int(msg.get("value", 0))

            outgoing.append({
                "from": msg.get("source"),
                "to": msg.get("destination"),
                "amount_ton": nano_to_ton(value),
                "memo": extract_comment(msg),
                "status": "success" if value > 0 else "failed"
            })

        # ====================================
        # FINAL
        # ====================================

        return {
            "lt": str(tx.get("lt")),
            "hash": tx.get("hash"),
            "date": tx_date,
            "incoming": incoming,
            "outgoing": outgoing
        }

    except Exception as e:
        return {
            "error": str(e),
            "raw_transaction": tx
        }

# ============================================
# 📅 TODAY FILTER
# ============================================

def is_today(date_string: str) -> bool:
    try:
        tx_date = datetime.fromisoformat(
            date_string.replace("Z", "+00:00")
        ).date()

        today = datetime.now(timezone.utc).date()

        return tx_date == today

    except:
        return False

# ============================================
# 🌐 FETCH ONE BATCH
# ============================================

async def fetch_batch(
    session: aiohttp.ClientSession,
    wallet: str,
    offset: int
):

    params = {
        "account": wallet,
        "limit": BATCH_LIMIT,
        "offset": offset,
        "sort": "desc"
    }

    for attempt in range(MAX_RETRIES):

        try:
            async with session.get(
                TONCENTER_API_URL,
                params=params,
                timeout=REQUEST_TIMEOUT
            ) as response:

                if response.status != 200:
                    error_text = await response.text()

                    return {
                        "success": False,
                        "status_code": response.status,
                        "error": error_text,
                        "transactions": []
                    }

                data = await response.json()

                transactions = data.get("transactions", [])

                return {
                    "success": True,
                    "status_code": response.status,
                    "transactions": transactions
                }

        except asyncio.TimeoutError:

            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 408,
                    "error": "Request timeout",
                    "transactions": []
                }

        except aiohttp.ClientError as e:

            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 503,
                    "error": str(e),
                    "transactions": []
                }

        except Exception as e:

            if attempt == MAX_RETRIES - 1:
                return {
                    "success": False,
                    "status_code": 500,
                    "error": str(e),
                    "transactions": []
                }

        await asyncio.sleep(1)

# ============================================
# 📦 FETCH ALL TRANSACTIONS
# ============================================

async def fetch_all_transactions(wallet: str):

    headers = {}

    if API_KEY:
        headers["X-API-Key"] = API_KEY

    timeout = aiohttp.ClientTimeout(total=None)

    all_transactions = []
    errors = []

    offset = 0

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        while True:

            result = await fetch_batch(
                session,
                wallet,
                offset
            )

            if not result["success"]:

                errors.append({
                    "offset": offset,
                    "status_code": result.get("status_code"),
                    "error": result.get("error")
                })

                break

            batch_transactions = result["transactions"]

            if not batch_transactions:
                break

            transformed = [
                transform_transaction(tx)
                for tx in batch_transactions
            ]

            all_transactions.extend(transformed)

            offset += BATCH_LIMIT

            if len(batch_transactions) < BATCH_LIMIT:
                break

            await asyncio.sleep(REQUEST_DELAY)

    return {
        "transactions": all_transactions,
        "errors": errors
    }

# ============================================
# 🏠 HOME ROUTE
# ============================================

@app.get("/")
async def home():

    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "TON Transaction API Running",
            "routes": {
                "all_transactions":
                "/check_transaction/WALLET?status=all",

                "today_transactions":
                "/check_transaction/WALLET?status=today"
            }
        }
    )

# ============================================
# 🚀 MAIN ROUTE
# ============================================

@app.get("/check_transaction/{wallet}")
async def check_transaction(
    wallet: str,
    status: str = Query(
        "all",
        description="all or today"
    )
):

    try:

        status = status.lower()

        if status not in ["all", "today"]:

            return JSONResponse(
                status_code=400,
                content={
                    "success": False,
                    "status_code": 400,
                    "error": "Invalid status. Use all or today"
                }
            )

        result = await fetch_all_transactions(wallet)

        transactions = result["transactions"]
        errors = result["errors"]

        # ====================================
        # FILTER TODAY
        # ====================================

        if status == "today":

            transactions = [
                tx for tx in transactions
                if tx.get("date")
                and is_today(tx["date"])
            ]

        # ====================================
        # RESPONSE
        # ====================================

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "status_code": 200,
                "wallet": wallet,
                "filter": status,
                "server_time":
                datetime.now(timezone.utc).isoformat(),

                "total_transactions":
                len(transactions),

                "errors": errors,

                "transactions": transactions
            }
        )

    except Exception as e:

        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "status_code": 500,
                "error": str(e),
                "transactions": []
            }
        )

# ============================================
# ❌ 404 HANDLER
# ============================================

@app.exception_handler(404)
async def not_found_handler(
    request: Request,
    exc
):

    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "status_code": 404,
            "error": "Route not found"
        }
    )

# ============================================
# ❌ GLOBAL ERROR HANDLER
# ============================================

@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc
):

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "status_code": 500,
            "error": str(exc)
        }
    )

# ============================================
# ▶️ RUN SERVER
# ============================================

if __name__ == "__main__":

    uvicorn.run(
        "checkdetails:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False
    )
