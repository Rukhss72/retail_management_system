from datetime import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Max, Sum, Value
from django.db.models.functions import Coalesce

from retail.models import DailyProductSales, FestiveCalendar, Sale, SaleItem


class Command(BaseCommand):
    help = (
        "Set sales to zero on festival_date closures. "
        "Updates SaleItem, recomputes Sale totals, and updates DailyProductSales."
    )

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--to", dest="date_to", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually persist changes. Without this flag, command runs as dry-run.",
        )

    @staticmethod
    def _parse_ymd(raw):
        if not raw:
            return None
        return datetime.strptime(raw, "%Y-%m-%d").date()

    @transaction.atomic
    def handle(self, *args, **options):
        dfrom = self._parse_ymd(options["date_from"])
        dto = self._parse_ymd(options["date_to"])
        apply_changes = bool(options["apply"])

        fest_qs = FestiveCalendar.objects.exclude(festival_date__isnull=True)
        if dfrom:
            fest_qs = fest_qs.filter(festival_date__gte=dfrom)
        if dto:
            fest_qs = fest_qs.filter(festival_date__lte=dto)

        closure_dates = sorted(set(fest_qs.values_list("festival_date", flat=True)))
        if not closure_dates:
            self.stdout.write(self.style.WARNING("No festival_date rows matched. Nothing to do."))
            return

        sale_items_qs = SaleItem.objects.select_related("sale").filter(sale__sale_date__in=closure_dates)
        impacted_sales = set(sale_items_qs.values_list("sale_id", flat=True))
        impacted_items_count = sale_items_qs.count()

        daily_qs = DailyProductSales.objects.filter(date__in=closure_dates)
        impacted_daily_count = daily_qs.count()

        self.stdout.write(
            f"Closure dates: {len(closure_dates)} | SaleItem rows: {impacted_items_count} | Daily rows: {impacted_daily_count}"
        )

        if not apply_changes:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("Dry-run complete. Re-run with --apply to persist."))
            return

        # 1) Force closed-day sale items to zero.
        to_update_items = []
        for item in sale_items_qs.iterator():
            changed = False
            if item.qty != 0:
                item.qty = 0
                changed = True
            if item.discount_amt != Decimal("0.00"):
                item.discount_amt = Decimal("0.00")
                changed = True
            if item.discount_pct != Decimal("0.00"):
                item.discount_pct = Decimal("0.00")
                changed = True
            if item.line_total != Decimal("0.00"):
                item.line_total = Decimal("0.00")
                changed = True
            if item.promo_flag:
                item.promo_flag = False
                changed = True
            if changed:
                to_update_items.append(item)

        if to_update_items:
            SaleItem.objects.bulk_update(
                to_update_items,
                fields=["qty", "discount_amt", "discount_pct", "line_total", "promo_flag"],
                batch_size=1000,
            )

        # 2) Recompute sale headers.
        if impacted_sales:
            aggs = (
                SaleItem.objects.filter(sale_id__in=impacted_sales)
                .values("sale_id")
                .annotate(
                    subtotal=Coalesce(Sum(F("unit_price") * F("qty")), Value(Decimal("0.00"))),
                    discount=Coalesce(Sum("discount_amt"), Value(Decimal("0.00"))),
                    total=Coalesce(Sum("line_total"), Value(Decimal("0.00"))),
                    has_promo=Coalesce(Max("promo_flag"), Value(False)),
                )
            )
            agg_map = {r["sale_id"]: r for r in aggs}
            sales_to_update = []
            for sale in Sale.objects.filter(id__in=impacted_sales):
                row = agg_map.get(sale.id)
                if row:
                    sale.subtotal = Decimal(row["subtotal"])
                    sale.discount = Decimal(row["discount"])
                    sale.total = Decimal(row["total"])
                    sale.promo_flag = bool(row["has_promo"])
                else:
                    sale.subtotal = Decimal("0.00")
                    sale.discount = Decimal("0.00")
                    sale.total = Decimal("0.00")
                    sale.promo_flag = False
                sales_to_update.append(sale)

            if sales_to_update:
                Sale.objects.bulk_update(
                    sales_to_update,
                    fields=["subtotal", "discount", "total", "promo_flag"],
                    batch_size=1000,
                )

        # 3) Keep daily training table aligned.
        if impacted_daily_count:
            daily_qs.update(qty_sold=0, sales_amt=Decimal("0.00"), promo_flag=False)

        self.stdout.write(
            self.style.SUCCESS(
                f"Applied closure. Updated SaleItem={len(to_update_items)}, Sale={len(impacted_sales)}, DailyProductSales={impacted_daily_count}"
            )
        )
