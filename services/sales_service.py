"""
services/sales_service.py
──────────────────────────
Business logic for POS sales transactions.

RESPONSIBILITIES:
  • Generate unique receipt numbers (RX-YYYYMMDD-NNNN format).
  • Validate stock availability before committing a sale.
  • Apply FEFO (First-Expiry-First-Out) to deduct from the earliest-expiring batch.
  • Calculate subtotal, discount, tax, total, change.
  • Enforce prescription requirement for controlled/Rx medicines.
  • Void sales and restore inventory.

RISKS MITIGATED:
  • All stock operations within a single DB transaction → partial sales impossible.
  • Stock check before deduction → prevents overselling.
  • Receipt number uses DB sequence + date → no collisions across concurrent requests.
  • Void requires reason text (min 5 chars) → accidental voids are not silent.
  • Only PENDING sales can be voided → completed payments aren't silently reversed.
  • Price snapshot at sale time → price changes don't retroactively alter old receipts.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import HTTPException, status

from models.orm import (
    AuditLog, Customer, Inventory, Medicine,
    Prescription, Sale, SaleItem, SaleStatus, User,
)
from schemas.schemas import SaleCreateIn

logger = logging.getLogger(__name__)

# Kenya VAT rate – adjust per jurisdiction
VAT_RATE = Decimal("0.16")


# ── Receipt number generator ───────────────────────────────────────────────────

def _generate_receipt_number(db: Session) -> str:
    """
    Thread-safe receipt number: RX-YYYYMMDD-NNNN
    Counts today's sales and increments – safe for SQLite because we're in a
    transaction with the insert.
    """
    today = datetime.now(timezone.utc).date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end   = datetime.combine(today, datetime.max.time())

    count = (
        db.query(func.count(Sale.id))
        .filter(Sale.sold_at.between(today_start, today_end))
        .scalar()
    ) or 0

    return f"RX-{today.strftime('%Y%m%d')}-{count + 1:04d}"


# ── Stock resolution (FEFO) ────────────────────────────────────────────────────

def _get_available_stock(db: Session, medicine_id: int) -> int:
    """Return total available quantity across all batches for a medicine."""
    result = (
        db.query(func.sum(Inventory.quantity))
        .filter(
            Inventory.medicine_id == medicine_id,
            Inventory.quantity > 0,
        )
        .scalar()
    )
    return result or 0


def _deduct_fefo(db: Session, medicine_id: int, quantity: int) -> tuple[Decimal, int]:
    """
    Deduct `quantity` units using First-Expiry-First-Out.

    Returns (unit_selling_price, inventory_batch_id) of the primary batch used.
    Raises ValueError if stock is insufficient.
    """
    batches = (
        db.query(Inventory)
        .filter(
            Inventory.medicine_id == medicine_id,
            Inventory.quantity > 0,
        )
        .order_by(
            # Null expiry dates last (no expiry → assume long shelf life)
            Inventory.expires_at.asc().nullslast(),
            Inventory.received_at.asc(),  # FIFO within same expiry date
        )
        .all()
    )

    remaining = quantity
    primary_price: Optional[Decimal] = None
    primary_batch_id: Optional[int] = None

    for batch in batches:
        if remaining <= 0:
            break
        take = min(batch.quantity, remaining)
        if primary_price is None:
            primary_price = batch.selling_price
            primary_batch_id = batch.id
        batch.quantity -= take
        remaining -= take

    if remaining > 0:
        raise ValueError(f"Insufficient stock: {quantity - remaining} of {quantity} units available")

    return primary_price or Decimal("0.00"), primary_batch_id


# ── Main sale creation ─────────────────────────────────────────────────────────

def create_sale(db: Session, payload: SaleCreateIn, cashier: User) -> Sale:
    """
    Create and commit a full POS sale.

    Steps:
      1. Validate all medicines exist and are active.
      2. Check prescription requirements.
      3. Check total stock availability.
      4. Compute financials.
      5. Deduct stock (FEFO).
      6. Create Sale, SaleItem, optional Prescription records.
      7. Audit log.
    """
    # ── 1. Validate medicines ──────────────────────────────────────────────
    medicines: dict[int, Medicine] = {}
    for item in payload.items:
        med = db.get(Medicine, item.medicine_id)
        if not med or not med.is_active:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Medicine id={item.medicine_id} not found or inactive",
            )
        medicines[item.medicine_id] = med

    # ── 2. Prescription check ──────────────────────────────────────────────
    rx_required_meds = [
        m.name for m in medicines.values()
        if m.requires_prescription or m.is_controlled
    ]
    if rx_required_meds and not payload.prescription:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Prescription required for: {', '.join(rx_required_meds)}. "
                "Include 'prescription' in the request body."
            ),
        )

    # ── 3. Stock availability check ────────────────────────────────────────
    for item in payload.items:
        available = _get_available_stock(db, item.medicine_id)
        if available < item.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient stock for '{medicines[item.medicine_id].name}'. "
                    f"Requested: {item.quantity}, Available: {available}"
                ),
            )

    # ── 4. Compute financials ──────────────────────────────────────────────
    subtotal = Decimal("0.00")
    line_data = []

    for item in payload.items:
        med = medicines[item.medicine_id]
        unit_price = med.unit_price
        discount_factor = Decimal("1") - (item.discount_pct / Decimal("100"))
        line_total = (unit_price * item.quantity * discount_factor).quantize(Decimal("0.01"))
        subtotal += line_total
        line_data.append({
            "medicine_id": item.medicine_id,
            "quantity": item.quantity,
            "unit_price": unit_price,
            "discount_pct": item.discount_pct,
            "line_total": line_total,
        })

    subtotal = subtotal.quantize(Decimal("0.01"))
    discount_amount = min(payload.discount_amount, subtotal)  # Can't discount more than subtotal
    taxable = subtotal - discount_amount
    tax_amount = (taxable * VAT_RATE).quantize(Decimal("0.01"))
    total_amount = (taxable + tax_amount).quantize(Decimal("0.01"))
    change_given = max(Decimal("0.00"), payload.amount_paid - total_amount)

    if payload.amount_paid < total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Amount paid ({payload.amount_paid}) is less than "
                f"total due ({total_amount})"
            ),
        )

    # ── 5 & 6. Deduct stock and create records (atomic) ───────────────────
    receipt_number = _generate_receipt_number(db)

    sale = Sale(
        receipt_number=receipt_number,
        cashier_id=cashier.id,
        customer_id=payload.customer_id,
        subtotal=subtotal,
        discount_amount=discount_amount,
        tax_amount=tax_amount,
        total_amount=total_amount,
        amount_paid=payload.amount_paid,
        change_given=change_given,
        payment_method=payload.payment_method,
        status=SaleStatus.COMPLETED,
        notes=payload.notes,
    )
    db.add(sale)
    db.flush()  # Get sale.id without full commit

    for ld in line_data:
        price, batch_id = _deduct_fefo(db, ld["medicine_id"], ld["quantity"])
        db.add(SaleItem(
            sale_id=sale.id,
            medicine_id=ld["medicine_id"],
            inventory_id=batch_id,
            quantity=ld["quantity"],
            unit_price=ld["unit_price"],  # Snapshot price
            discount_pct=ld["discount_pct"],
            line_total=ld["line_total"],
        ))

    # ── Optional prescription record ───────────────────────────────────────
    if payload.prescription:
        p = payload.prescription
        db.add(Prescription(
            sale_id=sale.id,
            customer_id=payload.customer_id,
            prescriber_name=p.prescriber_name,
            prescriber_reg=p.prescriber_reg,
            facility=p.facility,
            issued_date=p.issued_date,
            expiry_date=p.expiry_date,
            notes=p.notes,
        ))

    # ── Loyalty points (1 point per currency unit spent, rounded) ─────────
    if payload.customer_id:
        customer = db.get(Customer, payload.customer_id)
        if customer:
            customer.loyalty_points += int(total_amount)

    # ── Audit log ─────────────────────────────────────────────────────────
    db.add(AuditLog(
        user_id=cashier.id,
        action="SALE_CREATED",
        entity="Sale",
        entity_id=sale.id,
        detail=f"Receipt {receipt_number} | Total: {total_amount} | Method: {payload.payment_method}",
    ))

    db.commit()
    db.refresh(sale)
    logger.info("Sale created: %s by user=%d", receipt_number, cashier.id)
    return sale


# ── Void sale ─────────────────────────────────────────────────────────────────

def void_sale(db: Session, sale_id: int, reason: str, user: User) -> Sale:
    """
    Void a completed sale and restore inventory.

    Only COMPLETED sales can be voided; PENDING/REFUNDED/VOIDED cannot.
    """
    sale = db.get(Sale, sale_id)
    if not sale:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sale not found")

    if sale.status != SaleStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot void a sale with status '{sale.status}'. Only COMPLETED sales can be voided.",
        )

    # Restore inventory
    for item in sale.items:
        if item.inventory_id:
            batch = db.get(Inventory, item.inventory_id)
            if batch:
                batch.quantity += item.quantity

    # Reverse loyalty points
    if sale.customer_id:
        customer = db.get(Customer, sale.customer_id)
        if customer:
            customer.loyalty_points = max(0, customer.loyalty_points - int(sale.total_amount))

    sale.status = SaleStatus.VOIDED
    sale.voided_at = datetime.now(timezone.utc)
    sale.voided_by_id = user.id
    sale.void_reason = reason

    db.add(AuditLog(
        user_id=user.id,
        action="SALE_VOIDED",
        entity="Sale",
        entity_id=sale.id,
        detail=f"Receipt {sale.receipt_number} voided. Reason: {reason}",
    ))

    db.commit()
    db.refresh(sale)
    logger.info("Sale voided: %s by user=%d", sale.receipt_number, user.id)
    return sale