"""
services/mpesa_service.py
──────────────────────────
Handles all communication with the Safaricom Daraja API.

FLOW:
  1. get_access_token()     → OAuth2 token (valid 1 hour, cached)
  2. stk_push()             → Sends payment prompt to customer's phone
  3. query_stk_status()     → Polls transaction status (fallback if callback missed)
  4. parse_callback()       → Validates and extracts data from Safaricom callback
"""

import base64
import logging
from datetime import datetime, timezone
from functools import lru_cache

import httpx

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Token cache ───────────────────────────────────────────────────────────────
# Safaricom tokens are valid for 3600 seconds. We store the token and its
# expiry time so we only re-fetch when it has expired.
_token_cache: dict = {"token": None, "expires_at": 0}


def _is_token_valid() -> bool:
    import time
    return (
        _token_cache["token"] is not None
        and time.time() < _token_cache["expires_at"] - 60  # 60s buffer
    )


def get_access_token() -> str:
    """
    Fetch (or return cached) Daraja OAuth2 access token.
    Safaricom requires Basic Auth with Consumer Key + Secret.
    """
    import time

    if _is_token_valid():
        return _token_cache["token"]

    key = settings.MPESA_CONSUMER_KEY.get_secret_value()
    secret = settings.MPESA_CONSUMER_SECRET.get_secret_value()
    credentials = base64.b64encode(f"{key}:{secret}".encode()).decode()

    url = f"{settings.MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"

    with httpx.Client(timeout=10) as client:
        response = client.get(
            url,
            headers={"Authorization": f"Basic {credentials}"},
        )
        response.raise_for_status()
        data = response.json()

    token = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))

    _token_cache["token"] = token
    _token_cache["expires_at"] = time.time() + expires_in

    logger.info("Daraja: fetched new access token (expires in %ds)", expires_in)
    return token


