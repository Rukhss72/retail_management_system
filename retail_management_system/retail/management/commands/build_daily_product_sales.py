from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Max, Case, When, IntegerField
from datetime import datetime, date

from retail.models import SaleItem, DailyProductSales, FestiveCalendar


def pick_festival_name(day: date, festivals):
    matches = []
    for f in festivals:
        if f.start_date is None or f.end_date is None:
            continue
        if f.start_date <= day <= f.end_date:
            window_len = (f.end_date - f.start_date).days
            matches.append((window_len, f.end_date, f.festival_name))

    if not matches:
        return None

    matches.sort(key=lambda x: (x[0], x[1]))
    return matches[0][2]


class Command(BaseCommand):
    help = "Populate/refresh DailyProductSales from Sale + SaleItem, and tag festive flags from FestiveCalendar."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="date_from", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--to", dest="date_to", type=str, default=None, help="YYYY-MM-DD")
        parser.add_argument("--wipe", action="store_true",
                            help="Delete existing DailyProductSales in range before rebuild")

    @transaction.atomic
    def handle(self, *args, **options):
        date_from = options["date_from"]
        date_to = options["date_to"]
        wipe = options["wipe"]

        def parse_ymd(s):
            return datetime.strptime(s, "%Y-%m-%d").date()

        dfrom = parse_ymd(date_from) if date_from else None
        dto = parse_ymd(date_to) if date_to else None

        festivals = list(FestiveCalendar.objects.all())

        # Aggregate by business date (sale_date)
        qs = (
            SaleItem.objects
            .select_related("sale", "product")
            .values("sale__sale_date", "product_id")
            .annotate(
                qty_sold=Sum("qty"),
                sales_amt=Sum("line_total"),
                promo_flag=Max(
                    Case(
                        When(sale__promo_flag=True, then=1),
                        default=0,
                        output_field=IntegerField(),
                    )
                ),
            )
        )

        # Apply date filters
        if dfrom:
            qs = qs.filter(sale__sale_date__gte=dfrom)
        if dto:
            qs = qs.filter(sale__sale_date__lte=dto)

        if wipe:
            del_q = DailyProductSales.objects.all()
            if dfrom:
                del_q = del_q.filter(date__gte=dfrom)
            if dto:
                del_q = del_q.filter(date__lte=dto)
            deleted = del_q.delete()[0]
            self.stdout.write(self.style.WARNING(f"Wiped {deleted} DailyProductSales rows in range."))

        existing_q = DailyProductSales.objects.all()
        if dfrom:
            existing_q = existing_q.filter(date__gte=dfrom)
        if dto:
            existing_q = existing_q.filter(date__lte=dto)

        existing = {(r.date, r.product_id): r for r in existing_q}

        to_create = []
        to_update = []

        for row in qs:
            day = row["sale__sale_date"]
            product_id = row["product_id"]

            qty_sold = row["qty_sold"] or 0
            sales_amt = row["sales_amt"] or 0
            promo_flag = bool(row["promo_flag"] or 0)

            fest_name = pick_festival_name(day, festivals)
            if fest_name is None:
                fest_name = ""
            is_festive = bool(fest_name)

            key = (day, product_id)

            if key in existing:
                obj = existing[key]
                obj.qty_sold = qty_sold
                obj.sales_amt = sales_amt
                obj.promo_flag = promo_flag
                obj.is_festive = is_festive
                obj.festival_name = fest_name
                to_update.append(obj)
            else:
                to_create.append(DailyProductSales(
                    date=day,
                    product_id=product_id,
                    qty_sold=qty_sold,
                    sales_amt=sales_amt,
                    promo_flag=promo_flag,
                    is_festive=is_festive,
                    festival_name=fest_name,
                ))

        if to_create:
            DailyProductSales.objects.bulk_create(to_create, batch_size=1000)

        if to_update:
            DailyProductSales.objects.bulk_update(
                to_update,
                fields=["qty_sold", "sales_amt", "promo_flag", "is_festive", "festival_name", "updated_at"],
                batch_size=1000
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {len(to_create)}, Updated: {len(to_update)}"
        ))
