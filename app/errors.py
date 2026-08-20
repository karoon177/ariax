# -*- coding: utf-8 -*-
"""
M11 (error management): Bybit v5-compatible error catalogue.

Signed/REST responses always use the Bybit envelope:
    {"retCode": <int>, "retMsg": "<msg>", "result": {...}, "retExtInfo": {}, "time": <ms>}

The codes below mirror Bybit v5 documented codes wherever a direct
equivalent exists; `retMsg` is kept in English (bot-friendly logs).
"""
from __future__ import annotations


class ApiError(Exception):
    """Raise inside handlers to emit a Bybit-style error envelope."""

    def __init__(self, ret_code: int, ret_msg: str, http_status: int = 200):
        super().__init__(ret_msg)
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.http_status = http_status


# Frequently used Bybit v5 error codes
OK = 0
E_PARAM = 10001              # invalid or missing parameter
E_TIMESTAMP = 10002          # request timestamp expired
E_KEY_INVALID = 10003        # API key invalid / not found
E_RATE_LIMIT = 10006         # rate limit exceeded (HTTP 429)
E_PERMISSION = 10007         # API key lacks required permission
E_IP_MISMATCH = 10010        # request IP not in key whitelist
E_INTERNAL = 10016           # internal server error
E_NOT_FOUND = 10017          # route / resource not found
E_SYMBOL_INVALID = 10019     # symbol not found for category
E_INSUFFICIENT_BALANCE = 110007
E_INVALID_SIDE = 110013
E_PRICE_DEVIATION = 110014
E_INVALID_QTY = 110017
E_ORDER_NOT_FOUND = 110043
E_DUPLICATE_LINK_ID = 110072
E_POSITION_ZERO = 110131     # position already closed / size zero
E_LEVERAGE_NOT_MODIFIED = 110045
E_QTY_EXCEEDS_POSITION = 110126
E_LIQUIDATION = 110125       # position in liquidation
E_AUTH_REQUIRED = 10003


def bybit_envelope(result=None, ret_code: int = OK, ret_msg: str = "OK",
                   ts: int = 0) -> dict:
    """Build the standard v5 response envelope."""
    return {
        "retCode": ret_code,
        "retMsg": ret_msg,
        "result": {} if result is None else result,
        "retExtInfo": {},
        "time": ts,
    }
