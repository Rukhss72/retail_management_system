import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from retail.models import Product, Sale, SaleItem


# Accepts the date formats used by the source dataset exports.
def parse_date(value: str):
    v = (value or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


# Parses decimal-like CSV values defensively and falls back to a known default.
def parse_decimal(value, default="0.00"):
    try:
        return Decimal(str(value).strip() if value is not None else default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


# Normalizes all currency math to two decimal places before persisting.
def quant2(value: Decimal):
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Accepts a few loose truthy values commonly seen in exported CSVs.
def parse_bool(value):
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


# Returns the first populated key from alternative column names.
def first_present(row, *keys, default=""):
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return default


@dataclass
class ItemRow:
    product: Product
    qty: int
    unit_price: Decimal
    discount_pct: Decimal
    item_promo: bool
    gross: Decimal
    discount_amt: Decimal
    line_total: Decimal


class Command(BaseCommand):
    help = "Import one full sales CSV and backfill retail_sale + retail_saleitem."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to full source CSV")
        parser.add_argument(
            "--create-missing-users",
            action="store_true",
            help="Create missing cashier usernames with unusable passwords.",
        )
        parser.add_argument(
            "--replace-receipts",
            action="store_true",
            help="Replace existing receipts found in CSV (default: update sale + replace its items).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and summarize only; do not write to DB.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        file_path = Path(options["file"])
        create_missing_users = options["create_missing_users"]
        replace_receipts = options["replace_receipts"]
        dry_run = options["dry_run"]

        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        with file_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = set(reader.fieldnames or [])
            required_base = {"receipt_no", "sale_date", "cashier_username", "payment_method"}
            missing = sorted(required_base - headers)
            if missing:
                raise CommandError(f"Missing required columns: {', '.join(missing)}")

            product_header_ok = "sku" in headers or "product_id" in headers
            qty_header_ok = "qty" in headers or "qty_sold" in headers
            price_header_ok = "unit_price" in headers or "sales_amt" in headers
            if not product_header_ok:
                raise CommandError("Missing required product column: provide either 'sku' or 'product_id'")
            if not qty_header_ok:
                raise CommandError("Missing required quantity column: provide either 'qty' or 'qty_sold'")
            if not price_header_ok:
                raise CommandError("Missing required price column: provide either 'unit_price' or 'sales_amt'")

            rows = list(reader)

        if not rows:
            self.stdout.write(self.style.WARNING("CSV is empty. Nothing to import."))
            return

        # Preload all referenced users/products once so row validation stays cheap.
        usernames = {str(r.get("cashier_username", "")).strip() for r in rows if str(r.get("cashier_username", "")).strip()}
        skus = {str(r.get("sku", "")).strip() for r in rows if str(r.get("sku", "")).strip()}
        product_ids = {int(str(r.get("product_id", "")).strip()) for r in rows if str(r.get("product_id", "")).strip().isdigit()}

        user_model = get_user_model()
        user_map = {u.username: u for u in user_model.objects.filter(username__in=usernames)}
        products = Product.objects.filter(sku__in=skus) if skus else Product.objects.none()
        products_by_id = Product.objects.filter(id__in=product_ids) if product_ids else Product.objects.none()
        product_map = {p.sku: p for p in products}
        product_id_map = {p.id: p for p in products_by_id}

        missing_users = sorted(usernames - set(user_map.keys()))
        created_users = 0
        if missing_users and create_missing_users and not dry_run:
            for username in missing_users:
                u, created = user_model.objects.get_or_create(username=username)
                if created:
                    u.set_unusable_password()
                    u.save(update_fields=["password"])
                    created_users += 1
                user_map[username] = u
            missing_users = sorted(usernames - set(user_map.keys()))

        missing_skus = sorted(skus - set(product_map.keys()))
        missing_product_ids = sorted(product_ids - set(product_id_map.keys()))

        grouped = defaultdict(lambda: {"meta": None, "items": []})
        skipped_rows = 0
        bad_rows = 0
        warnings = 0

        for idx, row in enumerate(rows, start=2):
            receipt_no = str(row.get("receipt_no", "")).strip()
            cashier_username = str(row.get("cashier_username", "")).strip()
            sku = str(row.get("sku", "")).strip()
            product_id_raw = str(row.get("product_id", "")).strip()

            if not receipt_no or not cashier_username or (not sku and not product_id_raw):
                bad_rows += 1
                continue

            cashier = user_map.get(cashier_username)
            product = product_map.get(sku)
            if not product and product_id_raw.isdigit():
                product = product_id_map.get(int(product_id_raw))
            if not cashier or not product:
                skipped_rows += 1
                continue

            sale_date = parse_date(row.get("sale_date", ""))
            if not sale_date:
                bad_rows += 1
                continue

            payment_method = str(row.get("payment_method", "cash")).strip().lower()
            valid_methods = {m[0] for m in Sale.PaymentMethod.choices}
            if payment_method not in valid_methods:
                payment_method = "other"

            try:
                qty = int(parse_decimal(first_present(row, "qty", "qty_sold", default="0"), "0"))
            except ValueError:
                qty = 0
            if qty <= 0:
                bad_rows += 1
                continue

            if "unit_price" in headers:
                unit_price = quant2(parse_decimal(row.get("unit_price", "0"), "0"))
            else:
                sales_amt = quant2(parse_decimal(first_present(row, "sales_amt", default="0"), "0"))
                unit_price = quant2(sales_amt / Decimal(qty)) if qty > 0 else Decimal("0.00")
            if unit_price < 0:
                unit_price = Decimal("0.00")

            discount_pct = quant2(parse_decimal(row.get("discount_pct", "0"), "0"))
            if discount_pct < 0:
                discount_pct = Decimal("0.00")
            if discount_pct > 100:
                discount_pct = Decimal("100.00")

            gross = quant2(unit_price * Decimal(qty))
            discount_amt = quant2((gross * discount_pct) / Decimal("100"))
            if discount_amt > gross:
                discount_amt = gross
            line_total = quant2(gross - discount_amt)

            row_promo = parse_bool(row.get("promo_flag", "0"))
            item_promo = bool(row_promo or discount_pct > 0 or discount_amt > 0)

            meta = grouped[receipt_no]["meta"]
            if meta is None:
                # First row for a receipt establishes the sale header metadata.
                grouped[receipt_no]["meta"] = {
                    "receipt_no": receipt_no,
                    "sale_date": sale_date,
                    "cashier": cashier,
                    "payment_method": payment_method,
                    "customer_name": str(row.get("customer_name", "") or "").strip(),
                    "customer_phone": str(row.get("customer_phone", "") or "").strip(),
                }
            else:
                if (
                    meta["sale_date"] != sale_date
                    or meta["cashier"].id != cashier.id
                    or meta["payment_method"] != payment_method
                ):
                    warnings += 1

            grouped[receipt_no]["items"].append(
                ItemRow(
                    product=product,
                    qty=qty,
                    unit_price=unit_price,
                    discount_pct=discount_pct,
                    item_promo=item_promo,
                    gross=gross,
                    discount_amt=discount_amt,
                    line_total=line_total,
                )
            )

        receipt_count = len(grouped)
        item_count = sum(len(v["items"]) for v in grouped.values())

        self.stdout.write(
            self.style.WARNING(
                f"Parsed rows={len(rows)}, receipts={receipt_count}, importable_items={item_count}, "
                f"skipped_missing_ref={skipped_rows}, bad_rows={bad_rows}, warnings={warnings}"
            )
        )
        if created_users:
            self.stdout.write(self.style.WARNING(f"Created missing users: {created_users}"))
        if missing_users:
            self.stdout.write(self.style.WARNING(f"Missing users: {len(missing_users)} (first 20) {missing_users[:20]}"))
        if missing_skus:
            self.stdout.write(self.style.WARNING(f"Missing SKUs: {len(missing_skus)} (first 20) {missing_skus[:20]}"))
        if missing_product_ids:
            self.stdout.write(
                self.style.WARNING(f"Missing product_ids: {len(missing_product_ids)} (first 20) {missing_product_ids[:20]}")
            )

        if dry_run:
            self.stdout.write(self.style.SUCCESS("Dry run complete. No DB changes made."))
            transaction.set_rollback(True)
            return

        sales_created = 0
        sales_updated = 0
        items_created = 0

        for payload in grouped.values():
            meta = payload["meta"]
            items = payload["items"]
            if not items:
                continue

            defaults = {
                "cashier": meta["cashier"],
                "sale_date": meta["sale_date"],
                "payment_method": meta["payment_method"],
                "customer_name": meta["customer_name"],
                "customer_phone": meta["customer_phone"],
            }
            sale, created = Sale.objects.update_or_create(receipt_no=meta["receipt_no"], defaults=defaults)
            if created:
                sales_created += 1
            else:
                sales_updated += 1

            # Rebuild item rows from the CSV payload so repeated imports stay deterministic.
            if replace_receipts:
                sale.items.all().delete()
            else:
                # Default behavior still replaces items to keep deterministic backfill.
                sale.items.all().delete()

            subtotal = Decimal("0.00")
            discount = Decimal("0.00")
            total = Decimal("0.00")
            has_promo = False

            for it in items:
                SaleItem.objects.create(
                    sale=sale,
                    product=it.product,
                    qty=it.qty,
                    unit_price=it.unit_price,
                    discount_pct=it.discount_pct,
                    discount_amt=it.discount_amt,
                )
                items_created += 1
                subtotal = quant2(subtotal + it.gross)
                discount = quant2(discount + it.discount_amt)
                total = quant2(total + it.line_total)
                has_promo = has_promo or it.item_promo

            sale.subtotal = subtotal
            sale.discount = discount
            sale.total = total
            sale.promo_flag = has_promo
            sale.save(update_fields=["subtotal", "discount", "total", "promo_flag"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Import complete: sales_created={sales_created}, sales_updated={sales_updated}, "
                f"items_created={items_created}, skipped_missing_ref={skipped_rows}, bad_rows={bad_rows}"
            )
        )
