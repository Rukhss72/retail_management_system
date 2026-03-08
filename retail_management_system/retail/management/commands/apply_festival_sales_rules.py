from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
import math
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Max, Sum, Value
from django.db.models.functions import Coalesce

from retail.models import Category, FestiveCalendar, Product, Sale, SaleItem


QTY_ROUND = Decimal("1")
PCT_ROUND = Decimal("0.01")
MONEY_ROUND = Decimal("0.01")


@dataclass(frozen=True)
class FestivalRule:
    aliases: tuple[str, ...]
    skus: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


RULES: tuple[FestivalRule, ...] = (
    FestivalRule(
        aliases=("new year", "christmas"),
        categories=("swimwear", "sportswear"),
    ),
    FestivalRule(
        aliases=("chinese spring festival",),
        skus=("WOM-TRD-00020", "WOM-TOP-00012"),
    ),
    FestivalRule(
        aliases=("thaipossam cavadee", "mahashivratree", "ougadi", "ganeshchaturti", "diwali"),
        skus=("WOM-ACC-00012", "MEN-TRD-00013", "WOM-TRD-00018", "WOM-TRD-00019"),
    ),
    FestivalRule(
        aliases=("valentine", "mother"),
        skus=("WOM-TOP-00012", "WOM-DRS-00021", "WOM-DRS-00022"),
    ),
    FestivalRule(
        aliases=("ramadaan", "ramadan", "eid"),
        skus=("WOM-ACC-00012", "WOM-ACC-00014", "UNI-SET-00002", "UNI-TRD-00003", "MEN-TRD-00013"),
    ),
)


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def quantize_money(x: Decimal) -> Decimal:
    return x.quantize(MONEY_ROUND, rounding=ROUND_HALF_UP)


def quantize_pct(x: Decimal) -> Decimal:
    return x.quantize(PCT_ROUND, rounding=ROUND_HALF_UP)


