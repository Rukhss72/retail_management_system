from django.db import migrations, models


def backfill_supplier_categories(apps, schema_editor):
    Supplier = apps.get_model("retail", "Supplier")
    Product = apps.get_model("retail", "Product")

    for supplier in Supplier.objects.all().only("id", "category_id"):
        category_ids = set()

        if supplier.category_id:
            category_ids.add(supplier.category_id)

        product_cat_ids = Product.objects.filter(
            supplier_id=supplier.id,
            category_id__isnull=False,
        ).values_list("category_id", flat=True).distinct()
        category_ids.update(product_cat_ids)

        if category_ids:
            supplier.categories.add(*category_ids)


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0040_product_supplier_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplier",
            name="categories",
            field=models.ManyToManyField(blank=True, related_name="suppliers_multi", to="retail.category"),
        ),
        migrations.RunPython(backfill_supplier_categories, migrations.RunPython.noop),
    ]
