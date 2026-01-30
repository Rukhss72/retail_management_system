from django.db import migrations
from django.db.models import DateField
from django.db.models.functions import Cast


def backfill_sale_date(apps, schema_editor):
    Sale = apps.get_model("retail", "Sale")
    Sale.objects.update(sale_date=Cast("created_at", output_field=DateField()))


class Migration(migrations.Migration):
    dependencies = [
        ("retail", "0023_alter_dailyproductsales_festival_name"),
    ]

    operations = [
        migrations.RunPython(backfill_sale_date, reverse_code=migrations.RunPython.noop),
    ]
