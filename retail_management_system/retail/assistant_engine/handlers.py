import math
from datetime import timedelta
from decimal import Decimal
from calendar import monthrange

from django.db.models import Case, Count, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from retail.models import (
    DailyProductSales,
    ForecastDaily,
    ForecastRun,
    PurchaseOrder,
    Product,
    ProductSupplier,
    ReorderSnooze,
    Sale,
    SaleItem,
    StockMovement,
)


def _table(columns, rows):
    return {"columns": columns, "rows": rows}


def _range_label(start_date, end_date) -> str:
    if (
        start_date
        and end_date
        and start_date.year == end_date.year
        and start_date.month == end_date.month
        and start_date.day == 1
        and end_date.day == monthrange(end_date.year, end_date.month)[1]
    ):
        return start_date.strftime("%B %Y")
    return f"{start_date} to {end_date}"


def handle_stock_lookup(query: str, entities: dict) -> dict:
    text = query.lower()
    threshold = entities.get("threshold")
    category = entities.get("category")
    product = entities.get("product")
    low_phrases = ("low stock", "low in stock", "running low", "below reorder")
    asks_low = any(p in text for p in low_phrases)
    asks_oos = ("out of stock" in text) or ("no stock" in text)
    asks_set_count = ("how many" in text) and ("stock" in text) and (" set " in f" {text} " or " sets " in f" {text} ")

    if asks_set_count:
        qs = Product.objects.filter(is_active=True, qty_in_stock__gt=0, product_name__icontains="set").select_related("category")
        if category:
            qs = qs.filter(category=category)
        total_units = sum(int(p.qty_in_stock or 0) for p in qs)
        rows = [
            {
                "sku": p.sku,
                "product": p.product_name,
                "category": p.category.category_name,
                "stock": p.qty_in_stock,
            }
            for p in qs.order_by("product_name")[:25]
        ]
        return {
            "summary": f"There are {total_units} set units in stock across {qs.count()} products.",
            "table": _table(["sku", "product", "category", "stock"], rows),
            "chart": None,
            "source": ["Product", "Category"],
        }

    if asks_low and asks_oos:
        low_qs = Product.objects.filter(
            is_active=True,
            qty_in_stock__gt=0,
            qty_in_stock__lte=F("reorder_level"),
        ).select_related("category")
        oos_qs = Product.objects.filter(is_active=True, qty_in_stock=0).select_related("category")
        if category:
            low_qs = low_qs.filter(category=category)
            oos_qs = oos_qs.filter(category=category)

        rows = [
            {
                "sku": p.sku,
                "product": p.product_name,
                "category": p.category.category_name,
                "stock": p.qty_in_stock,
                "status": "low",
            }
            for p in low_qs.order_by("qty_in_stock", "product_name")[:15]
        ]
        rows.extend(
            [
                {
                    "sku": p.sku,
                    "product": p.product_name,
                    "category": p.category.category_name,
                    "stock": p.qty_in_stock,
                    "status": "out_of_stock",
                }
                for p in oos_qs.order_by("product_name")[:15]
            ]
        )
        return {
            "summary": f"{low_qs.count()} low-stock and {oos_qs.count()} out-of-stock items.",
            "table": _table(["sku", "product", "category", "stock", "status"], rows),
            "chart": None,
            "source": ["Product", "Category"],
        }

    if asks_oos:
        qs = Product.objects.filter(is_active=True, qty_in_stock=0).select_related("category").order_by("product_name")
        rows = [
            {"sku": p.sku, "product": p.product_name, "category": p.category.category_name, "stock": p.qty_in_stock}
            for p in qs[:20]
        ]
        return {
            "summary": f"{qs.count()} items are currently out of stock.",
            "table": _table(["sku", "product", "category", "stock"], rows),
            "chart": None,
            "source": ["Product"],
        }

    if "summary by category" in text or "stock summary by category" in text:
        qs = (
            Product.objects.filter(is_active=True)
            .values("category__category_name")
            .annotate(
                total_skus=Count("id"),
                units_in_stock=Coalesce(Sum("qty_in_stock"), 0),
                low_stock=Coalesce(
                    Sum(
                        Case(
                            When(qty_in_stock__gt=0, qty_in_stock__lte=F("reorder_level"), then=Value(1)),
                            default=Value(0),
                            output_field=IntegerField(),
                        )
                    ),
                    0,
                ),
                out_of_stock=Coalesce(
                    Sum(Case(When(qty_in_stock=0, then=Value(1)), default=Value(0), output_field=IntegerField())), 0
                ),
            )
            .order_by("-units_in_stock")
        )
        rows = [
            {
                "category": r["category__category_name"] or "Uncategorized",
                "total_skus": r["total_skus"],
                "units_in_stock": r["units_in_stock"],
                "low_stock": r["low_stock"],
                "out_of_stock": r["out_of_stock"],
            }
            for r in qs
        ]
        return {
            "summary": f"Stock summary generated for {len(rows)} categories.",
            "table": _table(["category", "total_skus", "units_in_stock", "low_stock", "out_of_stock"], rows),
            "chart": None,
            "source": ["Product", "Category"],
        }

    if asks_low:
        qs = Product.objects.filter(
            is_active=True,
            qty_in_stock__gt=0,
            qty_in_stock__lte=F("reorder_level"),
        ).select_related("category")
        if category:
            qs = qs.filter(category=category)
        qs = qs.order_by("qty_in_stock", "product_name")
        summary = f"{qs.count()} items are low stock (qty in stock <= reorder level)."
        rows = [
            {
                "sku": p.sku,
                "product": p.product_name,
                "category": p.category.category_name,
                "stock": p.qty_in_stock,
                "reorder_level": p.reorder_level,
            }
            for p in qs[:25]
        ]
        return {
            "summary": summary,
            "table": _table(["sku", "product", "category", "stock", "reorder_level"], rows),
            "chart": None,
            "source": ["Product", "Category"],
        }

    if product:
        return {
            "summary": f"{product.product_name} ({product.sku}) has {product.qty_in_stock} units in stock.",
            "table": _table(
                ["sku", "product", "stock", "reorder_level"],
                [
                    {
                        "sku": product.sku,
                        "product": product.product_name,
                        "stock": product.qty_in_stock,
                        "reorder_level": product.reorder_level,
                    }
                ],
            ),
            "chart": None,
            "source": ["Product"],
        }

    top = Product.objects.filter(is_active=True).order_by("qty_in_stock", "product_name")[:20]
    rows = [{"sku": p.sku, "product": p.product_name, "stock": p.qty_in_stock} for p in top]
    return {
        "summary": "I need a product name/SKU or a stock filter. Showing lowest stock items for now.",
        "table": _table(["sku", "product", "stock"], rows),
        "chart": None,
        "source": ["Product"],
    }


