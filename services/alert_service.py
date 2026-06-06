"""
services/alert_service.py
──────────────────────────
Generates and manages medicine alerts (low stock, expiry).

This service is called:
  • After every sale (to check if stock dropped below reorder level).
  • On a scheduled basis via the /admin/alerts/scan endpoint.
  • On startup (after DB init).

RISKS MITIGATED:
  • Duplicate alert prevention → one active alert per medicine per type.
  • Expiry window configurable (default 30 days) → pharmacist can tune.
  • Alerts reference medicine_id not name → safe after renames.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session
from models.orm import AuditLog

from models.orm import AlertType, Inventory, Medicine, MedicineAlert

logger = logging.getLogger(__name__)

EXPIRY_WARNING_DAYS = 30  # Flag medicines expiring within this many days


def _has_active_alert(db: Session, medicine_id: int, alert_type: AlertType) -> bool:
    """Return True if an unresolved alert of this type already exists."""
    return (
        db.query(MedicineAlert)
        .filter(
            MedicineAlert.medicine_id == medicine_id,
            MedicineAlert.alert_type == alert_type,
            MedicineAlert.is_resolved == False,
        )
        .first()
    ) is not None


def check_stock_alerts(db: Session, medicine_id: int) -> None:
    """
    Check stock level for one medicine and create alert if needed.
    Called after every sale transaction.
    """
    medicine = db.get(Medicine, medicine_id)
    if not medicine or not medicine.is_active:
        return

    total_qty = (
        db.query(func.sum(Inventory.quantity))
        .filter(Inventory.medicine_id == medicine_id)
        .scalar()
    ) or 0

    if total_qty == 0:
        alert_type = AlertType.OUT_OF_STOCK
        message = f"'{medicine.name}' is completely out of stock."
        # Auto-resolve any existing LOW_STOCK alert so the worse alert is visible
        _resolve_superseded_alert(db, medicine_id, AlertType.LOW_STOCK)
    elif total_qty <= medicine.reorder_level:
        alert_type = AlertType.LOW_STOCK
        message = (
            f"'{medicine.name}' stock is low: {total_qty} units remaining "
            f"(reorder level: {medicine.reorder_level})."
        )
    else:
        # Stock is fine — auto-resolve any open stock alerts
        _resolve_superseded_alert(db, medicine_id, AlertType.LOW_STOCK)
        _resolve_superseded_alert(db, medicine_id, AlertType.OUT_OF_STOCK)
        return  # Stock is fine

    if not _has_active_alert(db, medicine_id, alert_type):
        db.add(MedicineAlert(
            medicine_id=medicine_id,
            alert_type=alert_type,
            message=message,
        ))
        db.commit()
        logger.info("Alert created: %s for medicine_id=%d", alert_type, medicine_id)

def _resolve_superseded_alert(db: Session, medicine_id: int, alert_type: AlertType) -> None:
    """Silently resolve an alert that has been superseded by a more severe one."""
    alert = (
        db.query(MedicineAlert)
        .filter(
            MedicineAlert.medicine_id == medicine_id,
            MedicineAlert.alert_type == alert_type,
            MedicineAlert.is_resolved == False,
        )
        .first()
    )
    if alert:
        alert.is_resolved = True
        alert.resolved_at = datetime.now(timezone.utc)
        # resolved_by_id stays None — indicates system auto-resolution


def scan_expiry_alerts(db: Session) -> int:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=EXPIRY_WARNING_DAYS)
    """
    Scan all inventory batches for medicines expiring within EXPIRY_WARNING_DAYS.
    Returns count of new alerts created.
    """
    now_naive = now.replace(tzinfo=None)
    cutoff_naive = cutoff.replace(tzinfo=None)

    expiring_batches = (
        db.query(Inventory)
        .filter(
            Inventory.expires_at <= cutoff_naive,
            Inventory.expires_at >= now_naive,
            Inventory.quantity > 0,
        )
        .all()
    )


    new_alerts = 0
    for batch in expiring_batches:
        medicine = db.get(Medicine, batch.medicine_id)
        if medicine is None:
            logger.warning(
                "Inventory batch id=%d references non-existent medicine_id=%d — skipping.",
                batch.id, batch.medicine_id,
            )
            continue  # Skip this batch entirely; don't insert a broken alert

        if not _has_active_alert(db, batch.medicine_id, AlertType.EXPIRY):
            expires_aware = (
                batch.expires_at.replace(tzinfo=timezone.utc)
                if batch.expires_at.tzinfo is None
                else batch.expires_at
            )
            days_left = (expires_aware - datetime.now(timezone.utc)).days

            db.add(MedicineAlert(
                medicine_id=batch.medicine_id,
                alert_type=AlertType.EXPIRY,
                message=(
                    f"Batch '{batch.batch_number or 'N/A'}' of "
                    f"'{medicine.name if medicine else 'Unknown'}' "
                    f"expires in {days_left} day(s) "
                    f"({batch.expires_at.strftime('%Y-%m-%d')}). "
                    f"Quantity: {batch.quantity}."
                ),
            ))
            new_alerts += 1

    if new_alerts:
        db.commit()
        logger.info("Expiry scan: %d new alert(s) created", new_alerts)
    return new_alerts


def resolve_alert(db: Session, alert_id: int, user_id: int) -> MedicineAlert:
    """Mark an alert as resolved."""
    alert = db.get(MedicineAlert, alert_id)
    if not alert:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")

    alert.is_resolved = True
    alert.resolved_by_id = user_id
    alert.resolved_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        user_id=user_id,
        action="ALERT_RESOLVED",
        entity="MedicineAlert",
        entity_id=alert_id,
        detail=f"Alert type={alert.alert_type} resolved for medicine_id={alert.medicine_id}",
    ))
    db.commit()
    db.refresh(alert)
    return alert