def _generate_password() -> tuple[str, str]:
    """
    Daraja STK Push password = base64(shortcode + passkey + timestamp).
    Returns (password, timestamp) — both needed in the request body.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    shortcode = settings.MPESA_SHORTCODE
    passkey = settings.MPESA_PASSKEY.get_secret_value()
    raw = f"{shortcode}{passkey}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def _get_transaction_type(shortcode: str) -> str:
    """
    Infer the correct Daraja STK transaction type.

    If MPESA_TRANSACTION_TYPE is configured explicitly, use it.
    Otherwise infer from shortcode prefix:
      - 56xxxx Till numbers -> CustomerBuyGoodsOnline
      - 6xxxxx Paybill numbers -> CustomerPayBillOnline
    """
    explicit = settings.MPESA_TRANSACTION_TYPE
    if explicit:
        transaction_type = explicit.strip()
        logger.info(
            "Using explicit M-Pesa transaction type=%s from config",
            transaction_type,
        )
        return transaction_type

    shortcode = shortcode.strip()
    if shortcode.startswith("56"):
        logger.info("Inferred M-Pesa transaction type CustomerBuyGoodsOnline for shortcode=%s", shortcode)
        return "CustomerBuyGoodsOnline"
    if shortcode.startswith("6"):
        logger.info("Inferred M-Pesa transaction type CustomerPayBillOnline for shortcode=%s", shortcode)
        return "CustomerPayBillOnline"

    logger.warning(
        "Unable to infer M-Pesa transaction type from shortcode=%s; defaulting to CustomerPayBillOnline",
        shortcode,
    )
    return "CustomerPayBillOnline"


def stk_push(
    phone_number: str,
    amount: int,
    account_reference: str,
    description: str,
) -> dict:
    """
    Initiate an STK Push — sends a payment prompt to the customer's phone.

    Args:
        phone_number:      Customer phone in 254XXXXXXXXX format (no +, no 0)
        amount:            Amount in KES, must be a whole number (int)
        account_reference: Short label shown to customer (e.g. "INV-001")
        description:       Brief description (e.g. "Pharmacy payment")

    Returns:
        Safaricom response dict containing CheckoutRequestID and MerchantRequestID.
        Store CheckoutRequestID — you need it to match the callback.

    Raises:
        httpx.HTTPStatusError: on Safaricom API errors
        ValueError: if phone number format is invalid
    """
    phone_number = normalise_phone(phone_number)
    shortcode = settings.MPESA_SHORTCODE.strip()
    password, timestamp = _generate_password()
    token = get_access_token()
    transaction_type = _get_transaction_type(shortcode)

    party_b = settings.MPESA_PARTYB.strip() if settings.MPESA_PARTYB else shortcode
    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": transaction_type,
        "Amount": int(amount),                        # Must be integer, no decimals
        "PartyA": phone_number,                       # Customer phone
        "PartyB": party_b,
        "PhoneNumber": phone_number,
        "CallBackURL": settings.MPESA_CALLBACK_URL,
        "AccountReference": account_reference[:12],   # Max 12 chars
        "TransactionDesc": description[:13],          # Max 13 chars
    }

    if party_b != shortcode:
        logger.info("Using explicit M-Pesa PartyB=%s instead of shortcode=%s", party_b, shortcode)

    logger.debug(
        "STK Push payload: shortcode=%s transaction_type=%s partyB=%s amount=%s",
        shortcode,
        transaction_type,
        shortcode,
        amount,
    )

    url = f"{settings.MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"

    with httpx.Client(timeout=15) as client:
        response = client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()

    logger.info(
        "STK Push initiated: CheckoutRequestID=%s phone=%s amount=%d",
        data.get("CheckoutRequestID"), phone_number, amount,
    )
    return data


def query_stk_status(checkout_request_id: str) -> dict:
    """
    Query the status of an STK Push transaction.
    Use this as a fallback if the callback was not received within ~60 seconds.
    """
    password, timestamp = _generate_password()
    token = get_access_token()

    payload = {
        "BusinessShortCode": settings.MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }

    url = f"{settings.MPESA_BASE_URL}/mpesa/stkpushquery/v1/query"

    with httpx.Client(timeout=15) as client:
        response = client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()


def parse_callback(body: dict) -> dict:
    """
    Extract meaningful fields from a Safaricom STK callback body.

    Returns a normalised dict with:
        success         bool
        checkout_request_id   str
        mpesa_receipt   str | None   (M-Pesa transaction code, e.g. "QJ12345KLM")
        amount          int | None
        phone           str | None
        transaction_date str | None
        result_desc     str          (human-readable status message)
    """
    stk_callback = body.get("Body", {}).get("stkCallback", {})
    result_code = stk_callback.get("ResultCode")
    result_desc = stk_callback.get("ResultDesc", "Unknown")
    checkout_request_id = stk_callback.get("CheckoutRequestID")

    success = result_code == 0

    mpesa_receipt = None
    amount = None
    phone = None
    transaction_date = None

    if success:
        items = stk_callback.get("CallbackMetadata", {}).get("Item", [])
        meta = {item["Name"]: item.get("Value") for item in items}
        mpesa_receipt = meta.get("MpesaReceiptNumber")
        amount = meta.get("Amount")
        phone = str(meta.get("PhoneNumber", ""))
        transaction_date = str(meta.get("TransactionDate", ""))

    return {
        "success": success,
        "checkout_request_id": checkout_request_id,
        "result_code": result_code,
        "result_desc": result_desc,
        "mpesa_receipt": mpesa_receipt,
        "amount": amount,
        "phone": phone,
        "transaction_date": transaction_date,
    }


def normalise_phone(phone: str) -> str:
    """
    Convert Kenyan mobile phone formats to 2547XXXXXXXX.
      07XXXXXXXX   → 2547XXXXXXXX
      +2547XXXXXXX → 2547XXXXXXX
      2547XXXXXXX  → 2547XXXXXXX (unchanged)

    Rejects numbers that normalize to non-mobile prefixes like 2541.
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    elif len(phone) == 9 and phone.isdigit() and phone.startswith("1"):
        phone = "254" + phone

    if len(phone) != 12 or not phone.startswith("254"):
        raise ValueError(
            f"Invalid Kenyan phone number: '{phone}'. "
            "Expected 07XXXXXXXX, 2547XXXXXXXX, or local 1XXXXXXXX number."
        )

    return phone