def handle_stock_movements(query: str, entities: dict) -> dict:
    start, end = entities["start"], entities["end"]
    product = entities.get("product")

    qs = StockMovement.objects.filter(movement_date__date__gte=start, movement_date__date__lte=end).select_related("product")
    if product:
        qs = qs.filter(product=product)

    summary_rows = (
        qs.values("movement_type")
        .annotate(total_qty=Coalesce(Sum("qty_moved"), 0))
        .order_by("movement_type")
    )
    totals = {r["movement_type"]: r["total_qty"] for r in summary_rows}

    rows = [
        {
            "date": timezone.localtime(m.movement_date).strftime("%Y-%m-%d"),
            "type": m.movement_type,
            "product": m.product.product_name,
            "sku": m.product.sku,
            "qty": m.qty_moved,
        }
        for m in qs.order_by("-movement_date")[:30]
    ]
    chart = {
        "type": "bar",
        "labels": list(totals.keys()),
        "datasets": [{"label": "Qty moved", "data": [int(totals[k]) for k in totals]}],
    }
    return {
        "summary": f"{qs.count()} stock movements found from {start} to {end}.",
        "table": _table(["date", "type", "product", "sku", "qty"], rows),
        "chart": chart,
        "source": ["StockMovement", "Product"],
    }


def handle_sales_summary(query: str, entities: dict) -> dict:
    start, end = entities["start"], entities["end"]

    sales_qs = Sale.objects.filter(sale_date__gte=start, sale_date__lte=end)
    items_qs = SaleItem.objects.filter(sale__sale_date__gte=start, sale__sale_date__lte=end)

    agg_sale = sales_qs.aggregate(revenue=Coalesce(Sum("total"), Decimal("0.00")))
    agg_items = items_qs.aggregate(qty=Coalesce(Sum("qty"), 0))

    rows = [
        {
            "range": f"{start} to {end}",
            "revenue": float(agg_sale["revenue"]),
            "quantity": int(agg_items["qty"]),
            "orders": sales_qs.count(),
        }
    ]

    return {
        "summary": f"Sales from {start} to {end}: revenue {agg_sale['revenue']} and quantity {agg_items['qty']}.",
        "table": _table(["range", "revenue", "quantity", "orders"], rows),
        "chart": None,
        "source": ["Sale", "SaleItem"],
    }


