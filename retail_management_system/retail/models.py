from django.conf import settings
from django.db import models
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import Q

from decimal import Decimal 
from django.db.models import Sum

# Core catalog and inventory models.


# Product category master table.
class Category(models.Model):
    category_name = models.CharField(max_length=120, unique=True)

    def __str__(self):
        return self.category_name

# Tracks the last SKU sequence used for each section-category pair.
class SKUCounter(models.Model):
    section = models.CharField(max_length=10)  # stores "men/women/kids/unisex"
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    last_seq = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("section", "category")

    def __str__(self):
        return f"{self.section} - {self.category.category_name} = {self.last_seq}"

# Main product record used across inventory, sales, and forecasting.
class Product(models.Model):
    class Meta:  # Prevents duplicate product identities with the same key fields.
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
    supplier = models.ForeignKey(
        "Supplier",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    opening_stock = models.PositiveIntegerField(default=0)
    qty_in_stock = models.PositiveIntegerField(default=0)
    section = models.CharField(max_length=10, choices=Section.choices, default=Section.UNISEX)

    selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    reorder_level = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product_name} ({self.sku})"

# Stores every stock movement that changes product quantity.
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
    qty_moved = models.PositiveIntegerField()  # Quantity is always stored as a positive number.

    # Used only for ADJUST movements.
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
    
# Sales models.
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

# One line item inside a sale.
class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_items")

    qty = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    discount_amt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    promo_flag = models.BooleanField(default=False)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        # Recalculates discount values and final line total before saving.
        gross = self.unit_price * Decimal(self.qty)
        if self.discount_amt and self.discount_amt > 0:
            if self.discount_amt > gross:
                self.discount_amt = gross
            self.discount_pct = (self.discount_amt / gross * Decimal("100")) if gross > 0 else Decimal("0.00")
        else:
            self.discount_amt = (gross * self.discount_pct) / Decimal("100")
        self.promo_flag = bool(self.discount_amt and self.discount_amt > 0)
        self.line_total = gross - self.discount_amt
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sale.receipt_no} - {self.product.sku}"

# Stores the app-specific role linked to a Django user.
class UserProfile(models.Model):
    ROLE_CHOICES = (
        ("admin", "Admin"),
        ("staff", "Staff"),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="staff")

    def __str__(self):
        return f"{self.user.username} ({self.role})"    


# Stores remembered devices used to skip repeated 2FA verification.
class TrustedDevice(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trusted_devices")
    device_id = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64)

    user_agent = models.TextField(blank=True)
    ip_at_creation = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "device_id"], name="uniq_trusted_device_per_user"),
        ]
        indexes = [
            models.Index(fields=["user", "device_id"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.device_id}"
    
# Supplier master data used by reorder and purchase-order flows.
class Supplier(models.Model):
    supplier_name = models.CharField(max_length=150, unique=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    lead_time_days = models.PositiveIntegerField(default=0)  # 0 means no lead time is set yet.
    is_active = models.BooleanField(default=True)
    
    contact_person = models.CharField(max_length=120, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    products_services = models.TextField(blank=True, null=True)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="suppliers",
    )
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="suppliers_multi",
    )


    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


    def __str__(self):
        return self.supplier_name
    
# Links products to suppliers with optional cost and lead-time overrides.
class ProductSupplier(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_links")
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="product_links")
    last_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    lead_time_days = models.PositiveIntegerField(null=True, blank=True)  # Overrides the supplier default when provided.
    is_primary = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "supplier"], name="uniq_product_supplier"),
            models.UniqueConstraint(
                fields=["product"],
                condition=Q(is_primary=True),
                name="uniq_primary_supplier_per_product",
            ),
        ]
