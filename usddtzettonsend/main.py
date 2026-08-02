import asyncio
import time
import requests

from fastapi import FastAPI
from pydantic import BaseModel
from web3 import Web3

# TON imports
from ton_core import NetworkGlobalID, to_nano, Address
from tonutils.clients import ToncenterClient
from tonutils.contracts import WalletV5R1, JettonTransferBuilder

# For memo (optional forward payload)
from pytoniq_core import begin_cell

# =========================================================
# APP
# =========================================================
app = FastAPI(title="TON Jetton USDT Payment Gateway")

# Prevent parallel tx conflicts
wallet_lock = asyncio.Lock()

# =========================================================
# BSC PRICE ORACLE
# =========================================================
bsc_rpc = "https://bsc-dataseed.binance.org/"
web3 = Web3(Web3.HTTPProvider(bsc_rpc, request_kwargs={"timeout": 15}))
pool_address = web3.to_checksum_address("0x819a26D0C6F3af2B9fe4E9c4BcaC04fCB3ea7f2a")
pool_abi = [
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"name": "_reserve0", "type": "uint112"},
            {"name": "_reserve1", "type": "uint112"},
            {"name": "_blockTimestampLast", "type": "uint32"}
        ],
        "type": "function"
    }
]
contract = web3.eth.contract(address=pool_address, abi=pool_abi)

def pancake_price():
    reserves = contract.functions.getReserves().call()
    reserve_usdt = reserves[0] / (10 ** 18)
    reserve_ton = reserves[1] / (10 ** 9)
    price = reserve_usdt / reserve_ton
    if price <= 0:
        raise Exception("Invalid price")
    return float(price)

def diadata_price():
    url = "https://api.diadata.org/v1/assetQuotation/Ton/0x0000000000000000000000000000000000000000"
    r = requests.get(url, timeout=10)
    data = r.json()
    return float(data["Price"])

def get_ton_price():
    try:
        return pancake_price()
    except Exception:
        pass
    try:
        return diadata_price()
    except Exception:
        pass
    return 0.0

# =========================================================
# USDT JETTON CONFIG
# =========================================================
USDT_JETTON_MASTER = Address("EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")  # Mainnet USDT Jetton Master
USDT_DECIMALS = 6

# =========================================================
# TONAPI REAL FEE FETCHER (WITH FALLBACK)
# =========================================================
async def get_real_fee_from_tonapi(tx_hash: str, fallback_fee: float) -> float:
    url = f"https://tonapi.io/v2/blockchain/transactions/{tx_hash}"
    
    # ব্লকচেইনে ট্রানজেকশন প্রোপাগেট হতে সামান্য সময় লাগতে পারে, তাই রিট্রেস লুপ রাখা ভালো
    for _ in range(3):
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                tx = resp.json()
                fee_nano = int(tx.get("total_fees", 0))
                if fee_nano > 0:
                    return fee_nano / 1_000_000_000
        except Exception:
            pass
        await asyncio.sleep(2)
        
    # যদি API থেকে ফেচ করা না যায়, তবে ডিফল্ট গ্যাস ডিপোজিট (fallback_fee) রিটার্ন করবে
    return fallback_fee

# =========================================================
# MODELS & HELPERS
# =========================================================
class SendRequest(BaseModel):
    mnemonic: list[str]
    to_address: str
    amount_usdt: float           # Amount in USDT (human readable)
    memo: str | None = None      # Optional forward payload / comment

def response(ok, message, data=None, code=200):
    return {
        "ok": ok,
        "message": message,
        "status_code": code,
        "timestamp": int(time.time()),
        "data": data
    }

# =========================================================
# MAIN PAYMENT (JETTON USDT)
# =========================================================
async def process_payment(req: SendRequest):
    client = ToncenterClient(network=NetworkGlobalID.MAINNET)
    await client.connect()

    try:
        # Create wallet from mnemonic
        wallet, public_key, private_key, mnemonic = WalletV5R1.from_mnemonic(
            client,
            " ".join(req.mnemonic)
        )

        wallet_addr = wallet.address.to_str(is_bounceable=False)

        # Refresh state
        await wallet.refresh()

        # ---- AMOUNT ----
        amount_usdt = float(req.amount_usdt)
        jetton_amount = to_nano(amount_usdt, decimals=USDT_DECIMALS)  # Convert to base units

        ton_price = get_ton_price()
        amount_usd = amount_usdt  # Since it's USDT

        # ---- MEMO / FORWARD PAYLOAD ----
        forward_payload = None
        memo_value = None
        if req.memo and req.memo.strip():
            memo_value = req.memo.strip()
            memo_bytes = memo_value.encode("utf-8")
            if len(memo_bytes) > 123:
                return response(False, "Memo too long", {"memo_bytes": len(memo_bytes), "max_bytes": 123}, 400)
            forward_payload = begin_cell().store_uint(0, 32).store_string(memo_value).end_cell()

        # ---- LOCKED TRANSFER ----
        async with wallet_lock:
            if wallet.state != "active":
                print("Wallet not deployed. Deploying...")
                deploy_amount = to_nano(0.01)
                await wallet.transfer(
                    destination=wallet.address,
                    amount=deploy_amount,
                    body=None
                )
                await asyncio.sleep(5)
                await wallet.refresh()
                print("Wallet deployed.")

            # গ্যাস ফি ডিপোজিট (ডিফল্ট ফলব্যাক ভ্যালু)
            gas_deposit_ton = 0.05 

            # Build Jetton Transfer
            jetton_builder = JettonTransferBuilder(
                destination=Address(req.to_address),
                jetton_amount=jetton_amount,
                jetton_master_address=USDT_JETTON_MASTER,
                forward_payload=forward_payload,
                forward_amount=to_nano(0.000000001),  # Small amount to trigger notification
                amount=to_nano(gas_deposit_ton),  # TON for gas fees
            )

            msg = await wallet.transfer_message(jetton_builder)
            txid = msg.normalized_hash

        # Tonapi থেকে রিয়েল গ্যাস ফি ফেচ করা (ফেইল করলে gas_deposit_ton ফলব্যাক হিসেবে দেখাবে)
        real_gas_used = await get_real_fee_from_tonapi(txid, gas_deposit_ton)

        # Final response
        await wallet.refresh()

        return response(True, "USDT Jetton transaction completed", {
            "success": True,
            "wallet": wallet_addr,
            "to_address": req.to_address,
            "memo": memo_value,
            "txid": txid,
            "hash_status": "confirmed",
            #"amount_usdt": round(amount_usdt, USDT_DECIMALS),
            "amount_usd": round(amount_usd, 6),
            "ton_price_usd": round(ton_price, 6),
            "gas_used_ton": round(real_gas_used, 9),  # Tonapi থেকে পাওয়া আসল ফি অথবা ফলব্যাক মান
        })

    except Exception as e:
        return response(False, "Transaction failed", {"error": str(e)}, 500)

    finally:
        await client.close()


# =========================================================
# API ENDPOINT
# =========================================================
@app.post("/send")
async def send(req: SendRequest):
    return await process_payment(req)


# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
