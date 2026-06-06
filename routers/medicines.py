"""
routers/medicines.py
─────────────────────
Medicine catalogue endpoints + WHO/OpenFDA sync trigger.

ACCESS:
  • Search/read  → any authenticated user
  • Create/update/delete → Admin or Pharmacist
  • Sync trigger → Admin only
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from sqlalchemy import literal

from database import get_db
from models.orm import AuditLog, Medicine, User
from schemas.schemas import (
    MedicineCreateIn, MedicineOut, MedicineSearchOut,
    MedicineUpdateIn, MedicineSyncResultOut,
)
from services.medicine_sync import sync_who_medicines
from utils.security import (
    get_current_user, require_admin,
    require_admin_or_pharmacist,
)

router = APIRouter(prefix="/medicines", tags=["Medicines"])


@router.get("", response_model=list[MedicineSearchOut], summary="Search medicines")
def search_medicines(
    q: str = Query(None, description="Search by name, generic name, or ATC code"),
    requires_prescription: bool = Query(None),
    is_controlled: bool = Query(None),
    is_active: bool = Query(True),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Full-text search on medicine name and generic name.
    Returns lightweight search results (use GET /{id} for full detail).
    """
    query = db.query(Medicine)

    if is_active is not None:
        query = query.filter(Medicine.is_active == literal(is_active))
    if requires_prescription is not None:
        query = query.filter(Medicine.requires_prescription == literal(requires_prescription))
    if is_controlled is not None:
        query = query.filter(Medicine.is_controlled == literal(is_controlled))
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                Medicine.name.ilike(pattern),
                Medicine.generic_name.ilike(pattern),
                Medicine.brand_name.ilike(pattern),
                Medicine.atc_code.ilike(pattern),
                Medicine.barcode.ilike(pattern),
            )
        )

    return query.order_by(Medicine.name).offset(skip).limit(limit).all()


@router.get("/barcode/{barcode}", response_model=MedicineOut, summary="Look up by barcode")
def get_by_barcode(
    barcode: str,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Used by barcode scanner at POS terminal."""
    med = db.query(Medicine).filter(Medicine.barcode == barcode).first()
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"No medicine found with barcode '{barcode}'")
    return med


@router.get("/{medicine_id}", response_model=MedicineOut, summary="Get medicine detail")
def get_medicine(
    medicine_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")
    return med


@router.post("", response_model=MedicineOut, status_code=status.HTTP_201_CREATED,
             summary="Add medicine manually (admin/pharmacist)")
def create_medicine(
    payload: MedicineCreateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    """Add a medicine that isn't in the WHO catalogue."""
    # Barcode uniqueness check
    if payload.barcode:
        existing = db.query(Medicine).filter(Medicine.barcode == payload.barcode).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Medicine with barcode '{payload.barcode}' already exists",
            )

    med = Medicine(**payload.model_dump(), source="manual")
    db.add(med)
    db.flush()

    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_CREATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Created medicine: {med.name}",
    ))
    db.commit()
    db.refresh(med)
    return med


@router.patch("/{medicine_id}", response_model=MedicineOut,
              summary="Update medicine (admin/pharmacist)")
def update_medicine(
    medicine_id: int,
    payload: MedicineUpdateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    changes = payload.model_dump(exclude_none=True)

    # Barcode uniqueness check if changing
    if "barcode" in changes and changes["barcode"]:
        dup = db.query(Medicine).filter(
            Medicine.barcode == changes["barcode"],
            Medicine.id != medicine_id,
        ).first()
        if dup:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Barcode '{changes['barcode']}' belongs to another medicine",
            )

    for field, value in changes.items():
        setattr(med, field, value)

    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_UPDATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Updated fields: {list(changes.keys())}",
    ))
    db.commit()
    db.refresh(med)
    return med


@router.delete("/{medicine_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Deactivate medicine (admin)")
def deactivate_medicine(
    medicine_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete: marks medicine as inactive (preserves sales history)."""
    med = db.get(Medicine, medicine_id)
    if not med:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Medicine not found")

    med.is_active = False
    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_DEACTIVATED",
        entity="Medicine",
        entity_id=med.id,
        detail=f"Deactivated medicine: {med.name}",
    ))
    db.commit()


@router.post("/sync/who-eml", response_model=MedicineSyncResultOut,
             summary="Import WHO Essential Medicines (admin)")
def trigger_who_sync(
    enrich_openfda: bool = Query(True, description="Also enrich with OpenFDA data"),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Import the WHO Essential Medicines List into the catalogue.

    This is idempotent – already-imported medicines are skipped.
    If enrich_openfda=true, each medicine is additionally enriched with
    brand name, manufacturer, and description from OpenFDA's drug label API.

    Note: With OpenFDA enrichment this may take 30-60 seconds depending on
    the number of medicines and network latency.
    """
    db.add(AuditLog(
        user_id=current_user.id,
        action="MEDICINE_SYNC_TRIGGERED",
        detail=f"WHO EML sync started (openfda={enrich_openfda})",
    ))
    db.commit()

    # Run async sync in a new event loop (safe for sync FastAPI endpoints)
    result = asyncio.run(sync_who_medicines(db, enrich_with_openfda=enrich_openfda))

    return MedicineSyncResultOut(
        imported=result["imported"],
        skipped=result["skipped"],
        errors=result["errors"],
        message=(
            f"Sync complete. {result['imported']} imported, "
            f"{result['skipped']} already existed, "
            f"{result['errors']} error(s)."
        ),
    )