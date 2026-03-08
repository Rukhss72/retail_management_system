from django.db import migrations, models
import django.db.models.deletion


def backfill_product_supplier(apps, schema_editor):
    Product = apps.get_model("retail", "Product")
    ProductSupplier = apps.get_model("retail", "ProductSupplier")

    for product in Product.objects.all().only("id", "supplier_id"):
        primary_link = (
            ProductSupplier.objects.filter(product_id=product.id, is_primary=True)
            .order_by("id")
            .first()
        )
        link = primary_link or ProductSupplier.objects.filter(product_id=product.id).order_by("id").first()
        if link and link.supplier_id:
            product.supplier_id = link.supplier_id
            product.save(update_fields=["supplier_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("retail", "0039_product_opening_stock"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="supplier",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="products",
                to="retail.supplier",
            ),
        ),
        migrations.RunPython(backfill_product_supplier, migrations.RunPython.noop),
    ]
