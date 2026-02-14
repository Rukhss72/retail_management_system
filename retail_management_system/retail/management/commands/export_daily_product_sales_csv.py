import csv
from django.core.management.base import BaseCommand
from django.utils.timezone import localtime

from retail.models import DailyProductSales


class Command(BaseCommand):
    help = "Export DailyProductSales to CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            dest="out_path",
            required=True,
            help="Output CSV path (e.g. C:\\Users\\baich\\Downloads\\ruk_dataset.csv)",
        )
        parser.add_argument(
            "--chunk",
            dest="chunk_size",
            type=int,
            default=2000,
            help="Rows per DB fetch when streaming (default: 2000).",
        )

    def handle(self, *args, **options):
        out_path = options["out_path"]
        chunk_size = options["chunk_size"]

        fields = [
            "id",
            "date",
            "product_id",
            "qty_sold",
            "sales_amt",
            "promo_flag",
            "is_festive",
            "festival_name",
            "product_active",
            "in_stock_flag",
            "created_at",
            "updated_at",
        ]

        qs = DailyProductSales.objects.order_by("date", "product_id").values_list(*fields)

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(fields)

            count = 0
            for row in qs.iterator(chunk_size=chunk_size):
                row = list(row)
                # Format datetimes as local time ISO
                for idx in (10, 11):  # created_at, updated_at
                    if row[idx]:
                        row[idx] = localtime(row[idx]).isoformat(sep=" ")
                writer.writerow(row)
                count += 1

        self.stdout.write(self.style.SUCCESS(f"Exported {count} rows to {out_path}"))
