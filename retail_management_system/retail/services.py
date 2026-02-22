import uuid
import math
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.utils import timezone
from django.db.models import Q, Sum, F
from django.db.models.functions import Coalesce

from .models import StockMovement, Sale, SaleItem, Product, ProductSupplier


def apply_stock_movement(*, product, movement_type, qty, adjust_direction=None):
    if qty <= 0:
        raise ValidationError("Quantity must be positive.")

    if movement_type == "IN":
        delta = qty
    elif movement_type in ("OUT", "SALE"):
        delta = -qty
    elif movement_type == "ADJUST":
        if adjust_direction == "INC":
            delta = qty
        elif adjust_direction == "DEC":
            delta = -qty
        else:
            raise ValidationError("ADJUST requires INC or DEC.")
    else:
        raise ValidationError("Invalid movement type.")

    if product.qty_in_stock + delta < 0:
        raise ValidationError("Insufficient stock.")

    product.qty_in_stock += delta
    product.save(update_fields=["qty_in_stock", "updated_at"])


def generate_receipt_no(*, sale_date=None):
    sale_date = sale_date or timezone.localdate()
    prefix = f"R{sale_date.strftime('%y%m%d')}"
    last = (
        Sale.objects
        .filter(sale_date=sale_date, receipt_no__startswith=prefix)
        .order_by("-receipt_no")
        .first()
    )
    if last and last.receipt_no and last.receipt_no[len(prefix):].isdigit():
        seq = int(last.receipt_no[len(prefix):]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:05d}"

@transaction.atomic
def record_stock_movement(*, product, movement_type, qty, user, reference=None, comments=None, reason=None, adjust_direction=None):
    # Updates Product.qty_in_stock
    apply_stock_movement(
        product=product,
        movement_type=movement_type,
        qty=qty,
        adjust_direction=adjust_direction,
    )

    # Writes StockMovement row
    StockMovement.objects.create(
        product=product,
        movement_type=movement_type,
        qty_moved=qty,
        adjust_direction=adjust_direction,
        reason=reason,
        comments=comments,
        created_by=user,
        reference=reference,
    )

@transaction.atomic
def create_sale(*, cashier, customer_name, customer_phone, discount, payment_method, cart_items):
    """
    cart_items = [{"product_id": 1, "qty": 2, "unit_price": "45.00"}, ...]
    """
    if not cart_items:
        raise ValidationError("Cart is empty.")

    product_ids = [int(i["product_id"]) for i in cart_items]
    products = Product.objects.select_for_update().filter(id__in=product_ids, is_active=True)
    product_map = {p.id: p for p in products}

    for item in cart_items:
        pid = int(item["product_id"])
        qty = int(item["qty"])

        if qty <= 0:
            raise ValidationError("Quantity must be at least 1.")

        p = product_map.get(pid)
        if not p:
            raise ValidationError("Invalid product in cart.")

        if p.qty_in_stock < qty:
            raise ValidationError(f"Insufficient stock for {p.sku}. Only {p.qty_in_stock} left.")

    sale = Sale.objects.create(
        receipt_no=generate_receipt_no(),
        cashier=cashier,
        customer_name=(customer_name or "").strip(),
        customer_phone=(customer_phone or "").strip(),
        discount=Decimal("0.00"),
        payment_method=payment_method or "cash",
    )

    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")

    for item in cart_items:
        pid = int(item["product_id"])
        qty = int(item["qty"])
        p = product_map[pid]

        unit_price = Decimal(str(item.get("unit_price") or p.selling_price))
        gross = unit_price * qty
        subtotal += gross

        discount_pct = Decimal(str(item.get("discount_pct") or "0.00"))
        if discount_pct < 0:
            discount_pct = Decimal("0.00")
        if discount_pct > 100:
            discount_pct = Decimal("100.00")

        discount_amt = (gross * discount_pct) / Decimal("100")
        discount_total += discount_amt

        SaleItem.objects.create(
            sale=sale,
            product=p,
            qty=qty,
            unit_price=unit_price,
            discount_pct=discount_pct,
        )

        record_stock_movement(
            product=p,
            movement_type="SALE",
            qty=qty,
            user=cashier,
            reference=sale.receipt_no,
            comments="Sale transaction",
        )

    sale.subtotal = subtotal
    sale.discount = discount_total
    sale.total = max(Decimal("0.00"), subtotal - discount_total)
    sale.save(update_fields=["subtotal", "discount", "total"])

    return sale



from .models import Product, ProductSupplier

SAFETY_STOCK_DEFAULT = 5

def get_reorder_queryset():
    now = timezone.now()
    since = now - timedelta(days=30)

    snooze_ok = Q(reorder_snooze__snoozed_until__isnull=True) | Q(reorder_snooze__snoozed_until__lte=now)
    dismiss_ok = Q(reorder_snooze__dismissed_until__isnull=True) | Q(reorder_snooze__dismissed_until__lte=now)

    out_q = Q(qty_in_stock=0)
    low_q = Q(qty_in_stock__gt=0, qty_in_stock__lte=F("reorder_level"), reorder_level__gt=0)

    qs = (
        Product.objects.filter(is_active=True)
        .filter(snooze_ok & dismiss_ok)
        .filter(out_q | low_q)
        .annotate(
            sold_30d=Coalesce(
                Sum("sale_items__qty", filter=Q(sale_items__sale__created_at__gte=since)),
                0
            )
        )
        .prefetch_related(
            models.Prefetch(
                "supplier_links",
                queryset=ProductSupplier.objects.filter(is_primary=True).select_related("supplier"),
                to_attr="primary_links",
            )
        )
        .select_related("category")
        .order_by("qty_in_stock", "product_name")
    )
    return qs, since


def compute_reorder_row(product, safety_stock=SAFETY_STOCK_DEFAULT):
    link = product.primary_links[0] if getattr(product, "primary_links", None) else None
    supplier = link.supplier if link else None

    lead_time = 0
    if link:
        lead_time = link.lead_time_days if link.lead_time_days is not None else (supplier.lead_time_days or 0)

    sold_30d = int(product.sold_30d or 0)
    avg_daily = sold_30d / 30.0

    fallback = max(product.reorder_level - product.qty_in_stock, 0)

    if sold_30d <= 0 or lead_time <= 0:
        suggested = fallback
    else:
        raw = (avg_daily * lead_time) + safety_stock - product.qty_in_stock
        demand_based = max(int(math.ceil(raw)), 0)
        suggested = max(demand_based, fallback)

    status = "OUT" if product.qty_in_stock == 0 else "LOW"

    return {
        "supplier": supplier,
        "supplier_id": supplier.id if supplier else None,
        "supplier_name": supplier.supplier_name if supplier else "No primary supplier",
        "lead_time_days": lead_time,
        "sold_30d": sold_30d,
        "avg_daily_sales": round(avg_daily, 2),
        "suggested_qty": suggested,
        "status_label": status,
        "last_unit_cost": link.last_unit_cost if link else None,
    }

@transaction.atomic
def set_primary_supplier(*, product, supplier, lead_time_days=None, last_unit_cost=None):
    if not supplier:
        return
    ProductSupplier.objects.filter(product=product).update(is_primary=False)
    ProductSupplier.objects.update_or_create(
        product=product,
        supplier=supplier,
        defaults={
            "is_primary": True,
            "lead_time_days": lead_time_days,
            "last_unit_cost": last_unit_cost,
        }
    )
