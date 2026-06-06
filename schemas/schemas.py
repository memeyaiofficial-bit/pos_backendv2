"""
schemas/schemas.py
──────────────────
Pydantic v2 models for request validation and response serialisation.

STRUCTURE:
  Each domain has a Base (shared fields), Create (input), Update (partial input),
  and Out (response) schema.

RISKS MITIGATED:
  • Passwords never appear in Out schemas (no accidental leakage).
  • from_attributes=True enables ORM → Pydantic conversion without extra code.
  • Field constraints (min_length, ge=0) validate data before it hits the DB.
  • EmailStr validates e-mail format at the boundary, not inside business logic.
"""

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator,ConfigDict

from models.orm import AlertType, PaymentMethod, POStatus, SaleStatus, UserRole
from models.orm import MpesaStatus


# ── Shared config ─────────────────────────────────────────────────────────────

class _OrmBase(BaseModel):
    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class TokenRefreshIn(BaseModel):
    refresh_token: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


# ══════════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════════

class UserBase(_OrmBase):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    role: UserRole = UserRole.CASHIER
    phone: Optional[str] = None


class UserCreateIn(UserBase):
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """
        Enforce basic password complexity to reduce brute-force risk.
        At least one uppercase, one lowercase, one digit.
        """
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserUpdateIn(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    phone: Optional[str] = None
    is_active: Optional[bool] = None
    role: Optional[UserRole] = None


class UserOut(UserBase):
    id: int
    is_active: bool
    created_at: datetime
    last_login: Optional[datetime] = None


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def complexity(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


# ══════════════════════════════════════════════════════════════════════════════
# MEDICINES
# ══════════════════════════════════════════════════════════════════════════════

class MedicineBase(_OrmBase):
    name: str = Field(min_length=1, max_length=300)
    generic_name: Optional[str] = None
    brand_name: Optional[str] = None
    barcode: Optional[str] = None
    atc_code: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    route: Optional[str] = None
    manufacturer: Optional[str] = None
    description: Optional[str] = None
    requires_prescription: bool = False
    is_controlled: bool = False
    reorder_level: int = Field(default=10, ge=0)
    unit_price: Decimal = Field(ge=Decimal("0.00"))


class MedicineCreateIn(MedicineBase):
    pass


class MedicineUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=300)
    generic_name: Optional[str] = None
    brand_name: Optional[str] = None
    barcode: Optional[str] = None
    dosage_form: Optional[str] = None
    strength: Optional[str] = None
    unit_price: Optional[Decimal] = Field(default=None, ge=Decimal("0.00"))
    requires_prescription: Optional[bool] = None
    is_controlled: Optional[bool] = None
    reorder_level: Optional[int] = Field(default=None, ge=0)
    is_active: Optional[bool] = None


class MedicineOut(MedicineBase):
    id: int
    who_eml_code: Optional[str] = None
    openfda_id: Optional[str] = None
    source: str
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime] = None


class MedicineSearchOut(BaseModel):
    """Lightweight response for search / autocomplete endpoints."""
    id: int
    name: str
    generic_name: Optional[str]
    dosage_form: Optional[str]
    strength: Optional[str]
    unit_price: Decimal
    requires_prescription: bool
    is_active: bool
    reorder_level: int
    source: str
    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════════════
# INVENTORY
# ══════════════════════════════════════════════════════════════════════════════

class InventoryCreateIn(BaseModel):
    medicine_id: int
    supplier_id: Optional[int] = None
    batch_number: Optional[str] = None
    quantity: int = Field(ge=1)
    unit_cost: Decimal = Field(ge=Decimal("0.01"))
    selling_price: Decimal = Field(ge=Decimal("0.01"))
    manufactured_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def selling_price_exceeds_cost(self):
        if self.selling_price < self.unit_cost:
            raise ValueError("selling_price must be >= unit_cost (margin cannot be negative)")
        return self


class InventoryUpdateIn(BaseModel):
    quantity: Optional[int] = Field(default=None, ge=0)
    selling_price: Optional[Decimal] = Field(default=None, ge=Decimal("0.01"))
    expires_at: Optional[datetime] = None
    notes: Optional[str] = None


class InventoryOut(_OrmBase):
    id: int
    medicine_id: int
    supplier_id: Optional[int]
    batch_number: Optional[str]
    quantity: int
    unit_cost: Decimal
    selling_price: Decimal
    expires_at: Optional[datetime]
    received_at: datetime


class InventorySummaryOut(BaseModel):
    """Total stock across all batches for a medicine."""
    medicine_id: int
    medicine_name: str
    total_quantity: int
    lowest_selling_price: Optional[Decimal]
    earliest_expiry: Optional[datetime]
    model_config = {"from_attributes": True}


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ══════════════════════════════════════════════════════════════════════════════

class CustomerBase(_OrmBase):
    full_name: str = Field(min_length=2, max_length=200)
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    date_of_birth: Optional[datetime] = None
    id_number: Optional[str] = None
    insurance_no: Optional[str] = None
    notes: Optional[str] = None


class CustomerCreateIn(CustomerBase):
    pass


