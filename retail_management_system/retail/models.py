from django.conf import settings
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import Q

from decimal import Decimal 
from django.db.models import Sum

# Create your models here.


class Category(models.Model):
    category_name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.category_name

class SKUCounter(models.Model):
    section = models.CharField(max_length=10)  # stores "men/women/kids/unisex"
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("section", "category")

    def __str__(self):
        return f"{self.section} - {self.category.category_name} = {self.last_seq}"


class Product(models.Model):
    class Meta: #uniqueness rule  to avoid duplicate rows in product table in db
        constraints = [
            models.UniqueConstraint(
                fields=["product_name", "category", "section", "selling_price"],
                name="uniq_product_identity",
            )
        ]

    class Section(models.TextChoices):
        MEN = "men", "Men"
        WOMEN = "women", "Women"
        KIDS = "kids", "Kids"
        UNISEX = "unisex", "Unisex"

    sku = models.CharField(max_length=40, unique=True)
    product_name = models.CharField(max_length=200)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    qty_in_stock = models.PositiveIntegerField(default=0)
    section = models.CharField(max_length=10, choices=Section.choices, default=Section.UNISEX)

    selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    reorder_level = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product_name} ({self.sku})"


class StockMovement(models.Model):
    class MovementType(models.TextChoices):
        IN_ = "IN", "IN"
        OUT = "OUT", "OUT"
        ADJUST = "ADJUST", "ADJUST"
        SALE = "SALE", "SALE"
        PO = "PO", "PO"   

    class AdjustDirection(models.TextChoices):
        INC = "INC", "Increase"
        DEC = "DEC", "Decrease"

    movement_date = models.DateTimeField(default=timezone.now)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="movements")
    movement_type = models.CharField(max_length=10, choices=MovementType.choices)
    qty_moved = models.PositiveIntegerField()  # always positive

    # Only for ADJUST
    adjust_direction = models.CharField(
        max_length=3, choices=AdjustDirection.choices, null=True, blank=True
    )

    reason = models.CharField(max_length=120, null=True, blank=True)
    comments = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reference = models.CharField(max_length=60, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["movement_date"]),
            models.Index(fields=["movement_type"]),
            models.Index(fields=["product", "movement_date"]),
        ]

    def __str__(self):
        return f"{self.movement_type} {self.product} {self.qty_moved}"
    
#sale and sale item models
class Sale(models.Model):   
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        MOBILE = "mobile", "Mobile Payment"
        OTHER = "other", "Other"
        JUICE = "juice", "Juice"

    receipt_no = models.CharField(max_length=40, unique=True)
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sales")

    customer_name = models.CharField(max_length=120, blank=True)
    customer_phone = models.CharField(max_length=30, blank=True)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    payment_method = models.CharField(max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH)
    sale_date = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(auto_now_add=True)

    promo_flag = models.BooleanField(default=False)

    def __str__(self):
        return self.receipt_no


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_items")

    qty = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    discount_amt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        gross = self.unit_price * Decimal(self.qty)
        if self.discount_amt and self.discount_amt > 0:
            if self.discount_amt > gross:
                self.discount_amt = gross
            self.discount_pct = (self.discount_amt / gross * Decimal("100")) if gross > 0 else Decimal("0.00")
        else:
            self.discount_amt = (gross * self.discount_pct) / Decimal("100")
        self.line_total = gross - self.discount_amt
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sale.receipt_no} - {self.product.sku}"

class UserProfile(models.Model):
    ROLE_CHOICES = (
        ("owner", "Owner"),
        ("staff", "Staff"),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="staff")

    def __str__(self):
        return f"{self.user.username} ({self.role})"    
    
