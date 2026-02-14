import math
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Min, Max

from retail.models import DailyProductSales, FestiveCalendar, Product


class Command(BaseCommand):
    help = "Backfill in_stock_flag using sales-pattern heuristics around festivals."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--to", dest="date_to", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--k", dest="k", type=int, default=5, help="Consecutive zero days (default: 5)")
        parser.add_argument("--fallback", dest="fallback", type=int, default=7, help="Fallback restock days (default: 7)")
        parser.add_argument("--top-pct", dest="top_pct", type=int, default=20, help="Top %% per category (default: 20)")
        parser.add_argument("--abs-threshold", dest="abs_threshold", type=int, default=5,
                            help="Absolute qty threshold during festival (default: 5)")
        parser.add_argument("--prewindow-days", dest="prewindow_days", type=int, default=60,
                            help="Days before festival start with any sale required (default: 60)")
        parser.add_argument("--reset", action="store_true",
                            help="Reset in_stock_flag to NULL, then set active products to 1 before applying rules")
        parser.add_argument("--dry-run", action="store_true", help="Compute counts but do not write changes")

    @transaction.atomic
    def handle(self, *args, **options):
        date_from = options["date_from"]
        date_to = options["date_to"]
        k = max(1, int(options["k"]))
        fallback = max(1, int(options["fallback"]))
        top_pct = max(1, int(options["top_pct"]))
        abs_threshold = int(options["abs_threshold"])
        prewindow_days = max(1, int(options["prewindow_days"]))
        reset = options["reset"]
        dry_run = options["dry_run"]

        def parse_ymd(s):
            return datetime.strptime(s, "%Y-%m-%d").date()

        dfrom = parse_ymd(date_from) if date_from else None
        dto = parse_ymd(date_to) if date_to else None

        agg = DailyProductSales.objects.aggregate(min_date=Min("date"), max_date=Max("date"))
        if not dfrom:
            dfrom = agg["min_date"]
        if not dto:
            dto = agg["max_date"]

        if not dfrom or not dto:
            self.stdout.write(self.style.WARNING("No DailyProductSales data found. Nothing to do."))
            return

        if dto < dfrom:
            self.stdout.write(self.style.ERROR("--to must be on/after --from."))
            return

        if reset and not dry_run:
            base_qs = DailyProductSales.objects.filter(date__gte=dfrom, date__lte=dto)
            base_qs.update(in_stock_flag=None)
            base_qs.filter(product__is_active=True).update(in_stock_flag=True)

        products = list(Product.objects.values("id", "category_id", "is_active"))
        product_active = {p["id"]: bool(p["is_active"]) for p in products}
        category_products = {}
        for p in products:
            category_products.setdefault(p["category_id"], []).append(p["id"])

        festivals = list(FestiveCalendar.objects.all())
        if not festivals:
            self.stdout.write(self.style.WARNING("No festivals found. Nothing to do."))
            return

        total_marked = 0
        total_candidates = 0

        for fest in festivals:
            if fest.start_date is None or fest.end_date is None:
                continue
            start = max(fest.start_date, dfrom)
            end = min(fest.end_date, dto)
            if start > end:
                continue

            totals_qs = (
                DailyProductSales.objects
                .filter(date__gte=start, date__lte=end)
                .values("product_id", "product__category_id")
                .annotate(total=Sum("qty_sold"))
            )

            total_by_product = {}
            by_category = {}
            for row in totals_qs:
                pid = row["product_id"]
                cid = row["product__category_id"]
                total = row["total"] or 0
                total_by_product[pid] = total
                by_category.setdefault(cid, []).append((pid, total))

            strong_abs = {pid for pid, total in total_by_product.items() if total >= abs_threshold}

            strong_top = set()
            for cid, plist in category_products.items():
                totals = [(pid, total_by_product.get(pid, 0)) for pid in plist]
                # Only consider products with any festival sales for top ranking
                totals = [t for t in totals if t[1] > 0]
                if not totals:
                    continue
                totals.sort(key=lambda x: x[1], reverse=True)
                top_n = max(1, int(math.ceil(len(totals) * (top_pct / 100.0))))
                for pid, _ in totals[:top_n]:
                    strong_top.add(pid)

            strong = strong_abs | strong_top

            pre_start = start - timedelta(days=prewindow_days)
            pre_end = start - timedelta(days=1)
            pre_set = set(
                DailyProductSales.objects
                .filter(date__gte=pre_start, date__lte=pre_end, qty_sold__gt=0)
                .values_list("product_id", flat=True)
                .distinct()
            )

            candidates = [pid for pid in strong if pid in pre_set and product_active.get(pid)]
            total_candidates += len(candidates)

            for pid in candidates:
                rows = list(
                    DailyProductSales.objects
                    .filter(product_id=pid, date__gt=end, date__lte=dto)
                    .order_by("date")
                    .values_list("date", "qty_sold")
                )
                if not rows:
                    continue

                streak_start_idx = None
                found = False
                for i, (_, qty) in enumerate(rows):
                    if qty == 0:
                        if streak_start_idx is None:
                            streak_start_idx = i
                        if i - streak_start_idx + 1 >= k:
                            found = True
                            break
                    else:
                        streak_start_idx = None

                if not found or streak_start_idx is None:
                    continue

                max_end_idx = min(streak_start_idx + fallback - 1, len(rows) - 1)
                end_idx = max_end_idx
                for j in range(streak_start_idx, max_end_idx + 1):
                    if rows[j][1] > 0:
                        end_idx = j - 1
                        break

                if end_idx < streak_start_idx:
                    continue

                dates_to_mark = [rows[j][0] for j in range(streak_start_idx, end_idx + 1) if rows[j][1] == 0]
                if not dates_to_mark:
                    continue

                total_marked += len(dates_to_mark)
                if not dry_run:
                    DailyProductSales.objects.filter(product_id=pid, date__in=dates_to_mark).update(in_stock_flag=False)

        self.stdout.write(self.style.SUCCESS(
            f"Candidates: {total_candidates}. Marked in_stock_flag=0 for {total_marked} rows."
        ))
