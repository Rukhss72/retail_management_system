import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from retail.models import ForecastDaily, ForecastRun, Product


def parse_forecast_date(raw_value: str):
    value = (raw_value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {raw_value!r}")


class Command(BaseCommand):
    help = "Import forecast rows from a CSV file into ForecastRun and ForecastDaily."

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True, help="Absolute path to forecast_output.csv")
        parser.add_argument(
            "--model-name",
            default="catboost_tuned_x0.90",
            help="Model label stored on ForecastRun.",
        )
        parser.add_argument(
            "--replace-latest",
            action="store_true",
            help="Delete the latest ForecastRun before importing the new file.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        csv_path = Path(options["path"]).expanduser()
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        if options["replace_latest"]:
            latest = ForecastRun.objects.order_by("-created_at").first()
            if latest:
                latest_id = latest.id
                latest.delete()
                self.stdout.write(self.style.WARNING(f"Deleted latest ForecastRun id={latest_id}."))

        rows = []
        product_ids = set()
        forecast_dates = []

        with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"product_id", "forecast_date", "predicted_qty"}
            if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                raise CommandError(
                    f"CSV must contain columns {sorted(required)}. Found: {reader.fieldnames or []}"
                )

            for idx, row in enumerate(reader, start=2):
                try:
                    product_id = int((row.get("product_id") or "").strip())
                except ValueError as exc:
                    raise CommandError(f"Row {idx}: invalid product_id {row.get('product_id')!r}") from exc

                try:
                    forecast_date = parse_forecast_date(row.get("forecast_date", ""))
                except ValueError as exc:
                    raise CommandError(f"Row {idx}: {exc}") from exc

                try:
                    predicted_qty = Decimal((row.get("predicted_qty") or "").strip())
                except (InvalidOperation, ValueError) as exc:
                    raise CommandError(f"Row {idx}: invalid predicted_qty {row.get('predicted_qty')!r}") from exc

                rows.append((product_id, forecast_date, predicted_qty))
                product_ids.add(product_id)
                forecast_dates.append(forecast_date)

        if not rows:
            raise CommandError("CSV is empty. Nothing to import.")

        existing_products = set(Product.objects.filter(id__in=product_ids).values_list("id", flat=True))
        missing_products = sorted(product_ids - existing_products)
        if missing_products:
            raise CommandError(
                "CSV references product IDs that do not exist in the configured database: "
                f"{missing_products[:20]}"
                + (" ..." if len(missing_products) > 20 else "")
            )

        run = ForecastRun.objects.create(
            model_name=options["model_name"],
            period_start=min(forecast_dates),
            period_end=max(forecast_dates),
        )

        items = [
            ForecastDaily(
                run=run,
                product_id=product_id,
                forecast_date=forecast_date,
                predicted_qty=predicted_qty,
            )
            for product_id, forecast_date, predicted_qty in rows
        ]
        ForecastDaily.objects.bulk_create(items, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(items)} rows into ForecastRun id={run.id} "
                f"for {len(product_ids)} products from {run.period_start} to {run.period_end}."
            )
        )