class Supplier(models.Model):
    supplier_name = models.CharField(max_length=150, unique=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    lead_time_days = models.PositiveIntegerField(default=0)  # 0 = unknown / not set
    is_active = models.BooleanField(default=True)
    
    contact_person = models.CharField(max_length=120, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    products_services = models.TextField(blank=True, null=True)


    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    def __str__(self):
        return self.supplier_name
    
class ProductSupplier(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_links")
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="product_links")
    last_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)  # overrides supplier default if set
    is_primary = models.BooleanField(default=False)

    class Meta:
        #unique_together = ("product", "supplier")
        constraints = [
            models.UniqueConstraint(fields=["product", "supplier"], name="uniq_product_supplier"),
            models.UniqueConstraint(
                fields=["product"],
                condition=Q(is_primary=True),
                name="uniq_primary_supplier_per_product",
            ),
        ]
class PurchaseOrder(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_ORDERED = "ORDERED"
    STATUS_PARTIAL = "PARTIAL"
    STATUS_RECEIVED = "RECEIVED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_ORDERED, "Ordered"),
        (STATUS_PARTIAL, "Partially Received"),
        (STATUS_RECEIVED, "Received / Closed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    supplier = models.ForeignKey("Supplier", on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    # workflow timestamps (keep for later)
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    # UI fields you requested
    order_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    notes = models.TextField(blank=True)

    po_number = models.CharField(max_length=20, unique=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def total_qty_ordered(self):
        return self.items.aggregate(total=Sum("qty_ordered"))["total"] or 0
    
    @property
    def total_qty_received(self):
        return self.items.aggregate(total=Sum("qty_received"))["total"] or 0
    
    def __str__(self):
        return self.po_number or f"PO-{self.id}"
    



class PurchaseOrderItem(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("Product", on_delete=models.PROTECT)

    qty_ordered = models.PositiveIntegerField(default=1)
    qty_received = models.PositiveIntegerField(default=0)
    reorder_request = models.ForeignKey(
        "ReorderRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders"
    )
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("po", "product")

class ReorderSnooze(models.Model):
    product = models.OneToOneField("Product", on_delete=models.CASCADE, related_name="reorder_snooze")
    snoozed_until = models.DateTimeField(null=True, blank=True)
    dismissed_until = models.DateTimeField(null=True, blank=True)  # optional: “Cancel for now”
    updated_at = models.DateTimeField(auto_now=True)

class ReorderRequest(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_SUBMITTED = "SUBMITTED"
    STATUS_APPROVED = "APPROVED"
    STATUS_REJECTED = "REJECTED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reorder_requests")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    supplier = models.ForeignKey("Supplier", on_delete=models.PROTECT, null=True, blank=True)

    notes = models.TextField(blank=True)

    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="reorder_requests_decided"
    )
    decision_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"RR-{self.id} ({self.status})"


class ReorderRequestItem(models.Model):
    request = models.ForeignKey(ReorderRequest, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)

    qty_requested = models.PositiveIntegerField(default=1)
    suggested_qty_snapshot = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("request", "product")


# forecast and prediction models
class FestiveCalendar(models.Model):
    """
    Stores festival windows for sales uplift tagging and ML training features.
    """
    festival_name = models.CharField(max_length=80)
    start_date = models.DateField()
    end_date = models.DateField()
    festival_date = models.DateField(null=True, blank=True)  # optional but recommended

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "festive_calendar"
        indexes = [
            models.Index(fields=["start_date"]),
            models.Index(fields=["end_date"]),
            models.Index(fields=["start_date", "end_date"]),
            models.Index(fields=["festival_name"]),
        ]

    def __str__(self):
        return f"{self.festival_name} ({self.start_date} → {self.end_date})"


class DailyProductSales(models.Model):
    """
    Forecast input table (stored).
    One row per product per day, aggregated from Sale + SaleItem,
    enriched using FestiveCalendar.
    """
    date = models.DateField()
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="daily_sales")

    qty_sold = models.PositiveIntegerField(default=0)
    sales_amt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    promo_flag = models.BooleanField(default=False)  # derived from Sale.promo_flag
    is_festive = models.BooleanField(default=False)
    festival_name = models.CharField(max_length=80, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "daily_product_sales"
        constraints = [
            models.UniqueConstraint(fields=["date", "product"], name="uniq_daily_product_sales"),
        ]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["product", "date"]),
            models.Index(fields=["is_festive"]),
            models.Index(fields=["festival_name"]),
        ]

    def __str__(self):
        return f"{self.date} - {self.product.sku} - qty={self.qty_sold}"


class ForecastRun(models.Model):
    """
    One record per forecasting batch/run (model execution).
    """
    model_name = models.CharField(max_length=60, default="model_v1")
    period_start = models.DateField()
    period_end = models.DateField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forecast_run"
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["period_start", "period_end"]),
            models.Index(fields=["model_name"]),
        ]

    def __str__(self):
        return f"{self.model_name} ({self.period_start} → {self.period_end}) @ {self.created_at}"


class ForecastDaily(models.Model):
    """
    Forecast output table (per day).
    One row per product per forecast date per ForecastRun.
    """
    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="forecasts")

    forecast_date = models.DateField()
    predicted_qty = models.DecimalField(max_digits=12, decimal_places=2)  # keep decimal for ML output flexibility

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "forecast_daily"
        constraints = [
            models.UniqueConstraint(fields=["run", "product", "forecast_date"], name="uniq_forecast_run_product_day"),
        ]
        indexes = [
            models.Index(fields=["forecast_date"]),
            models.Index(fields=["product", "forecast_date"]),
            models.Index(fields=["run", "forecast_date"]),
        ]

    def __str__(self):
        return f"{self.product.sku} {self.forecast_date} -> {self.predicted_qty}"
