from django.db import migrations, models
from django.db.models import Q


def backfill_promo_flags(apps, schema_editor):
    Sale = apps.get_model("retail", "Sale")
    SaleItem = apps.get_model("retail", "SaleItem")

    promo_q = Q(discount_amt__gt=0) | Q(discount_pct__gt=0)

    SaleItem.objects.filter(promo_q).update(promo_flag=True)
    SaleItem.objects.exclude(promo_q).update(promo_flag=False)

    Sale.objects.all().update(promo_flag=False)
    promo_sale_ids = SaleItem.objects.filter(promo_flag=True).values_list("sale_id", flat=True).distinct()
    Sale.objects.filter(id__in=promo_sale_ids).update(promo_flag=True)


def reverse_backfill_promo_flags(apps, schema_editor):
    Sale = apps.get_model("retail", "Sale")
    SaleItem = apps.get_model("retail", "SaleItem")
    SaleItem.objects.all().update(promo_flag=False)
    Sale.objects.all().update(promo_flag=False)


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0037_dailygroupsales_skuforecastgroupmap"),
    ]

    operations = [
        migrations.AddField(
            model_name="saleitem",
            name="promo_flag",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_promo_flags, reverse_backfill_promo_flags),
    ]
