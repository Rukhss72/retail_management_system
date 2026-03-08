from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0038_saleitem_promo_flag"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="opening_stock",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