def handle_compare_sales(query: str, entities: dict) -> dict:
    start_a, end_a = entities["start"], entities["end"]
    start_b = entities.get("compare_start") or start_a
    end_b = entities.get("compare_end") or end_a
    category = entities.get("category")
    label_a = _range_label(start_a, end_a)
    label_b = _range_label(start_b, end_b)

    qs_a = SaleItem.objects.filter(sale__sale_date__gte=start_a, sale__sale_date__lte=end_a)
    qs_b = SaleItem.objects.filter(sale__sale_date__gte=start_b, sale__sale_date__lte=end_b)
    if category:
        qs_a = qs_a.filter(product__category=category)
        qs_b = qs_b.filter(product__category=category)

    agg_a = {
        row["product__sku"]: row
        for row in qs_a.values("product__sku", "product__product_name")
        .annotate(units=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
    }
    agg_b = {
        row["product__sku"]: row
        for row in qs_b.values("product__sku", "product__product_name")
        .annotate(units=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
    }

    keys = set(agg_a.keys()) | set(agg_b.keys())
    rows = []
    for sku in keys:
        a = agg_a.get(sku)
        b = agg_b.get(sku)
        units_a = int((a or {}).get("units", 0))
        units_b = int((b or {}).get("units", 0))
        revenue_a = float((a or {}).get("revenue", 0))
        revenue_b = float((b or {}).get("revenue", 0))
        rows.append(
            {
                "sku": sku,
                "product": (a or b or {}).get("product__product_name", ""),
                f"units ({label_a})": units_a,
                f"units ({label_b})": units_b,
                "delta units (A-B)": units_a - units_b,
                f"revenue ({label_a})": revenue_a,
                f"revenue ({label_b})": revenue_b,
                "delta revenue (A-B)": round(revenue_a - revenue_b, 2),
            }
        )

    rows.sort(key=lambda r: (r[f"units ({label_a})"] + r[f"units ({label_b})"]), reverse=True)
    rows = rows[:15]

    total_rev_a = round(sum(r[f"revenue ({label_a})"] for r in rows), 2)
    total_rev_b = round(sum(r[f"revenue ({label_b})"] for r in rows), 2)
    total_qty_a = sum(r[f"units ({label_a})"] for r in rows)
    total_qty_b = sum(r[f"units ({label_b})"] for r in rows)

    category_suffix = f" in {category.category_name}" if category else ""
    return {
        "summary": (
            f"Compared sales{category_suffix}: {label_a} vs {label_b}. "
            f"Delta columns are A-B. Revenue delta {round(total_rev_a-total_rev_b, 2)} and qty delta {total_qty_a-total_qty_b}."
        ),
        "table": _table(
            [
                "sku",
                "product",
                f"units ({label_a})",
                f"units ({label_b})",
                "delta units (A-B)",
                f"revenue ({label_a})",
                f"revenue ({label_b})",
                "delta revenue (A-B)",
            ],
            rows,
        ),
        "chart": {
            "charts": [
                {
                    "title": f"Units Comparison: {label_a} vs {label_b}",
                    "labels": [r["product"] for r in rows[:10]],
                    "datasets": [
                        {"label": label_a, "data": [r[f"units ({label_a})"] for r in rows[:10]]},
                        {"label": label_b, "data": [r[f"units ({label_b})"] for r in rows[:10]]},
                    ],
                },
                {
                    "title": f"Revenue Comparison: {label_a} vs {label_b}",
                    "labels": [r["product"] for r in rows[:10]],
                    "datasets": [
                        {"label": label_a, "data": [r[f"revenue ({label_a})"] for r in rows[:10]]},
                        {"label": label_b, "data": [r[f"revenue ({label_b})"] for r in rows[:10]]},
                    ],
                },
            ]
        },
        "source": ["Sale", "SaleItem", "Product"],
    }


def handle_top_sellers(query: str, entities: dict) -> dict:
    text = query.lower()
    start, end = entities["start"], entities["end"]
    product = entities.get("product")
    category = entities.get("category")
    base_qs = SaleItem.objects.filter(sale__sale_date__gte=start, sale__sale_date__lte=end)
    if category:
        base_qs = base_qs.filter(product__category=category)

    if "category" in text:
        qs = (
            base_qs
            .values("product__category__category_name")
            .annotate(units=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
            .order_by("-units", "-revenue")[:10]
        )
        rows = [
            {
                "category": r["product__category__category_name"] or "Uncategorized",
                "units": int(r["units"]),
                "revenue": float(r["revenue"]),
            }
            for r in qs
        ]
        return {
            "summary": f"Top-selling categories from {start} to {end}.",
            "table": _table(["category", "units", "revenue"], rows),
            "chart": {
                "type": "bar",
                "labels": [r["category"] for r in rows],
                "datasets": [{"label": "Units", "data": [r["units"] for r in rows]}],
            },
            "source": ["SaleItem", "Sale", "Category"],
        }

    if "trend" in text and product:
        trend_start = end - timedelta(days=29)
        qs = (
            SaleItem.objects.filter(sale__sale_date__gte=trend_start, sale__sale_date__lte=end, product=product)
            .values("sale__sale_date")
            .annotate(units=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
            .order_by("sale__sale_date")
        )
        rows = [
            {"date": str(r["sale__sale_date"]), "units": int(r["units"]), "revenue": float(r["revenue"])}
            for r in qs
        ]
        return {
            "summary": f"{product.product_name} trend for last 30 days generated.",
            "table": _table(["date", "units", "revenue"], rows),
            "chart": {
                "type": "line",
                "labels": [r["date"] for r in rows],
                "datasets": [{"label": "Units sold", "data": [r["units"] for r in rows]}],
            },
            "source": ["SaleItem", "Sale", "Product"],
        }

    qs = (
        base_qs
        .values("product__sku", "product__product_name")
        .annotate(units=Coalesce(Sum("qty"), 0), revenue=Coalesce(Sum("line_total"), Decimal("0.00")))
        .order_by("-units", "-revenue")[:10]
    )
    rows = [
        {
            "sku": r["product__sku"],
            "product": r["product__product_name"],
            "units": int(r["units"]),
            "revenue": float(r["revenue"]),
        }
        for r in qs
    ]
    category_suffix = f" in {category.category_name}" if category else ""
    return {
        "summary": f"Top sellers{category_suffix} from {start} to {end}.",
        "table": _table(["sku", "product", "units", "revenue"], rows),
        "chart": {
            "type": "bar",
            "labels": [r["product"] for r in rows],
            "datasets": [{"label": "Units sold", "data": [r["units"] for r in rows]}],
        },
        "source": ["SaleItem", "Sale", "Product"],
    }


def handle_forecast_best_sellers(query: str, entities: dict) -> dict:
    today = timezone.localdate()
    horizon_days = entities["horizon_days"]
    end = today + timedelta(days=horizon_days - 1)
    run = ForecastRun.objects.order_by("-created_at").first()

    if not run:
        return {
            "summary": "No forecast run found yet.",
            "table": _table(["message"], [{"message": "Run forecasting pipeline first."}]),
            "chart": None,
            "source": ["ForecastRun"],
        }

    text = query.lower()
    product = entities.get("product")
    base_qs = ForecastDaily.objects.filter(run=run, forecast_date__gte=today, forecast_date__lte=end)

    if product:
        points = (
            base_qs.filter(product=product).values("forecast_date").annotate(pred=Coalesce(Sum("predicted_qty"), Decimal("0.00"))).order_by("forecast_date")
        )
        rows = [{"date": str(r["forecast_date"]), "predicted_qty": float(r["pred"])} for r in points]
        return {
            "summary": f"Forecast demand for {product.product_name} for next {horizon_days} days.",
            "table": _table(["date", "predicted_qty"], rows),
            "chart": {
                "type": "line",
                "labels": [r["date"] for r in rows],
                "datasets": [{"label": "Predicted qty", "data": [r["predicted_qty"] for r in rows]}],
            },
            "source": ["ForecastRun", "ForecastDaily", "Product"],
        }

    qs = (
        base_qs.values("product__sku", "product__product_name")
        .annotate(predicted_qty=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
        .order_by("-predicted_qty")[:10]
    )
    rows = [
        {"sku": r["product__sku"], "product": r["product__product_name"], "predicted_qty": float(r["predicted_qty"])}
        for r in qs
    ]

    summary = f"Top predicted best-sellers for next {horizon_days} days."
    if "actual vs predicted" in text and product:
        summary = f"Actual vs predicted for {product.product_name} generated."

    return {
        "summary": summary,
        "table": _table(["sku", "product", "predicted_qty"], rows),
        "chart": {
            "type": "bar",
            "labels": [r["product"] for r in rows],
            "datasets": [{"label": "Predicted qty", "data": [r["predicted_qty"] for r in rows]}],
        },
        "source": ["ForecastRun", "ForecastDaily", "Product"],
    }


def handle_reorder_suggestions(query: str, entities: dict) -> dict:
    text = query.lower()
    asks_reorder_history = any(
        p in text
        for p in (
            "did i reorder",
            "did we reorder",
            "reordered this week",
            "reorder this week",
            "purchase order this week",
            "did i place an order",
        )
    )
    if asks_reorder_history:
        start, end = entities["start"], entities["end"]
        pos = (
            PurchaseOrder.objects.filter(order_date__gte=start, order_date__lte=end)
            .exclude(status=PurchaseOrder.STATUS_DRAFT)
            .select_related("supplier")
            .order_by("-order_date", "-id")
        )
        rows = [
            {
                "po_number": po.po_number or f"PO-{po.id}",
                "supplier": po.supplier.supplier_name,
                "status": po.status,
                "order_date": str(po.order_date),
                "delivery_date": str(po.delivery_date) if po.delivery_date else "",
                "total_amount": float(po.total_amount or 0),
                "total_qty_ordered": int(po.total_qty_ordered or 0),
            }
            for po in pos[:20]
        ]
        if rows:
            return {
                "summary": f"Yes. {pos.count()} purchase orders were created from {start} to {end}.",
                "table": _table(
                    ["po_number", "supplier", "status", "order_date", "delivery_date", "total_amount", "total_qty_ordered"],
                    rows,
                ),
                "chart": None,
                "source": ["PurchaseOrder", "Supplier", "PurchaseOrderItem"],
            }
        return {
            "summary": f"No purchase orders were created from {start} to {end}.",
            "table": _table(["po_number", "supplier", "status", "order_date"], []),
            "chart": None,
            "source": ["PurchaseOrder"],
        }

    today = timezone.localdate()
    horizon_days = entities["horizon_days"]
    horizon_end = today + timedelta(days=horizon_days - 1)
    run = ForecastRun.objects.order_by("-created_at").first()

    products = Product.objects.filter(is_active=True).select_related("category").order_by("product_name")
    snoozed = {
        s.product_id
        for s in ReorderSnooze.objects.filter(
            Q(snoozed_until__isnull=False, snoozed_until__gt=timezone.now())
            | Q(dismissed_until__isnull=False, dismissed_until__gt=timezone.now())
        )
    }

    forecast_map = {}
    if run:
        forecast_rows = (
            ForecastDaily.objects.filter(run=run, forecast_date__gte=today, forecast_date__lte=horizon_end)
            .values("product_id")
            .annotate(pred=Coalesce(Sum("predicted_qty"), Decimal("0.00")))
        )
        forecast_map = {r["product_id"]: float(r["pred"]) for r in forecast_rows}

    primary_links = {
        link.product_id: link
        for link in ProductSupplier.objects.filter(is_primary=True).select_related("supplier")
    }

    rows = []
    for p in products:
        if p.id in snoozed:
            continue
        predicted = forecast_map.get(p.id, 0.0)
        safety_stock = max(p.reorder_level, 0)
        suggested = max(int(math.ceil(predicted + safety_stock - p.qty_in_stock)), 0)
        if suggested <= 0:
            continue
        link = primary_links.get(p.id)
        supplier_name = link.supplier.supplier_name if link else "No primary supplier"
        lead_time = 0
        if link:
            lead_time = link.lead_time_days if link.lead_time_days is not None else (link.supplier.lead_time_days or 0)
        rows.append(
            {
                "sku": p.sku,
                "product": p.product_name,
                "stock": p.qty_in_stock,
                "predicted_next_days": round(predicted, 2),
                "reorder_level": p.reorder_level,
                "suggested_qty": suggested,
                "supplier": supplier_name,
                "lead_time_days": lead_time,
            }
        )
    rows.sort(key=lambda r: r["suggested_qty"], reverse=True)
    rows = rows[:20]
    return {
        "summary": f"{len(rows)} products need reorder for next {horizon_days} days.",
        "table": _table(
            ["sku", "product", "stock", "predicted_next_days", "reorder_level", "suggested_qty", "supplier", "lead_time_days"],
            rows,
        ),
        "chart": {
            "type": "bar",
            "labels": [r["product"] for r in rows[:10]],
            "datasets": [{"label": "Suggested reorder qty", "data": [r["suggested_qty"] for r in rows[:10]]}],
        },
        "source": ["Product", "ForecastRun", "ForecastDaily", "ProductSupplier", "Supplier", "ReorderSnooze"],
    }


def handle_festival_insights(query: str, entities: dict) -> dict:
    festival = entities.get("festival_name")
    product = entities.get("product")
    qs = DailyProductSales.objects.all()
    if product:
        qs = qs.filter(product=product)
    if festival:
        qs = qs.filter(festival_name__icontains=festival)
    else:
        qs = qs.filter(is_festive=True)

    if product and not festival:
        by_festival = (
            qs.exclude(festival_name="")
            .values("festival_name")
            .annotate(total_qty=Coalesce(Sum("qty_sold"), 0), total_sales=Coalesce(Sum("sales_amt"), Decimal("0.00")))
            .order_by("-total_qty")[:10]
        )
        rows = [
            {
                "festival": r["festival_name"] or "Unknown",
                "qty": int(r["total_qty"]),
                "sales": float(r["total_sales"]),
            }
            for r in by_festival
        ]
        if not rows:
            return {
                "summary": f"No festive sales records found for {product.product_name}.",
                "table": _table(["festival", "qty", "sales"], []),
                "chart": None,
                "source": ["DailyProductSales", "FestiveCalendar", "Product"],
            }
        return {
            "summary": f"{product.product_name} sells best during {rows[0]['festival']}.",
            "table": _table(["festival", "qty", "sales"], rows),
            "chart": {
                "type": "bar",
                "labels": [r["festival"] for r in rows],
                "datasets": [{"label": "Qty sold", "data": [r["qty"] for r in rows]}],
            },
            "source": ["DailyProductSales", "FestiveCalendar", "Product"],
        }

    top = (
        qs.values("product__sku", "product__product_name")
        .annotate(total_qty=Coalesce(Sum("qty_sold"), 0), total_sales=Coalesce(Sum("sales_amt"), Decimal("0.00")))
        .order_by("-total_qty")[:10]
    )
    rows = [
        {"sku": r["product__sku"], "product": r["product__product_name"], "qty": int(r["total_qty"]), "sales": float(r["total_sales"])}
        for r in top
    ]
    label = festival or "festival windows"
    return {
        "summary": f"Top products during {label}.",
        "table": _table(["sku", "product", "qty", "sales"], rows),
        "chart": {
            "type": "bar",
            "labels": [r["product"] for r in rows],
            "datasets": [{"label": "Qty sold", "data": [r["qty"] for r in rows]}],
        },
        "source": ["DailyProductSales", "FestiveCalendar", "Product"],
    }


def handle_seasonality(query: str, entities: dict) -> dict:
    product = entities.get("product")
    if not product:
        return {
            "summary": "Please provide a product name or SKU for seasonality analysis.",
            "table": _table(["product", "note"], []),
            "chart": None,
            "source": ["DailyProductSales", "Product"],
        }

    rows_qs = (
        DailyProductSales.objects.filter(product=product)
        .values("date__month")
        .annotate(total_qty=Coalesce(Sum("qty_sold"), 0), total_sales=Coalesce(Sum("sales_amt"), Decimal("0.00")))
        .order_by("date__month")
    )
    if not rows_qs:
        return {
            "summary": f"No seasonality history found for {product.product_name}.",
            "table": _table(["season", "qty", "sales"], []),
            "chart": None,
            "source": ["DailyProductSales", "Product"],
        }

    def month_to_season(m):
        if m in (12, 1, 2):
            return "Summer"
        if m in (3, 4, 5):
            return "Autumn"
        if m in (6, 7, 8):
            return "Winter"
        return "Spring"

    by_season = {}
    for r in rows_qs:
        season = month_to_season(int(r["date__month"]))
        by_season.setdefault(season, {"qty": 0, "sales": 0.0})
        by_season[season]["qty"] += int(r["total_qty"] or 0)
        by_season[season]["sales"] += float(r["total_sales"] or 0)

    ordered = sorted(by_season.items(), key=lambda kv: kv[1]["qty"], reverse=True)
    rows = [{"season": s, "qty": v["qty"], "sales": round(v["sales"], 2)} for s, v in ordered]
    best = rows[0]["season"] if rows else "Unknown"
    return {
        "summary": f"{product.product_name} sells best in {best}.",
        "table": _table(["season", "qty", "sales"], rows),
        "chart": {
            "type": "bar",
            "labels": [r["season"] for r in rows],
            "datasets": [{"label": "Qty sold", "data": [r["qty"] for r in rows]}],
        },
        "source": ["DailyProductSales", "Product"],
    }
