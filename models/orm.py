"""
models/orm.py
─────────────
SQLAlchemy ORM models that map to SQLite tables.

TABLE OVERVIEW
──────────────
  users            – Staff accounts (admin / pharmacist / cashier)
  audit_logs       – Immutable record of every sensitive action
  medicines        – Product catalogue seeded from WHO / OpenFDA data
  suppliers        – Vendors that supply medicines
  inventory        – Stock levels per medicine per batch
  customers        – Patient / client records
  sales            – One row per POS transaction (receipt header)
  sale_items       – Line items within a sale
  prescriptions    – Linked to a sale; stores prescription metadata
  purchase_orders  – Restock orders raised to suppliers
  po_items         – Line items within a purchase order
  medicine_alerts  – Low-stock / expiry warnings

RISKS MITIGATED:
  • All monetary columns use Numeric(10,2) – avoids float rounding errors.
  • Soft-delete pattern (is_active flag) – preserves audit trail, no data loss.
  • server_default=func.now() on created_at – DB sets time, not application layer
    (avoids clock-skew bugs in multi-process deploys).
  • Cascade delete rules explicit – no orphaned rows.
  • Indexes on high-frequency query columns (barcode, email, sale date).
"""

from datetime import datetime,timezone
from typing import Optional
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, ForeignKey, Index,
    Integer, Numeric, String, Text, func,
)
from sqlalchemy.orm import relationship, Mapped, mapped_column

from database import Base
import enum

# ══════════════════════════════════════════════════════════════════════════════
# ENUMERATIONS
# ══════════════════════════════════════════════════════════════════════════════

class UserRole(str, PyEnum):
    ADMIN       = "admin"        # Full access
    PHARMACIST  = "pharmacist"   # Can dispense, view prescriptions
    CASHIER     = "cashier"      # Can process sales, no admin routes


class SaleStatus(str, PyEnum):
    PENDING    = "pending"
    COMPLETED  = "completed"
    VOIDED     = "voided"
    REFUNDED   = "refunded"


class PaymentMethod(str, PyEnum):
    CASH   = "cash"
    MPESA  = "mpesa"       # Mobile money (Kenya / East Africa)
    CARD   = "card"
    CREDIT = "credit"      # On-account for known customers


class POStatus(str, PyEnum):
    DRAFT     = "draft"
    SENT      = "sent"
    RECEIVED  = "received"
    CANCELLED = "cancelled"


class AlertType(str, PyEnum):
    LOW_STOCK  = "low_stock"
    EXPIRY     = "expiry"
    OUT_OF_STOCK = "out_of_stock"


# ══════════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════════

class User(Base):
    """
    Staff accounts.  Passwords are stored as bcrypt hashes – never plain text.
    role determines which endpoints and actions are permitted (RBAC).
    """
    __tablename__ = "users"

    id               = Column(Integer, primary_key=True, index=True)
    full_name        = Column(String(120), nullable=False)
    email            = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password  = Column(String(255), nullable=False)
    role             = Column(Enum(UserRole), nullable=False, default=UserRole.CASHIER)
    is_active        = Column(Boolean, default=True, nullable=False)
    phone            = Column(String(20), nullable=True)
    created_at       = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at       = Column(DateTime, server_default=func.now(), onupdate=func.now())
    last_login       = Column(DateTime, nullable=True)

    must_change_credentials = Column(Boolean, default=False, nullable=False)

    # Relationships
    sales      = relationship("Sale", foreign_keys="[Sale.cashier_id]", back_populates="cashier")
    audit_logs = relationship("AuditLog", back_populates="user")

    def __repr__(self):
        return f"<User id={self.id} email={self.email} role={self.role}>"


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG  (append-only – never update or delete rows here)
# ══════════════════════════════════════════════════════════════════════════════

class AuditLog(Base):
    """
    Immutable record of every sensitive operation.
    Satisfies pharmacy regulatory requirements for traceability.
    """
    __tablename__ = "audit_logs"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action      = Column(String(100), nullable=False)   # e.g. "SALE_CREATED"
    entity      = Column(String(100), nullable=True)    # e.g. "Sale"
    entity_id   = Column(Integer, nullable=True)
    detail      = Column(Text, nullable=True)           # JSON or human-readable summary
    ip_address  = Column(String(45), nullable=True)     # IPv4 or IPv6
    created_at  = Column(DateTime, server_default=func.now(), nullable=False, index=True)

    user = relationship("User", back_populates="audit_logs")


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ══════════════════════════════════════════════════════════════════════════════

