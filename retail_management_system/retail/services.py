import uuid
import math
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.utils import timezone
from django.db.models import Q, Sum, F, Max, Case, When, IntegerField
from django.db.models.functions import Coalesce

from .models import StockMovement, Sale, SaleItem, Product, ProductSupplier, DailyProductSales, FestiveCalendar


# Core stock and sales service helpers.
def apply_stock_movement(*, product, movement_type, qty, adjust_direction=None):
    # Applies the stock delta directly to the product based on movement type.
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


# Generates the next receipt number for a sale date.
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
    # Updates product stock first.
    apply_stock_movement(
        product=product,
        movement_type=movement_type,
        qty=qty,
        adjust_direction=adjust_direction,
    )

    # Then stores the matching stock movement record.
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


def _sync_daily_product_sales_for_sale(sale):
    # Keeps the stored daily analytics table aligned with newly created sales.
    sale_rows = (
        sale.items.values("product_id")
        .annotate(
            qty_sold=Coalesce(Sum("qty"), 0),
            sales_amt=Coalesce(Sum("line_total"), Decimal("0.00")),
            promo_flag=Max(
                Case(
                    When(promo_flag=True, then=1),
                    When(discount_amt__gt=0, then=1),
                    When(discount_pct__gt=0, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )
    )

    festival = (
        FestiveCalendar.objects.filter(start_date__lte=sale.sale_date, end_date__gte=sale.sale_date)
        .order_by("start_date", "festival_name")
        .first()
    )
    festival_name = festival.festival_name if festival else ""
    is_festive = bool(festival_name)

    for row in sale_rows:
        product = Product.objects.filter(id=row["product_id"]).only("id", "category_id", "is_active", "qty_in_stock").first()
        if not product:
            continue

        defaults = {
            "category_id": product.category_id,
            "qty_sold": row["qty_sold"],
            "sales_amt": row["sales_amt"],
            "promo_flag": bool(row["promo_flag"] or 0),
            "is_festive": is_festive,
            "festival_name": festival_name,
            "product_active": product.is_active,
            "in_stock_flag": bool((product.qty_in_stock or 0) > 0),
        }
        daily_row, created = DailyProductSales.objects.get_or_create(
            date=sale.sale_date,
            product_id=product.id,
            defaults=defaults,
        )
        if created:
            continue

        daily_row.category_id = product.category_id
        daily_row.qty_sold = int(daily_row.qty_sold or 0) + int(row["qty_sold"] or 0)
        daily_row.sales_amt = Decimal(daily_row.sales_amt or Decimal("0.00")) + Decimal(row["sales_amt"] or Decimal("0.00"))
        daily_row.promo_flag = daily_row.promo_flag or bool(row["promo_flag"] or 0)
        daily_row.is_festive = is_festive
        daily_row.festival_name = festival_name
        daily_row.product_active = product.is_active
        daily_row.in_stock_flag = bool((product.qty_in_stock or 0) > 0)
        daily_row.save(
            update_fields=[
                "category",
                "qty_sold",
                "sales_amt",
                "promo_flag",
                "is_festive",
                "festival_name",
                "product_active",
                "in_stock_flag",
                "updated_at",
            ]
        )

@transaction.atomic
def create_sale(*, cashier, customer_name, customer_phone, discount, payment_method, cart_items):
    """
    cart_items = [{"product_id": 1, "qty": 2, "unit_price": "45.00"}, ...]
    """
    # Creates a sale, its sale items, and the matching stock movements.
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
    has_promo_item = False

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
        if discount_amt > 0:
            has_promo_item = True

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
    sale.promo_flag = has_promo_item
    sale.save(update_fields=["subtotal", "discount", "total", "promo_flag"])
    _sync_daily_product_sales_for_sale(sale)

    return sale


# Reorder suggestion helpers.
from .models import Product, ProductSupplier

SAFETY_STOCK_DEFAULT = 5

def get_reorder_queryset():
    # Returns products currently low or out of stock and eligible for reorder checks.
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
    # Builds the reorder summary data used by the suppliers reorder screen.
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
    # Marks one supplier as primary for the given product and syncs the product field.
    if not supplier:
        return
    ProductSupplier.objects.filter(product=product).update(is_primary=False)
    if product.supplier_id != supplier.id:
        product.supplier = supplier
        product.save(update_fields=["supplier", "updated_at"])
    ProductSupplier.objects.update_or_create(
        product=product,
        supplier=supplier,
        defaults={
            "is_primary": True,
            "lead_time_days": lead_time_days,
            "last_unit_cost": last_unit_cost,
        }
    )