class Command(BaseCommand):
    help = (
        "Apply business festival rules to SaleItem data: "
        "1) set qty=0 on festival_date (closed day), "
        "2) uplift target SKUs/categories in the pre-festival window, "
        "3) recalculate Sale totals."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--uplift-pct",
            type=float,
            default=25.0,
            help="Uplift percent for targeted products in pre-festival window (default: 25).",
        )
        parser.add_argument(
            "--window-pct",
            type=float,
            default=20.0,
            help="Use last X%% of festival window before holiday date (default: 20).",
        )
        parser.add_argument(
            "--min-window-days",
            type=int,
            default=2,
            help="Minimum number of pre-festival days to uplift (default: 2).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write changes. Without this flag command runs in dry-run mode.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        uplift_pct = Decimal(str(options["uplift_pct"])) / Decimal("100")
        window_pct = Decimal(str(options["window_pct"])) / Decimal("100")
        min_window_days = max(1, int(options["min_window_days"]))
        apply_changes = bool(options["apply"])

        festivals = list(FestiveCalendar.objects.all().order_by("start_date", "festival_name"))
        if not festivals:
            self.stdout.write(self.style.WARNING("No FestiveCalendar rows found. Nothing to do."))
            return

        rules = [(rule, tuple(normalize_name(a) for a in rule.aliases)) for rule in RULES]

        impacted_sale_ids: set[int] = set()
        closed_items_count = 0
        uplift_items_count = 0

        for fest in festivals:
            fest_name = normalize_name(fest.festival_name)
            matched_rule = None
            for rule, aliases in rules:
                if any(alias in fest_name for alias in aliases):
                    matched_rule = rule
                    break

            holiday_date = fest.festival_date or fest.end_date
            if holiday_date is None:
                continue

            # Rule 1: shop closed on holiday date -> all sales qty = 0
            closed_qs = SaleItem.objects.select_related("sale").filter(sale__sale_date=holiday_date)
            closed_items_count += closed_qs.count()
            impacted_sale_ids.update(closed_qs.values_list("sale_id", flat=True))
            if apply_changes:
                for item in closed_qs.iterator():
                    item.qty = 0
                    item.discount_amt = Decimal("0.00")
                    item.discount_pct = Decimal("0.00")
                    item.promo_flag = False
                    item.line_total = Decimal("0.00")
                    item.save(update_fields=["qty", "discount_amt", "discount_pct", "promo_flag", "line_total"])

            # Rule 2: pre-festival uplift for mapped products/categories
            if not matched_rule:
                continue

            if fest.start_date is None:
                pre_start = holiday_date - timedelta(days=min_window_days)
            else:
                days_span = max(1, (holiday_date - fest.start_date).days)
                dyn_days = max(min_window_days, math.ceil(days_span * float(window_pct)))
                pre_start = holiday_date - timedelta(days=dyn_days)

            pre_end = holiday_date - timedelta(days=1)
            if pre_end < pre_start:
                continue

            target_product_ids = set()

            if matched_rule.skus:
                sku_ids = set(
                    Product.objects.filter(sku__in=matched_rule.skus).values_list("id", flat=True)
                )
                target_product_ids.update(sku_ids)

            if matched_rule.categories:
                cat_ids = set(
                    Category.objects.filter(category_name__in=matched_rule.categories).values_list("id", flat=True)
                )
                cat_product_ids = set(
                    Product.objects.filter(category_id__in=cat_ids).values_list("id", flat=True)
                )
                target_product_ids.update(cat_product_ids)

            if not target_product_ids:
                continue

            uplift_qs = SaleItem.objects.select_related("sale").filter(
                sale__sale_date__gte=pre_start,
                sale__sale_date__lte=pre_end,
                product_id__in=target_product_ids,
                qty__gt=0,
            )

            uplift_items_count += uplift_qs.count()
            impacted_sale_ids.update(uplift_qs.values_list("sale_id", flat=True))

            if apply_changes:
                for item in uplift_qs.iterator():
                    old_qty = item.qty
                    new_qty = int(math.ceil(old_qty * (1.0 + float(uplift_pct))))
                    if new_qty <= old_qty:
                        new_qty = old_qty + 1

                    old_gross = item.unit_price * Decimal(old_qty)
                    if old_gross > 0:
                        if item.discount_pct and item.discount_pct > 0:
                            eff_pct = Decimal(item.discount_pct)
                        elif item.discount_amt and item.discount_amt > 0:
                            eff_pct = (Decimal(item.discount_amt) / old_gross) * Decimal("100")
                        else:
                            eff_pct = Decimal("0.00")
                    else:
                        eff_pct = Decimal("0.00")

                    new_gross = item.unit_price * Decimal(new_qty)
                    new_discount_amt = quantize_money((new_gross * eff_pct) / Decimal("100"))
                    if new_discount_amt > new_gross:
                        new_discount_amt = new_gross
                    new_line_total = quantize_money(new_gross - new_discount_amt)

                    item.qty = new_qty
                    item.discount_pct = quantize_pct(eff_pct)
                    item.discount_amt = new_discount_amt
                    item.promo_flag = bool(new_discount_amt > 0)
                    item.line_total = new_line_total
                    item.save(update_fields=["qty", "discount_pct", "discount_amt", "promo_flag", "line_total"])

        if not apply_changes:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("DRY-RUN only. No DB data changed."))
        else:
            self._recompute_sales(list(impacted_sale_ids))

        self.stdout.write(
            self.style.SUCCESS(
                f"Processed festivals={len(festivals)} | "
                f"closed_day_items={closed_items_count} | "
                f"uplift_items={uplift_items_count} | "
                f"impacted_sales={len(impacted_sale_ids)}"
            )
        )

    def _recompute_sales(self, sale_ids: list[int]) -> None:
        if not sale_ids:
            return

        aggs = (
            SaleItem.objects.filter(sale_id__in=sale_ids)
            .values("sale_id")
            .annotate(
                subtotal=Coalesce(Sum(F("unit_price") * F("qty")), Value(Decimal("0.00"))),
                discount=Coalesce(Sum("discount_amt"), Value(Decimal("0.00"))),
                total=Coalesce(Sum("line_total"), Value(Decimal("0.00"))),
                has_promo=Coalesce(Max("promo_flag"), Value(False)),
            )
        )

        agg_map = {row["sale_id"]: row for row in aggs}
        sales = list(Sale.objects.filter(id__in=sale_ids))
        for sale in sales:
            row = agg_map.get(sale.id)
            if not row:
                sale.subtotal = Decimal("0.00")
                sale.discount = Decimal("0.00")
                sale.total = Decimal("0.00")
                sale.promo_flag = False
            else:
                sale.subtotal = quantize_money(Decimal(row["subtotal"]))
                sale.discount = quantize_money(Decimal(row["discount"]))
                sale.total = quantize_money(Decimal(row["total"]))
                sale.promo_flag = bool(row["has_promo"])

        Sale.objects.bulk_update(
            sales,
            fields=["subtotal", "discount", "total", "promo_flag"],
            batch_size=1000,
        )
