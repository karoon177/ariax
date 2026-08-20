# -*- coding: utf-8 -*-
"""
M3/M7 (funds transfer): Bybit v5 inter-transfer endpoints.

AriaX uses a UNIFIED wallet (UTA): spot and derivatives share one
physical USDT balance, so an inter-account transfer is a bookkeeping
operation (ledger row + idempotent transferId registry) rather than a
balance move — exactly how unified accounts behave on Bybit.

Endpoints (Bybit v5 compatible):
  POST /v5/asset/transfer/inter-transfer          — transfer between
        SPOT / CONTRACT / UNIFIED / FUND account types
  GET  /v5/asset/transfer/query-account-coins-balance — per-type balance view
  GET  /v5/asset/transfer/query-inter-transfer-list   — transfer history
  GET  /v5/account/transferable-amount               — transferable funds
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, Request

from .. import config, util
from ..api import deps
from ..errors import ApiError, E_PARAM
from ..runtime import get_persister
from ..state import STATE

router = APIRouter(prefix="/v5", tags=["v5-transfer"])

ACCOUNT_TYPES = ("UNIFIED", "FUND", "CONTRACT", "SPOT")

# transferId -> {"status": "SUCCESS", "ts": ms}  (idempotency registry)
_TRANSFER_IDS: dict[str, dict] = {}
# recent transfers for query-inter-transfer-list (uid -> deque)
_TRANSFER_LOG: dict[int, deque] = {}


async def _json(request: Request) -> dict:
    try:
        import json as _json
        return _json.loads((await request.body()).decode() or "{}")
    except Exception:
        return {}


def _ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result,
            "retExtInfo": {}, "time": util.now_ms()}


def _record_transfer(uid: int, transfer_id: str, coin: str, amount: float,
                     frm: str, to: str) -> None:
    """Persist the bookkeeping row and in-memory registry."""
    from ..engine import matching
    _TRANSFER_IDS[transfer_id] = {"status": "SUCCESS", "ts": util.now_ms()}
    log = _TRANSFER_LOG.setdefault(uid, deque(maxlen=200))
    log.appendleft(dict(transferId=transfer_id, coin=coin, amount=amount,
                        fromAccountType=frm, toAccountType=to,
                        timestamp=util.now_ms(), status="SUCCESS"))
    # unified wallet: balances unchanged; ledger records the intent
    matching.ledger(uid, "transfer", coin, 0.0,
                    f"{frm}→{to} {amount:g} {coin} (UTA unified wallet, "
                    f"no balance move required) transferId={transfer_id}")


@router.post("/asset/transfer/inter-transfer")
async def inter_transfer(request: Request):
    """Transfer funds between account types (Bybit v5 semantics).

    Because the wallet is UNIFIED, the transfer always succeeds and does
    not change spendable balances; it is recorded for history/auditing
    and for compatibility with bots that pre-fund a CONTRACT wallet.
    """
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    transfer_id = (b.get("transferId") or "").strip()
    coin = (b.get("coin") or "USDT").upper()
    frm = (b.get("fromAccountType") or "").upper()
    to = (b.get("toAccountType") or "").upper()
    try:
        amount = float(b.get("amount", 0))
    except (TypeError, ValueError):
        raise ApiError(E_PARAM, "invalid amount")
    if not transfer_id:
        transfer_id = util.gen_hex(12)
    if len(transfer_id) > 64:
        raise ApiError(E_PARAM, "transferId too long (max 64)")
    if coin not in config.LISTED_ASSETS:
        raise ApiError(E_PARAM, f"unsupported coin {coin}")
    if frm not in ACCOUNT_TYPES or to not in ACCOUNT_TYPES:
        raise ApiError(E_PARAM,
                       f"account types must be one of {ACCOUNT_TYPES}")
    if amount <= 0:
        raise ApiError(E_PARAM, "amount must be positive")
    acct = STATE.account(rec["uid"])
    if acct.available(coin) < amount:
        raise ApiError(110007, f"insufficient {coin} balance")
    # idempotent: repeated transferId returns the same success
    if transfer_id not in _TRANSFER_IDS:
        _record_transfer(rec["uid"], transfer_id, coin, amount, frm, to)
    return _ok({"transferId": transfer_id, "status": "SUCCESS"})


@router.get("/asset/transfer/query-account-coins-balance")
async def query_account_coins(request: Request, accountType: str = "UNIFIED",
                              coin: str | None = None):
    """Per-account-type balance view (all types see the unified wallet)."""
    rec = await deps.verify_v5_signature(request)
    acct = STATE.account(rec["uid"])
    accountType = accountType.upper()
    if accountType not in ACCOUNT_TYPES:
        raise ApiError(E_PARAM, f"accountType must be one of {ACCOUNT_TYPES}")
    coins = []
    for asset in config.LISTED_ASSETS:
        if coin and asset != coin.upper():
            continue
        free = acct.free(asset)
        if free <= 0 and asset != "USDT":
            continue
        coins.append({
            "coin": asset,
            "transferBalance": f"{free:.8f}",
            "walletBalance": f"{free:.8f}",
            "bonus": "0",
        })
    return _ok({
        "accountType": accountType,
        "accountId": f"ariax-unified-{rec['uid']}",
        "list": coins,
    })


@router.get("/asset/transfer/query-inter-transfer-list")
async def query_inter_transfer_list(request: Request,
                                    coin: str | None = None,
                                    limit: int = 20,
                                    cursor: str | None = None):
    """Transfer history for the authenticated user."""
    rec = await deps.verify_v5_signature(request)
    rows = [t for t in _TRANSFER_LOG.get(rec["uid"], ())
            if not coin or t["coin"] == coin.upper()]
    limit = max(1, min(limit, 50))
    start = 0
    if cursor and cursor.isdigit():
        start = int(cursor)
    page = rows[start:start + limit]
    next_cursor = str(start + limit) if start + limit < len(rows) else ""
    return _ok({"list": page, "nextPageCursor": next_cursor})


@router.get("/account/transferable-amount")
async def transferable_amount(request: Request, accountType: str = "UNIFIED",
                              coin: str = "USDT"):
    """Max transferable = available (free minus holds) balance."""
    rec = await deps.verify_v5_signature(request)
    accountType = accountType.upper()
    if accountType not in ACCOUNT_TYPES:
        raise ApiError(E_PARAM, f"accountType must be one of {ACCOUNT_TYPES}")
    acct = STATE.account(rec["uid"])
    available = acct.available(coin.upper())
    return _ok({"transferableAmount": f"{max(0.0, available):.8f}"})
