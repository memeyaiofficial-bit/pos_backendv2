"""
routers/all_routers.py
───────────────────────
All routers: Inventory, Sales, Customers, Suppliers,
Purchase Orders, Alerts, Reports
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models.orm import (
    AuditLog, Customer, Inventory, Medicine, MedicineAlert,
    PurchaseOrder, POItem, POStatus, Sale, SaleItem, SaleStatus,
    Supplier, User, UserRole,
)
from schemas.schemas import (
    AlertOut,
    CustomerCreateIn, CustomerOut, CustomerUpdateIn,
    DashboardOut,
    InventoryCreateIn, InventoryOut, InventoryUpdateIn,
    PurchaseOrderCreateIn, PurchaseOrderOut,
    SaleCreateIn, SaleOut,
    SalesSummaryOut,
    SupplierCreateIn, SupplierOut, SupplierUpdateIn,
    TopMedicineOut,
    VoidSaleIn,
)
from services.alert_service import check_stock_alerts, resolve_alert, scan_expiry_alerts
from services.sales_service import create_sale, void_sale
from utils.security import get_current_user, require_admin, require_admin_or_pharmacist

from utils.errors import safe_error


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY ROUTER
# ══════════════════════════════════════════════════════════════════════════════

inventory_router = APIRouter(prefix="/inventory", tags=["Inventory"])


@inventory_router.get("", response_model=list[InventoryOut], summary="List inventory batches")
def list_inventory(
    medicine_id: int = Query(None),
    expiring_in_days: int = Query(None, ge=1),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    q = db.query(Inventory)
    if medicine_id:
        q = q.filter(Inventory.medicine_id == medicine_id)
    if expiring_in_days:
        cutoff = datetime.now(timezone.utc) + timedelta(days=expiring_in_days)
        q = q.filter(Inventory.expires_at <= cutoff, Inventory.quantity > 0)
    return q.order_by(Inventory.expires_at.asc().nullslast()).offset(skip).limit(limit).all()


@inventory_router.post("", response_model=InventoryOut, status_code=status.HTTP_201_CREATED,
                       summary="Receive stock")
def receive_stock(
    payload: InventoryCreateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        med = db.get(Medicine, payload.medicine_id)
        if not med or not med.is_active:
            raise HTTPException(status_code=404, detail="Medicine not found or inactive")

        if payload.supplier_id:
            supplier = db.get(Supplier, payload.supplier_id)
            if not supplier or not supplier.is_active:
                raise HTTPException(status_code=404, detail="Supplier not found or inactive")

        batch = Inventory(**payload.model_dump())
        db.add(batch)
        med.unit_price = payload.selling_price
        db.flush()

        db.add(AuditLog(
            user_id=current_user.id,
            action="STOCK_RECEIVED",
            entity="Inventory",
            entity_id=batch.id,
            detail=(
                f"Received {payload.quantity} units of '{med.name}' "
                f"batch={payload.batch_number} cost={payload.unit_cost}"
            ),
        ))
        db.commit()
        db.refresh(batch)
        return batch

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not add inventory. Please try again or contact support.")


@inventory_router.patch("/{inventory_id}", response_model=InventoryOut,
                        summary="Update inventory batch")
def update_inventory(
    inventory_id: int,
    payload: InventoryUpdateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        batch = db.get(Inventory, inventory_id)
        if not batch:
            raise HTTPException(status_code=404, detail="Inventory batch not found")

        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(batch, field, value)

        db.add(AuditLog(
            user_id=current_user.id,
            action="INVENTORY_UPDATED",
            entity="Inventory",
            entity_id=batch.id,
        ))
        db.commit()
        db.refresh(batch)
        return batch

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e,"Could not update inventory. Please try again or contact support.")


@inventory_router.get("/summary/by-medicine", summary="Stock summary per medicine")
def stock_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(
            Medicine.id.label("medicine_id"),
            Medicine.name.label("medicine_name"),
            func.coalesce(func.sum(Inventory.quantity), 0).label("total_quantity"),
            func.min(Inventory.selling_price).label("lowest_selling_price"),
            func.min(Inventory.expires_at).label("earliest_expiry"),
        )
        .outerjoin(Inventory, Inventory.medicine_id == Medicine.id)
        .filter(Medicine.is_active == True)
        .group_by(Medicine.id, Medicine.name)
        .order_by(Medicine.name)
        .all()
    )
    return [
        {
            "medicine_id": r.medicine_id,
            "medicine_name": r.medicine_name,
            "total_quantity": r.total_quantity,
            "lowest_selling_price": r.lowest_selling_price,
            "earliest_expiry": r.earliest_expiry,
        }
        for r in rows
    ]


# ══════════════════════════════════════════════════════════════════════════════
# SALES ROUTER
# ══════════════════════════════════════════════════════════════════════════════

sales_router = APIRouter(prefix="/sales", tags=["Sales"])


@sales_router.post("", response_model=SaleOut, status_code=status.HTTP_201_CREATED,
                   summary="Process a sale (POS)")
def process_sale(
    payload: SaleCreateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        sale = create_sale(db, payload, current_user)
        # Post-sale stock alerts — non-fatal
        try:
            for item in payload.items:
                check_stock_alerts(db, item.medicine_id)
        except Exception:
            pass
        return sale
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Sale failed. Please try again or contact support.")


@sales_router.get("", response_model=list[SaleOut], summary="List sales")
def list_sales(
    from_date: datetime = Query(None),
    to_date: datetime = Query(None),
    status_filter: SaleStatus = Query(None, alias="status"),
    customer_id: int = Query(None),
    cashier_id: int = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Sale).options(joinedload(Sale.items))

    if current_user.role == UserRole.CASHIER:
        q = q.filter(Sale.cashier_id == current_user.id)
    else:
        if cashier_id:
            q = q.filter(Sale.cashier_id == cashier_id)

    if from_date:
        q = q.filter(Sale.sold_at >= from_date)
    if to_date:
        q = q.filter(Sale.sold_at <= to_date)
    if status_filter:
        q = q.filter(Sale.status == status_filter)
    if customer_id:
        q = q.filter(Sale.customer_id == customer_id)

    return q.order_by(Sale.sold_at.desc()).offset(skip).limit(limit).all()


@sales_router.get("/{sale_id}", response_model=SaleOut, summary="Get sale detail")
def get_sale(
    sale_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sale = (
        db.query(Sale)
        .options(joinedload(Sale.items))
        .filter(Sale.id == sale_id)
        .first()
    )
    if not sale:
        raise HTTPException(status_code=404, detail="Sale not found")
    if current_user.role == UserRole.CASHIER and sale.cashier_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return sale


@sales_router.post("/{sale_id}/void", response_model=SaleOut, summary="Void a sale")
def void_sale_endpoint(
    sale_id: int,
    payload: VoidSaleIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        return void_sale(db, sale_id, payload.reason, current_user)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not void sale. Please contact support.")


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

customers_router = APIRouter(prefix="/customers", tags=["Customers"])


@customers_router.get("", response_model=list[CustomerOut], summary="Search customers")
def list_customers(
    q: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Customer).filter(Customer.is_active == True)
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                Customer.full_name.ilike(pattern),
                Customer.phone.ilike(pattern),
                Customer.email.ilike(pattern),
                Customer.id_number.ilike(pattern),
            )
        )
    return query.order_by(Customer.full_name).offset(skip).limit(limit).all()


@customers_router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED,
                       summary="Register a customer")
def create_customer(
    payload: CustomerCreateIn,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        if payload.phone:
            existing = db.query(Customer).filter(Customer.phone == payload.phone).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Customer with phone '{payload.phone}' already exists",
                )
        customer = Customer(**payload.model_dump())
        db.add(customer)
        db.commit()
        db.refresh(customer)
        return customer
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not register customer.")


@customers_router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(
    customer_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(status_code=404, detail="Customer not found")
    return c


@customers_router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: int,
    payload: CustomerUpdateIn,
    current_user:User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        c = db.get(Customer, customer_id)
        if not c:
            raise HTTPException(status_code=404, detail="Customer not found")
        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(c, field, value)
        db.add(AuditLog(
            user_id=current_user.id,
            action="CUSTOMER_UPDATED",
            entity="Customer",
            entity_id=customer_id,
            detail=f"Fields updated: {list(payload.model_dump(exclude_none=True).keys())}",
        ))
        db.commit()
        db.refresh(c)
        return c
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not update customer.")


@customers_router.get("/{customer_id}/sales", response_model=list[SaleOut],
                      summary="Customer purchase history")
def customer_sales(
    customer_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Verify the customer exists first
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    q = (
        db.query(Sale)
        .options(joinedload(Sale.items))
        .filter(Sale.customer_id == customer_id)
    )
    # Cashiers can only see their own transactions for this customer
    if current_user.role == UserRole.CASHIER:
        q = q.filter(Sale.cashier_id == current_user.id)

    return q.order_by(Sale.sold_at.desc()).offset(skip).limit(limit).all()




# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

suppliers_router = APIRouter(prefix="/suppliers", tags=["Suppliers"])


@suppliers_router.get("", response_model=list[SupplierOut])
def list_suppliers(
    q: str = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    query = db.query(Supplier).filter(Supplier.is_active == True)
    if q:
        query = query.filter(Supplier.name.ilike(f"%{q}%"))
    return query.order_by(Supplier.name).offset(skip).limit(limit).all()


@suppliers_router.post("", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        existing = db.query(Supplier).filter(Supplier.name == payload.name).first()
        if existing:
            raise HTTPException(status_code=409, detail="Supplier name already exists")
        s = Supplier(**payload.model_dump())
        db.add(s)
        db.add(AuditLog(
            user_id=current_user.id,
            action="SUPPLIER_CREATED",
            detail=f"Created supplier: {s.name}",
        ))
        db.commit()
        db.refresh(s)
        return s
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not create supplier.")


@suppliers_router.patch("/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        s = db.get(Supplier, supplier_id)
        if not s:
            raise HTTPException(status_code=404, detail="Supplier not found")
        for field, value in payload.model_dump(exclude_none=True).items():
            setattr(s, field, value)
        db.add(AuditLog(
            user_id=current_user.id,
            action="SUPPLIER_UPDATED",
            entity="Supplier",
            entity_id=supplier_id,
            detail=f"Fields updated: {list(payload.model_dump(exclude_none=True).keys())}",
        ))
        db.commit()
        db.refresh(s)
        return s
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not update supplier.")


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASE ORDERS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

po_router = APIRouter(prefix="/purchase-orders", tags=["Purchase Orders"])


def _generate_po_number(db: Session) -> str:
    count = db.query(func.count(PurchaseOrder.id)).scalar() or 0
    return f"PO-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{count + 1:04d}"


@po_router.post("", response_model=PurchaseOrderOut, status_code=status.HTTP_201_CREATED,
                summary="Create purchase order")
def create_po(
    payload: PurchaseOrderCreateIn,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        supplier = db.get(Supplier, payload.supplier_id)
        if not supplier or not supplier.is_active:
            raise HTTPException(status_code=404, detail="Supplier not found or inactive")

        po = PurchaseOrder(
            po_number=_generate_po_number(db),
            supplier_id=payload.supplier_id,
            raised_by_id=current_user.id,
            notes=payload.notes,
            status=POStatus.DRAFT,
        )
        db.add(po)
        db.flush()

        total = Decimal("0.00")
        for item in payload.items:
            med = db.get(Medicine, item.medicine_id)
            if not med:
                raise HTTPException(
                    status_code=404,
                    detail=f"Medicine id={item.medicine_id} not found",
                )
            line = Decimal(str(item.unit_cost)) * item.quantity_ordered
            db.add(POItem(
                purchase_order_id=po.id,
                medicine_id=item.medicine_id,
                quantity_ordered=item.quantity_ordered,
                quantity_received=0,
                unit_cost=item.unit_cost,
                line_total=line,
            ))
            total += line

        po.total_amount = total
        db.add(AuditLog(
            user_id=current_user.id,
            action="PO_CREATED",
            entity="PurchaseOrder",
            entity_id=po.id,
            detail=f"PO {po.po_number} created for supplier {supplier.name}",
        ))
        db.commit()
        db.refresh(po)
        return po

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not create purchase order.")


@po_router.get("", response_model=list[PurchaseOrderOut], summary="List purchase orders")
def list_pos(
    status_filter: POStatus = Query(None, alias="status"),
    _: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    q = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.items))
    if status_filter:
        q = q.filter(PurchaseOrder.status == status_filter)
    return q.order_by(PurchaseOrder.created_at.desc()).all()


@po_router.post("/{po_id}/receive", response_model=PurchaseOrderOut,
                summary="Mark PO as received")
def receive_po(
    po_id: int,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        po = (
            db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.items))
            .filter(PurchaseOrder.id == po_id)
            .first()
        )
        if not po:
            raise HTTPException(status_code=404, detail="Purchase order not found")
        if po.status == POStatus.RECEIVED:
            raise HTTPException(status_code=400, detail="PO already received")
        if po.status == POStatus.CANCELLED:
            raise HTTPException(status_code=400, detail="Cannot receive a cancelled PO")

        for item in po.items:
            item.quantity_received = item.quantity_ordered
            db.add(Inventory(
                medicine_id=item.medicine_id,
                supplier_id=po.supplier_id,
                quantity=item.quantity_ordered,
                unit_cost=item.unit_cost,
                selling_price=item.unit_cost * Decimal("1.30"),
            ))

        po.status = POStatus.RECEIVED
        po.received_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            user_id=current_user.id,
            action="PO_RECEIVED",
            entity="PurchaseOrder",
            entity_id=po.id,
            detail=f"PO {po.po_number} received",
        ))
        db.commit()
        db.refresh(po)
        return po

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise safe_error(e, "Could not recieve purchase order.")


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

alerts_router = APIRouter(prefix="/alerts", tags=["Alerts"])


@alerts_router.get("", response_model=list[AlertOut], summary="List alerts")
def list_alerts(
    is_resolved: bool = Query(False),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(MedicineAlert)
        .filter(MedicineAlert.is_resolved == is_resolved)
        .order_by(MedicineAlert.created_at.desc())
        .all()
    )


@alerts_router.post("/{alert_id}/resolve", response_model=AlertOut, summary="Resolve an alert")
def resolve_alert_endpoint(
    alert_id: int,
    current_user: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        return resolve_alert(db, alert_id, current_user.id)
    except HTTPException:
        raise
    except Exception as e:
        raise safe_error(e, "Could not resolve alert.")


@alerts_router.post("/scan", summary="Scan all inventory for alerts (admin)")
def trigger_alert_scan(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        expiry_count = scan_expiry_alerts(db)
        return {"message": f"Scan complete. {expiry_count} new expiry alert(s) created."}
    except Exception as e:
        raise safe_error(e, "Scan failed. Please try again.")


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

reports_router = APIRouter(prefix="/reports", tags=["Reports"])


@reports_router.get("/dashboard", response_model=DashboardOut, summary="Dashboard stats")
def dashboard(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        total_meds  = db.query(func.count(Medicine.id)).scalar() or 0
        active_meds = db.query(func.count(Medicine.id)).filter(Medicine.is_active == True).scalar() or 0

        low_stock_subq = (
            db.query(
                Inventory.medicine_id,
                func.sum(Inventory.quantity).label("total_qty"),
            )
            .group_by(Inventory.medicine_id)
            .subquery()
        )
        low_stock_count = (
            db.query(func.count(Medicine.id))
            .join(low_stock_subq, low_stock_subq.c.medicine_id == Medicine.id)
            .filter(
                low_stock_subq.c.total_qty <= Medicine.reorder_level,
                Medicine.is_active == True,
            )
            .scalar()
        ) or 0

        cutoff = datetime.now(timezone.utc) + timedelta(days=30)
        expiring_count = (
            db.query(func.count(func.distinct(Inventory.medicine_id)))
            .filter(Inventory.expires_at <= cutoff, Inventory.quantity > 0)
            .scalar()
        ) or 0

        today_sales = (
            db.query(
                func.count(Sale.id),
                func.coalesce(func.sum(Sale.total_amount), 0),
            )
            .filter(Sale.sold_at >= today_start, Sale.status == SaleStatus.COMPLETED)
            .first()
        )

        pending_pos = (
            db.query(func.count(PurchaseOrder.id))
            .filter(PurchaseOrder.status.in_([POStatus.DRAFT, POStatus.SENT]))
            .scalar()
        ) or 0

        unresolved_alerts = (
            db.query(func.count(MedicineAlert.id))
            .filter(MedicineAlert.is_resolved == False)
            .scalar()
        ) or 0

        return DashboardOut(
            total_medicines=total_meds,
            active_medicines=active_meds,
            low_stock_count=low_stock_count,
            expiring_soon_count=expiring_count,
            todays_sales_count=today_sales[0] or 0,
            todays_revenue=Decimal(str(today_sales[1] or 0)),
            pending_purchase_orders=pending_pos,
            unresolved_alerts=unresolved_alerts,
        )
    except Exception as e:
        raise safe_error(e,"Dashboard error. ")


@reports_router.get("/sales-summary", response_model=SalesSummaryOut,
                    summary="Revenue summary for a period")
def sales_summary(
    from_date: datetime = Query(...),
    to_date: datetime = Query(...),
    _: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        row = (
            db.query(
                func.count(Sale.id).label("count"),
                func.coalesce(func.sum(Sale.total_amount), 0).label("revenue"),
                func.coalesce(func.sum(Sale.discount_amount), 0).label("discount"),
                func.coalesce(func.sum(Sale.tax_amount), 0).label("tax"),
            )
            .filter(
                Sale.sold_at.between(from_date, to_date),
                Sale.status == SaleStatus.COMPLETED,
            )
            .first()
        )

        revenue  = Decimal(str(row.revenue))
        discount = Decimal(str(row.discount))
        tax      = Decimal(str(row.tax))

        return SalesSummaryOut(
            period=f"{from_date.date()} to {to_date.date()}",
            total_sales=row.count or 0,
            total_revenue=revenue,
            total_discount=discount,
            total_tax=tax,
            net_revenue=revenue - discount,
        )
    except Exception as e:
        raise safe_error(e, "Report error")


@reports_router.get("/top-medicines", response_model=list[TopMedicineOut],
                    summary="Best-selling medicines")
def top_medicines(
    from_date: datetime = Query(None),
    to_date: datetime = Query(None),
    limit: int = Query(10, ge=1, le=50),
    _: User = Depends(require_admin_or_pharmacist),
    db: Session = Depends(get_db),
):
    try:
        q = (
            db.query(
                Medicine.id.label("medicine_id"),
                Medicine.name.label("medicine_name"),
                func.coalesce(func.sum(SaleItem.quantity), 0).label("total_quantity_sold"),
                func.coalesce(func.sum(SaleItem.line_total), 0).label("total_revenue"),
            )
            .join(SaleItem, SaleItem.medicine_id == Medicine.id)
            .join(Sale, Sale.id == SaleItem.sale_id)
            .filter(Sale.status == SaleStatus.COMPLETED)
        )
        if from_date:
            q = q.filter(Sale.sold_at >= from_date)
        if to_date:
            q = q.filter(Sale.sold_at <= to_date)

        rows = (
            q.group_by(Medicine.id, Medicine.name)
            .order_by(func.sum(SaleItem.quantity).desc())
            .limit(limit)
            .all()
        )

        return [
            TopMedicineOut(
                medicine_id=r.medicine_id,
                medicine_name=r.medicine_name,
                total_quantity_sold=r.total_quantity_sold,
                total_revenue=Decimal(str(r.total_revenue)),
            )
            for r in rows
        ]
    except Exception as e:
        raise safe_error(e, "Report error")


@reports_router.get("/audit-log", summary="Audit trail (admin only)")
def audit_log(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    action: str = Query(None),
    user_id: int = Query(None),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        q = db.query(AuditLog)
        if action:
            q = q.filter(AuditLog.action.ilike(f"%{action}%"))
        if user_id:
            q = q.filter(AuditLog.user_id == user_id)
        return q.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit).all()
    except Exception as e:
        raise safe_error(e,"Could not fetch audit log")