# Purchase-order header record.
class PurchaseOrder(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_PENDING = "PENDING"
    STATUS_RECEIVED = "RECEIVED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_PENDING, "Pending"),
        (STATUS_RECEIVED, "Received"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    supplier = models.ForeignKey("Supplier", on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PENDING)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    # Workflow timestamps for order lifecycle tracking.
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    # User-facing fields shown in the purchase-order screens.
    order_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    notes = models.TextField(blank=True)

    po_number = models.CharField(max_length=50, unique=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def total_qty_ordered(self):
        # Returns the summed ordered quantity across all PO items.
        return self.items.aggregate(total=Sum("qty_ordered"))["total"] or 0
    
    @property
    def total_qty_received(self):
        # Returns the summed received quantity across all PO items.
        return self.items.aggregate(total=Sum("qty_received"))["total"] or 0
    
    def __str__(self):
        return self.po_number or f"PO-{self.id}"
    


# Purchase-order line item record.
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

# Stores temporary snooze settings for reorder alerts on one product.(not applied in the project, saved for future improvements)
class ReorderSnooze(models.Model):
    product = models.OneToOneField("Product", on_delete=models.CASCADE, related_name="reorder_snooze")
    snoozed_until = models.DateTimeField(null=True, blank=True)
    dismissed_until = models.DateTimeField(null=True, blank=True)  # optional: “Cancel for now”
    updated_at = models.DateTimeField(auto_now=True)

# Reorder request header created before supplier ordering.
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
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"RR-{self.id} ({self.status})"

# One requested product inside a reorder request.
class ReorderRequestItem(models.Model):
    request = models.ForeignKey(ReorderRequest, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True)

    qty_requested = models.PositiveIntegerField(default=1)
    suggested_qty_snapshot = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("request", "product")

# Forecasting and analytics models.
class FestiveCalendar(models.Model):
    """
    Stores festival windows for sales uplift tagging and ML training features.
    """
    festival_name = models.CharField(max_length=80)
    start_date = models.DateField()
    end_date = models.DateField()
    festival_date = models.DateField(null=True, blank=True)  
    
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

# Daily product-level sales features used as forecast input data.
class DailyProductSales(models.Model):
    """
    Forecast input table (stored).
    One row per product per day, aggregated from Sale + SaleItem,
    enriched using FestiveCalendar.
    """
    date = models.DateField()
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="daily_sales")
    category = models.ForeignKey(
        "Category",
        on_delete=models.PROTECT,
        related_name="daily_product_sales",
        null=True,
        blank=True,
        db_index=False,
    )

    qty_sold = models.PositiveIntegerField(default=0)
    sales_amt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    promo_flag = models.BooleanField(default=False)  # Derived from sale-item or sale-level promotion data.
    is_festive = models.BooleanField(default=False)
    festival_name = models.CharField(max_length=80, blank=True, default="")

    product_active = models.BooleanField(default=True)
    in_stock_flag = models.BooleanField(null=True, blank=True)

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
            models.Index(fields=["category"], name="idx_dps_category_id"),
            models.Index(fields=["is_festive"]),
            models.Index(fields=["festival_name"]),
            models.Index(fields=["product_active"]),
            models.Index(fields=["in_stock_flag"]),
        ]

    def __str__(self):
        return f"{self.date} - {self.product.sku} - qty={self.qty_sold}"

# Maps each SKU to its forecast grouping label.
class SKUForecastGroupMap(models.Model):
    """
    Maps one SKU to one forecasting group label.
    Keep this separate from raw sales so SKU history stays intact.
    """
    sku = models.OneToOneField(
        "Product",
        to_field="sku",
        db_column="sku",
        on_delete=models.PROTECT,
        related_name="forecast_group_map",
    )
    forecast_group = models.CharField(max_length=120)
    active_from = models.DateField(null=True, blank=True)
    active_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sku_forecast_group_map"
        indexes = [
            models.Index(fields=["forecast_group"]),
            models.Index(fields=["is_active"]),
            models.Index(fields=["active_from", "active_to"]),
        ]

    def __str__(self):
        return f"{self.sku_id} -> {self.forecast_group}"

# Daily forecast-group level sales features.
class DailyGroupSales(models.Model):
    """
    Forecast-ready grouped daily features.
    One row per forecast_group per date.
    """
    date = models.DateField()
    forecast_group = models.CharField(max_length=120)

    qty_sold = models.PositiveIntegerField(default=0)
    sales_amt = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    promo_flag = models.BooleanField(default=False)
    is_festive = models.BooleanField(default=False)
    festival_name = models.CharField(max_length=80, blank=True, default="")

    product_active = models.BooleanField(default=True)
    in_stock_flag = models.BooleanField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "daily_group_sales"
        constraints = [
            models.UniqueConstraint(fields=["date", "forecast_group"], name="uniq_daily_group_sales"),
        ]
        indexes = [
            models.Index(fields=["date"]),
            models.Index(fields=["forecast_group", "date"]),
            models.Index(fields=["promo_flag"]),
            models.Index(fields=["is_festive"]),
            models.Index(fields=["festival_name"]),
            models.Index(fields=["product_active"]),
            models.Index(fields=["in_stock_flag"]),
        ]

    def __str__(self):
        return f"{self.date} - {self.forecast_group} - qty={self.qty_sold}"

# One forecasting job or model run.
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

# Forecast output for one product on one forecast date.
class ForecastDaily(models.Model):
    """
    Forecast output table (per day).
    One row per product per forecast date per ForecastRun.
    """
    run = models.ForeignKey(ForecastRun, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("Product", on_delete=models.PROTECT, related_name="forecasts")

    forecast_date = models.DateField()
    predicted_qty = models.DecimalField(max_digits=12, decimal_places=2)  # Stored as decimal to keep forecast output flexible.

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

# Chat thread used by the retail assistant feature.
class ChatThread(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_threads")
    title = models.CharField(max_length=160, default="New Chat")
    context_json = models.JSONField(default=dict, blank=True)
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "updated_at"]),
            models.Index(fields=["is_archived"]),
        ]
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user_id}:{self.title}"

# One message inside an assistant chat thread.
class ChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        SYSTEM = "system", "System"

    thread = models.ForeignKey(ChatThread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    payload_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["thread", "created_at"]),
            models.Index(fields=["role"]),
        ]
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.thread_id}:{self.role}"
