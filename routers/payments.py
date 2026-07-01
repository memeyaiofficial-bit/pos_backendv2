"""
routers/payments.py
────────────────────
M-Pesa STK Push endpoints.

ENDPOINTS:
  POST /payments/mpesa/stk-push    → Initiate payment (cashier triggers this)
  POST /payments/mpesa/callback    → Safaricom calls this after customer pays
  GET  /payments/mpesa/{checkout_request_id}/status → Poll payment status
"""

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models.orm import AuditLog, MpesaTransaction, MpesaStatus, Sale, SaleStatus
from schemas.schemas import MpesaSTKPushIn, MpesaSTKPushOut, MpesaStatusOut
from services.mpesa_service import stk_push, query_stk_status, parse_callback
from utils.errors import safe_error
from utils.security import get_current_user, require_admin_or_pharmacist
from models.orm import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments/mpesa", tags=["Payments"])


# ── Initiate STK Push ─────────────────────────────────────────────────────────

@router.post("/register-payment", response_model=MpesaSTKPushOut,
             summary="Send KES 300 STK Push for new registration (no auth required)")
def register_payment(
    payload: MpesaSTKPushIn,
    db: Session = Depends(get_db),
):
    """
    Public endpoint (no auth) — sends STK Push for KES 300 registration fee.
    No sale_id needed — the transaction is tracked independently.
    """
    try:
        result = stk_push(
            phone_number=payload.phone_number,
            amount=300,
            account_reference="REG-UZAPAP",
            description="Pharmacy POS reg",
        )

        # Check if Safaricom returned an error response code
        response_code = result.get("ResponseCode")
        if response_code and response_code != "0":
            error_detail = result.get("ResponseDescription", f"Safaricom error code {response_code}")
            logger.error("M-Pesa STK push rejected: %s", result)
            raise HTTPException(
                status_code=400,
                detail=f"M-Pesa declined the request: {error_detail}. Check your Daraja credentials (consumer key/secret, passkey, shortcode)."
            )

        txn = MpesaTransaction(
            sale_id=None,
            checkout_request_id=result["CheckoutRequestID"],
            merchant_request_id=result["MerchantRequestID"],
            phone_number=payload.phone_number,
            amount=300,
            status=MpesaStatus.PENDING,
        )
        db.add(txn)
        db.commit()
        db.refresh(txn)

        return MpesaSTKPushOut(
            checkout_request_id=txn.checkout_request_id,
            message="M-Pesa prompt sent for KES 300 registration. Enter PIN on your phone.",
        )

    except HTTPException:
        db.rollback()
        raise
    except httpx.HTTPStatusError as e:
        db.rollback()
        logger.error("M-Pesa HTTP error: %s - %s", e, e.response.text if hasattr(e, 'response') else '')
        raise HTTPException(
            status_code=502,
            detail=f"Safaricom API error (HTTP {e.response.status_code}): {e.response.text[:200] if hasattr(e, 'response') else str(e)}. Verify MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET."
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        logger.error("M-Pesa unexpected error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=f"M-Pesa request failed: {str(e)[:200]}. Check Daraja credentials and network."
        )


@router.post("/stk-push", response_model=MpesaSTKPushOut,
             summary="Send M-Pesa payment prompt to customer")
def initiate_stk_push(
    payload: MpesaSTKPushIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cashier enters the customer's phone number and the sale amount.
    This sends an STK Push prompt to the customer's phone.
    Customer has 60 seconds to enter their PIN.
    """
    # Verify the sale exists and is in the right state
    sale = db.get(Sale, payload.sale_id)
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    if sale.status != SaleStatus.COMPLETED:
        raise HTTPException(
            status_code=400,
            detail="Can only request payment for a completed sale"
        )

    # Check no pending payment already exists for this sale
    existing = (
        db.query(MpesaTransaction)
        .filter(
            MpesaTransaction.sale_id == payload.sale_id,
            MpesaTransaction.status == MpesaStatus.PENDING,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A payment request is already pending for this sale. "
                   "Ask the customer to check their phone or wait 60 seconds."
        )

    try:
        amount_kes = int(sale.total_amount)  # M-Pesa only accepts whole KES
        reference = f"SALE-{sale.id}"

        result = stk_push(
            phone_number=payload.phone_number,
            amount=amount_kes,
            account_reference=reference,
            description="Pharmacy payment",
        )

        # Persist the pending transaction immediately
        txn = MpesaTransaction(
            sale_id=sale.id,
            checkout_request_id=result["CheckoutRequestID"],
            merchant_request_id=result["MerchantRequestID"],
            phone_number=payload.phone_number,
            amount=amount_kes,
            status=MpesaStatus.PENDING,
        )
        db.add(txn)
        db.add(AuditLog(
            user_id=current_user.id,
            action="MPESA_STK_PUSH",
            entity="Sale",
            entity_id=sale.id,
            detail=f"STK Push sent to {payload.phone_number} for KES {amount_kes}",
        ))
        db.commit()
        db.refresh(txn)

        return MpesaSTKPushOut(
            checkout_request_id=txn.checkout_request_id,
            message="Payment prompt sent. Ask the customer to enter their M-Pesa PIN.",
        )

    except HTTPException:
        raise
    except ValueError as e:
        # Phone number validation error from _normalise_phone
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not initiate M-Pesa payment. Please try again.")


# ── Safaricom Callback ────────────────────────────────────────────────────────

@router.post("/callback", summary="Safaricom payment result callback (do not call manually)")
async def mpesa_callback(request: Request, db: Session = Depends(get_db)):
    """
    Safaricom POSTs the payment result here after the customer acts on the STK prompt.

    SECURITY NOTES:
      • This endpoint must NOT require authentication — Safaricom calls it directly.
      • Always return HTTP 200 to Safaricom, even on errors. If you return non-200,
        Safaricom will retry the callback indefinitely.
      • Validate the CheckoutRequestID exists in your DB before trusting the payload.
      • Ideally, restrict this endpoint to Safaricom's IP ranges at the load balancer
        or nginx level (see Safaricom documentation for their IP list).
    """
    try:
        body = await request.json()
        logger.info("M-Pesa callback received: %s", body)

        parsed = parse_callback(body)
        checkout_id = parsed["checkout_request_id"]

        if not checkout_id:
            logger.warning("Callback missing CheckoutRequestID — ignoring")
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        # Find our record — reject unknown checkout IDs (prevents spoofed callbacks)
        txn = (
            db.query(MpesaTransaction)
            .filter(MpesaTransaction.checkout_request_id == checkout_id)
            .first()
        )
        if not txn:
            logger.warning("Callback for unknown CheckoutRequestID=%s", checkout_id)
            return {"ResultCode": 0, "ResultDesc": "Accepted"}  # Still return 200

        # Idempotency: if already processed, don't update again
        if txn.status != MpesaStatus.PENDING:
            logger.info("Duplicate callback for CheckoutRequestID=%s — already %s", checkout_id, txn.status)
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        # Update transaction
        txn.result_code = parsed["result_code"]
        txn.result_desc = parsed["result_desc"]
        txn.mpesa_receipt = parsed["mpesa_receipt"]
        txn.updated_at = datetime.now(timezone.utc)
        txn.status = MpesaStatus.SUCCESS if parsed["success"] else MpesaStatus.FAILED

        # Verify amount matches what we expected (prevents underpayment attacks)
        if parsed["success"] and parsed["amount"] is not None:
            if int(parsed["amount"]) < txn.amount:
                logger.error(
                    "AMOUNT MISMATCH: expected KES %d, received KES %s for CheckoutRequestID=%s",
                    txn.amount, parsed["amount"], checkout_id,
                )
                txn.status = MpesaStatus.FAILED
                txn.result_desc = f"Amount mismatch: expected {txn.amount}, got {parsed['amount']}"

        db.add(AuditLog(
            user_id=None,  # System-generated (Safaricom callback)
            action="MPESA_CALLBACK",
            entity="MpesaTransaction",
            entity_id=txn.id,
            detail=(
                f"Receipt={parsed['mpesa_receipt']} "
                f"Status={txn.status} "
                f"ResultCode={parsed['result_code']}"
            ),
        ))
        db.commit()

        logger.info(
            "M-Pesa payment %s: CheckoutRequestID=%s receipt=%s",
            txn.status, checkout_id, parsed["mpesa_receipt"],
        )

    except Exception as e:
        # IMPORTANT: always return 200 to Safaricom even if we had an internal error.
        # Log it and investigate manually. Do NOT let exceptions bubble up here.
        logger.error("Error processing M-Pesa callback: %s", e, exc_info=True)

    # Safaricom expects exactly this response shape
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# ── Status Poll ───────────────────────────────────────────────────────────────

@router.get("/{checkout_request_id}/status", response_model=MpesaStatusOut,
            summary="Check payment status")
def get_payment_status(
    checkout_request_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Authenticated — check M-Pesa payment status.
    Poll every 5 seconds while waiting for customer PIN.
    """
    txn = (
        db.query(MpesaTransaction)
        .filter(MpesaTransaction.checkout_request_id == checkout_request_id)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # If still pending after 70s, query Safaricom directly
    if txn.status == MpesaStatus.PENDING:
        age_seconds = (datetime.now(timezone.utc) - txn.created_at).total_seconds()
        if age_seconds > 70:
            try:
                result = query_stk_status(checkout_request_id)
                result_code = int(result.get("ResultCode", -1))
                if result_code == 0:
                    txn.status = MpesaStatus.SUCCESS
                    txn.result_desc = result.get("ResultDesc")
                elif result_code != 1032:  # 1032 = still waiting
                    txn.status = MpesaStatus.FAILED
                    txn.result_desc = result.get("ResultDesc")
                txn.updated_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as e:
                logger.warning("STK status query failed: %s", e)

    return MpesaStatusOut(
        checkout_request_id=txn.checkout_request_id,
        status=txn.status,
        mpesa_receipt=txn.mpesa_receipt,
        amount=txn.amount,
        result_desc=txn.result_desc,
    )


@router.get("/{checkout_request_id}/status/public", response_model=MpesaStatusOut,
            summary="Check payment status (no auth required)")
def get_payment_status_public(
    checkout_request_id: str,
    db: Session = Depends(get_db),
):
    """
    Public (no auth) — check M-Pesa payment status.
    Used by the landing page registration flow.
    """
    txn = (
        db.query(MpesaTransaction)
        .filter(MpesaTransaction.checkout_request_id == checkout_request_id)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if txn.status == MpesaStatus.PENDING:
        age_seconds = (datetime.now(timezone.utc) - txn.created_at).total_seconds()
        if age_seconds > 70:
            try:
                result = query_stk_status(checkout_request_id)
                result_code = int(result.get("ResultCode", -1))
                if result_code == 0:
                    txn.status = MpesaStatus.SUCCESS
                    txn.result_desc = result.get("ResultDesc")
                elif result_code != 1032:
                    txn.status = MpesaStatus.FAILED
                    txn.result_desc = result.get("ResultDesc")
                txn.updated_at = datetime.now(timezone.utc)
                db.commit()
            except Exception as e:
                logger.warning("STK status query failed: %s", e)

    return MpesaStatusOut(
        checkout_request_id=txn.checkout_request_id,
        status=txn.status,
        mpesa_receipt=txn.mpesa_receipt,
        amount=txn.amount,
        result_desc=txn.result_desc,
    )