class Supplier(Base):
    """Medicine vendors / distributors."""
    __tablename__ = "suppliers"

    id           = Column(Integer, primary_key=True, index=True)
    name         = Column(String(200), nullable=False, unique=True)
    contact_name = Column(String(120), nullable=True)
    phone        = Column(String(20), nullable=True)
    email        = Column(String(255), nullable=True)
    address      = Column(Text, nullable=True)
    is_active    = Column(Boolean, default=True, nullable=False)
    created_at   = Column(DateTime, server_default=func.now())

    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")
    inventory_items = relationship("Inventory", back_populates="supplier")


# ══════════════════════════════════════════════════════════════════════════════
# MEDICINES  (product catalogue)
# ══════════════════════════════════════════════════════════════════════════════

class Medicine(Base):
    """
    Master product catalogue.  Seeded from WHO EML / OpenFDA; extended locally.

    who_eml_code   → WHO Essential Medicines List code (may be null for non-EML drugs)
    openfda_id     → NDC or application number from openFDA (deduplication key)
    atc_code       → Anatomical Therapeutic Chemical classification (WHO standard)
    """
    __tablename__ = "medicines"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(300), nullable=False, index=True)
    generic_name    = Column(String(300), nullable=True)
    brand_name      = Column(String(300), nullable=True)
    barcode         = Column(String(100), unique=True, nullable=True, index=True)
    atc_code        = Column(String(20), nullable=True, index=True)
    who_eml_code    = Column(String(50), nullable=True)
    openfda_id      = Column(String(100), unique=True, nullable=True)  # dedup key
    dosage_form     = Column(String(100), nullable=True)    # tablet, syrup, injection…
    strength        = Column(String(100), nullable=True)    # e.g. "500 mg"
    route           = Column(String(100), nullable=True)    # oral, topical, IV…
    manufacturer    = Column(String(200), nullable=True)
    description     = Column(Text, nullable=True)
    requires_prescription = Column(Boolean, default=False, nullable=False)
    is_controlled   = Column(Boolean, default=False, nullable=False)  # controlled substance
    is_active       = Column(Boolean, default=True, nullable=False)
    reorder_level   = Column(Integer, default=10, nullable=False)  # trigger alert below this
    unit_price      = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    # Source tracking
    source          = Column(String(50), default="manual")  # "who_eml", "openfda", "manual"
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())

    inventory   = relationship("Inventory",  back_populates="medicine", cascade="all, delete-orphan")
    sale_items  = relationship("SaleItem",   back_populates="medicine")
    po_items    = relationship("POItem",     back_populates="medicine")
    alerts      = relationship("MedicineAlert", back_populates="medicine", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_medicines_name_generic", "name", "generic_name"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY  (stock batches)
# ══════════════════════════════════════════════════════════════════════════════

class Inventory(Base):
    """
    One row per batch of a medicine.
    Tracking per batch (not just per medicine) enables:
      • FEFO (First-Expiry-First-Out) dispensing.
      • Recall traceability by batch number.
    """
    __tablename__ = "inventory"

    id              = Column(Integer, primary_key=True, index=True)
    medicine_id     = Column(Integer, ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id     = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    batch_number    = Column(String(100), nullable=True)
    quantity        = Column(Integer, nullable=False, default=0)
    unit_cost       = Column(Numeric(10, 2), nullable=False)  # Purchase cost
    selling_price   = Column(Numeric(10, 2), nullable=False)  # Selling price this batch
    manufactured_at = Column(DateTime, nullable=True)
    expires_at      = Column(DateTime, nullable=True, index=True)  # For expiry alerts
    received_at     = Column(DateTime, server_default=func.now())
    notes           = Column(Text, nullable=True)

    medicine = relationship("Medicine",  back_populates="inventory")
    supplier = relationship("Supplier",  back_populates="inventory_items")

    __table_args__ = (
        Index("ix_inventory_expires", "expires_at", "medicine_id"),
    )


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ══════════════════════════════════════════════════════════════════════════════

class Customer(Base):
    """Patient / client registry."""
    __tablename__ = "customers"

    id           = Column(Integer, primary_key=True, index=True)
    full_name    = Column(String(200), nullable=False)
    phone        = Column(String(20), nullable=True, unique=True)
    email        = Column(String(255), nullable=True)
    date_of_birth = Column(DateTime, nullable=True)
    id_number    = Column(String(50), nullable=True)   # National ID / passport
    insurance_no = Column(String(100), nullable=True)
    notes        = Column(Text, nullable=True)
    loyalty_points = Column(Integer, default=0, nullable=False)
    is_active    = Column(Boolean, default=True, nullable=False)
    created_at   = Column(DateTime, server_default=func.now())

    sales         = relationship("Sale",         back_populates="customer")
    prescriptions = relationship("Prescription", back_populates="customer")


# ══════════════════════════════════════════════════════════════════════════════
# SALES (receipt header)
# ══════════════════════════════════════════════════════════════════════════════

class Sale(Base):
    """
    One row per POS transaction.  Items are in SaleItem.

    receipt_number → human-readable, e.g. RX-20240601-0001 (generated in service layer).
    discount_amount → flat discount applied to whole sale (before tax).
    tax_amount      → VAT / applicable tax.
    total_amount    → final amount paid.
    """
    __tablename__ = "sales"

    id              = Column(Integer, primary_key=True, index=True)
    receipt_number  = Column(String(50), unique=True, nullable=False, index=True)
    cashier_id      = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    customer_id     = Column(Integer, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    subtotal        = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    discount_amount = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    tax_amount      = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    total_amount    = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    amount_paid     = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    change_given    = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    payment_method  = Column(Enum(PaymentMethod), nullable=False, default=PaymentMethod.CASH)
    status          = Column(Enum(SaleStatus), nullable=False, default=SaleStatus.PENDING)
    notes           = Column(Text, nullable=True)
    sold_at         = Column(DateTime, server_default=func.now(), index=True)
    voided_at       = Column(DateTime, nullable=True)
    voided_by_id    = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    void_reason     = Column(Text, nullable=True)

    cashier      = relationship("User", foreign_keys="[Sale.cashier_id]", back_populates="sales")
    voided_by    = relationship("User", foreign_keys="[Sale.voided_by_id]")
    customer     = relationship("Customer", back_populates="sales")
    items        = relationship("SaleItem",     back_populates="sale", cascade="all, delete-orphan")
    prescription = relationship("Prescription", back_populates="sale", uselist=False)
    mpesa_transactions: Mapped[list["MpesaTransaction"]] = relationship(
        back_populates="sale")

    __table_args__ = (
        Index("ix_sales_sold_at_status", "sold_at", "status"),
    )


class SaleItem(Base):
    """Line items within a sale (one row per medicine dispensed)."""
    __tablename__ = "sale_items"

    id           = Column(Integer, primary_key=True, index=True)
    sale_id      = Column(Integer, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, index=True)
    medicine_id  = Column(Integer, ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    inventory_id = Column(Integer, ForeignKey("inventory.id", ondelete="SET NULL"), nullable=True)  # batch ref
    quantity     = Column(Integer, nullable=False)
    unit_price   = Column(Numeric(10, 2), nullable=False)   # Price at time of sale (snapshot)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=Decimal("0.00"))
    line_total   = Column(Numeric(10, 2), nullable=False)

    sale     = relationship("Sale",      back_populates="items")
    medicine = relationship("Medicine",  back_populates="sale_items")


# ══════════════════════════════════════════════════════════════════════════════
# PRESCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

class Prescription(Base):
    """
    Prescription details linked to a sale.
    Stored separately to satisfy regulatory requirements and enable future
    digital prescription verification.
    """
    __tablename__ = "prescriptions"

    id              = Column(Integer, primary_key=True, index=True)
    sale_id         = Column(Integer, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False, unique=True)
    customer_id     = Column(Integer, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    prescriber_name = Column(String(200), nullable=True)
    prescriber_reg  = Column(String(100), nullable=True)  # Medical registration number
    facility        = Column(String(200), nullable=True)  # Hospital / clinic
    issued_date     = Column(DateTime, nullable=True)
    expiry_date     = Column(DateTime, nullable=True)
    notes           = Column(Text, nullable=True)
    image_path      = Column(String(500), nullable=True)  # Scanned prescription
    verified        = Column(Boolean, default=False, nullable=False)
    verified_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at      = Column(DateTime, server_default=func.now())

    sale = relationship("Sale", back_populates="prescription")
    customer = relationship("Customer", back_populates="prescriptions")
    verified_by = relationship("User", foreign_keys="[Prescription.verified_by_id]")

# ══════════════════════════════════════════════════════════════════════════════
# PURCHASE ORDERS
# ══════════════════════════════════════════════════════════════════════════════

class PurchaseOrder(Base):
    """Restock orders raised to suppliers."""
    __tablename__ = "purchase_orders"

    id            = Column(Integer, primary_key=True, index=True)
    po_number     = Column(String(50), unique=True, nullable=False, index=True)
    supplier_id   = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    raised_by_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    status        = Column(Enum(POStatus), nullable=False, default=POStatus.DRAFT)
    total_amount  = Column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    notes         = Column(Text, nullable=True)
    ordered_at    = Column(DateTime, nullable=True)
    received_at   = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, server_default=func.now())

    supplier  = relationship("Supplier", back_populates="purchase_orders")
    raised_by = relationship("User", foreign_keys="[PurchaseOrder.raised_by_id]")
    items     = relationship("POItem", back_populates="purchase_order", cascade="all, delete-orphan")


class POItem(Base):
    """Line items within a purchase order."""
    __tablename__ = "po_items"

    id                  = Column(Integer, primary_key=True, index=True)
    purchase_order_id   = Column(Integer, ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    medicine_id         = Column(Integer, ForeignKey("medicines.id", ondelete="RESTRICT"), nullable=False)
    quantity_ordered    = Column(Integer, nullable=False)
    quantity_received   = Column(Integer, nullable=False, default=0)
    unit_cost           = Column(Numeric(10, 2), nullable=False)
    line_total          = Column(Numeric(10, 2), nullable=False)

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    medicine       = relationship("Medicine",      back_populates="po_items")


# ══════════════════════════════════════════════════════════════════════════════
# MEDICINE ALERTS
# ══════════════════════════════════════════════════════════════════════════════

class MedicineAlert(Base):
    """
    System-generated alerts for low stock and approaching expiry dates.
    Resolved alerts are kept for audit; is_resolved flag used for filtering.
    """
    __tablename__ = "medicine_alerts"

    id           = Column(Integer, primary_key=True, index=True)
    medicine_id  = Column(Integer, ForeignKey("medicines.id", ondelete="CASCADE"), nullable=False, index=True)
    alert_type   = Column(Enum(AlertType), nullable=False)
    message      = Column(Text, nullable=False)
    is_resolved  = Column(Boolean, default=False, nullable=False)
    resolved_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at  = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, server_default=func.now(), index=True)

    medicine = relationship("Medicine", back_populates="alerts")
    resolved_by = relationship("User", foreign_keys="[MedicineAlert.resolved_by_id]")

# ══════════════════════════════════════════════════════════════════════════════
# M-PESA
# ══════════════════════════════════════════════════════════════════════════════
class MpesaStatus(str, enum.Enum):
    PENDING   = "PENDING"    # STK push sent, waiting for customer
    SUCCESS   = "SUCCESS"    # Customer paid
    FAILED    = "FAILED"     # Customer cancelled, timeout, wrong PIN
    CANCELLED = "CANCELLED"  # Manually cancelled

class MpesaTransaction(Base):
    __tablename__ = "mpesa_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    sale_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("sales.id"), nullable=True, index=True
    )
    checkout_request_id: Mapped[str] = mapped_column(
        String(100), unique=True, index=True
    )
    merchant_request_id: Mapped[str] = mapped_column(String(100))
    phone_number: Mapped[str] = mapped_column(String(15))
    amount: Mapped[int] = mapped_column(Integer)

    mpesa_receipt: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_desc: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    status: Mapped[MpesaStatus] = mapped_column(
        Enum(MpesaStatus), default=MpesaStatus.PENDING
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    sale: Mapped[Optional["Sale"]] = relationship(back_populates="mpesa_transactions")