from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum, Max, Case, When, IntegerField, Min
from datetime import datetime, date, timedelta
from decimal import Decimal

from retail.models import Sale, SaleItem, DailyProductSales, FestiveCalendar, Product


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
        parser.add_argument("--only-sales", action="store_true",
                            help="Only insert/update rows with sales (no zero backfill)")
        parser.add_argument("--active-only", action="store_true",
                            help="Only include products where Product.is_active=True")
        parser.add_argument("--use-current-stock", action="store_true",
                            help="Fill in_stock_flag with current Product.qty_in_stock for ALL dates")

    @transaction.atomic
    def handle(self, *args, **options):
        date_from = options["date_from"]
        date_to = options["date_to"]
        wipe = options["wipe"]
        only_sales = options["only_sales"]
        active_only = options["active_only"]
        use_current_stock = options["use_current_stock"]

        def parse_ymd(s):
            return datetime.strptime(s, "%Y-%m-%d").date()

        dfrom = parse_ymd(date_from) if date_from else None
        dto = parse_ymd(date_to) if date_to else None

        festivals = list(FestiveCalendar.objects.all())

        if not dfrom or not dto:
            agg = Sale.objects.aggregate(min_date=Min("sale_date"), max_date=Max("sale_date"))
            if not dfrom:
                dfrom = agg["min_date"]
            if not dto:
                dto = agg["max_date"]

        if not dfrom or not dto:
            self.stdout.write(self.style.WARNING("No sales found and no --from/--to provided. Nothing to do."))
            return

        if dto < dfrom:
            self.stdout.write(self.style.ERROR("--to must be on/after --from."))
            return

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
        qs = qs.filter(sale__sale_date__gte=dfrom, sale__sale_date__lte=dto)

        if wipe:
            del_q = DailyProductSales.objects.all()
            del_q = del_q.filter(date__gte=dfrom, date__lte=dto)
            deleted = del_q.delete()[0]
            self.stdout.write(self.style.WARNING(f"Wiped {deleted} DailyProductSales rows in range."))

        existing_q = DailyProductSales.objects.filter(date__gte=dfrom, date__lte=dto)

        existing = {(r.date, r.product_id): r for r in existing_q}

        to_create = []
        to_update = []

        product_qs = Product.objects.all()
        if active_only:
            product_qs = product_qs.filter(is_active=True)
        products = list(product_qs)
        product_map = {p.id: p for p in products}

        sales_map = {}
        for row in qs:
            sales_map[(row["sale__sale_date"], row["product_id"])] = {
                "qty_sold": row["qty_sold"] or 0,
                "sales_amt": row["sales_amt"] or Decimal("0.00"),
                "promo_flag": bool(row["promo_flag"] or 0),
            }

        dates = []
        d = dfrom
        while d <= dto:
            dates.append(d)
            d = d + timedelta(days=1)

        festival_by_date = {}
        for day in dates:
            fest_name = pick_festival_name(day, festivals)
            festival_by_date[day] = fest_name or ""

        def get_stock_fields(product):
            if not use_current_stock:
                return None
            stock = product.qty_in_stock
            return bool(stock > 0)

        if only_sales:
            for (day, product_id), s in sales_map.items():
                product = product_map.get(product_id)
                if not product:
                    continue
                fest_name = festival_by_date.get(day, "")
                is_festive = bool(fest_name)
                in_stock_flag = get_stock_fields(product)

                key = (day, product_id)
                if key in existing:
                    obj = existing[key]
                    obj.qty_sold = s["qty_sold"]
                    obj.sales_amt = s["sales_amt"]
                    obj.promo_flag = s["promo_flag"]
                    obj.is_festive = is_festive
                    obj.festival_name = fest_name
                    obj.category_id = product.category_id
                    obj.product_active = product.is_active
                    obj.in_stock_flag = in_stock_flag
                    to_update.append(obj)
                else:
                    to_create.append(DailyProductSales(
                        date=day,
                        product_id=product_id,
                        category_id=product.category_id,
                        qty_sold=s["qty_sold"],
                        sales_amt=s["sales_amt"],
                        promo_flag=s["promo_flag"],
                        is_festive=is_festive,
                        festival_name=fest_name,
                        product_active=product.is_active,
                        in_stock_flag=in_stock_flag,
                    ))
        else:
            for day in dates:
                fest_name = festival_by_date[day]
                is_festive = bool(fest_name)
                for product in products:
                    s = sales_map.get((day, product.id))
                    qty_sold = s["qty_sold"] if s else 0
                    sales_amt = s["sales_amt"] if s else Decimal("0.00")
                    promo_flag = s["promo_flag"] if s else False
                    in_stock_flag = get_stock_fields(product)

                    key = (day, product.id)
                    if key in existing:
                        obj = existing[key]
                        obj.qty_sold = qty_sold
                        obj.sales_amt = sales_amt
                        obj.promo_flag = promo_flag
                        obj.is_festive = is_festive
                        obj.festival_name = fest_name
                        obj.category_id = product.category_id
                        obj.product_active = product.is_active
                        obj.in_stock_flag = in_stock_flag
                        to_update.append(obj)
                    else:
                        to_create.append(DailyProductSales(
                            date=day,
                            product_id=product.id,
                            category_id=product.category_id,
                            qty_sold=qty_sold,
                            sales_amt=sales_amt,
                            promo_flag=promo_flag,
                            is_festive=is_festive,
                            festival_name=fest_name,
                            product_active=product.is_active,
                        in_stock_flag=in_stock_flag,
                        ))

        if to_create:
            DailyProductSales.objects.bulk_create(to_create, batch_size=1000)

        if to_update:
            DailyProductSales.objects.bulk_update(
                to_update,
                fields=[
                    "qty_sold",
                    "sales_amt",
                    "promo_flag",
                    "is_festive",
                    "festival_name",
                    "category_id",
                    "product_active",
                    "in_stock_flag",
                    "updated_at",
                ],
                batch_size=1000
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {len(to_create)}, Updated: {len(to_update)}"
        ))