class CustomerUpdateIn(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    insurance_no: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class CustomerOut(CustomerBase):
    id: int
    loyalty_points: int
    is_active: bool
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# SALES
# ══════════════════════════════════════════════════════════════════════════════

class SaleItemIn(BaseModel):
    medicine_id: int
    quantity: int = Field(ge=1)
    discount_pct: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"), le=Decimal("100.00"))


class SaleCreateIn(BaseModel):
    customer_id: Optional[int] = None
    items: List[SaleItemIn] = Field(min_length=1)
    payment_method: PaymentMethod = PaymentMethod.CASH
    amount_paid: Decimal = Field(ge=Decimal("0.00"))
    discount_amount: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0.00"))
    notes: Optional[str] = None
    mpesa_receipt: Optional[str] = None
    # Prescription data (only required for prescription medicines)
    prescription: Optional["PrescriptionCreateIn"] = None


class SaleItemOut(_OrmBase):
    id: int
    medicine_id: int
    quantity: int
    unit_price: Decimal
    discount_pct: Decimal
    line_total: Decimal


class SaleOut(_OrmBase):
    id: int
    receipt_number: str
    cashier_id: Optional[int]
    customer_id: Optional[int]
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    amount_paid: Decimal
    change_given: Decimal
    payment_method: PaymentMethod
    status: SaleStatus
    notes: Optional[str]
    sold_at: datetime
    items: List[SaleItemOut] = []


class VoidSaleIn(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


# ══════════════════════════════════════════════════════════════════════════════
# PRESCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

class PrescriptionCreateIn(BaseModel):
    prescriber_name: Optional[str] = None
    prescriber_reg: Optional[str] = None
    facility: Optional[str] = None
    issued_date: Optional[datetime] = None
    expiry_date: Optional[datetime] = None
    notes: Optional[str] = None


class PrescriptionOut(_OrmBase):
    id: int
    sale_id: int
    prescriber_name: Optional[str]
    prescriber_reg: Optional[str]
    facility: Optional[str]
    issued_date: Optional[datetime]
    expiry_date: Optional[datetime]
    verified: bool
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ══════════════════════════════════════════════════════════════════════════════

class SupplierBase(_OrmBase):
    name: str = Field(min_length=2, max_length=200)
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[str] = None


class SupplierCreateIn(SupplierBase):
    pass


class SupplierUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    address: Optional[str] = None
    is_active: Optional[bool] = None


class SupplierOut(SupplierBase):
    id: int
    is_active: bool
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# PURCHASE ORDERS
# ══════════════════════════════════════════════════════════════════════════════

class POItemIn(BaseModel):
    medicine_id: int
    quantity_ordered: int = Field(ge=1)
    unit_cost: Decimal = Field(ge=Decimal("0.01"))


class PurchaseOrderCreateIn(BaseModel):
    supplier_id: int
    items: List[POItemIn] = Field(min_length=1)
    notes: Optional[str] = None


class POItemOut(_OrmBase):
    id: int
    medicine_id: int
    quantity_ordered: int
    quantity_received: int
    unit_cost: Decimal
    line_total: Decimal


class PurchaseOrderOut(_OrmBase):
    id: int
    po_number: str
    supplier_id: int
    status: POStatus
    total_amount: Decimal
    notes: Optional[str]
    ordered_at: Optional[datetime]
    received_at: Optional[datetime]
    created_at: datetime
    items: List[POItemOut] = []


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

class AlertOut(_OrmBase):
    id: int
    medicine_id: int
    alert_type: AlertType
    message: str
    is_resolved: bool
    created_at: datetime


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

class SalesSummaryOut(BaseModel):
    period: str
    total_sales: int
    total_revenue: Decimal
    total_discount: Decimal
    total_tax: Decimal
    net_revenue: Decimal


class TopMedicineOut(BaseModel):
    medicine_id: int
    medicine_name: str
    total_quantity_sold: int
    total_revenue: Decimal


class DashboardOut(BaseModel):
    total_medicines: int
    active_medicines: int
    low_stock_count: int
    expiring_soon_count: int     # expiry within 30 days
    todays_sales_count: int
    todays_revenue: Decimal
    pending_purchase_orders: int
    unresolved_alerts: int


# ══════════════════════════════════════════════════════════════════════════════
# WHO / OPENFDA MEDICINE SYNC
# ══════════════════════════════════════════════════════════════════════════════

class MedicineSyncResultOut(BaseModel):
    imported: int
    skipped: int
    errors: int
    message: str


# Resolve forward reference in SaleCreateIn
SaleCreateIn.model_rebuild()

# ══════════════════════════════════════════════════════════════════════════════
# M-PESA
# ══════════════════════════════════════════════════════════════════════════════
class MpesaSTKPushIn(BaseModel):
    sale_id: int
    phone_number: str    # Accepts 07XX, +2547XX, 2547XX

class MpesaSTKPushOut(BaseModel):
    checkout_request_id: str
    message: str

class MpesaStatusOut(BaseModel):
    checkout_request_id: str
    status: MpesaStatus
    mpesa_receipt: Optional[str] = None
    amount: int
    result_desc: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)