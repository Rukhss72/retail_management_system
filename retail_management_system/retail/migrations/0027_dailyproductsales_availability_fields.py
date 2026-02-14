from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0026_trusteddevice"),
    ]

    operations = [
        migrations.AddField(
            model_name="dailyproductsales",
            name="product_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="dailyproductsales",
            name="in_stock_flag",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="dailyproductsales",
            index=models.Index(fields=["product_active"], name="retail_dai_product_6f0fd0_idx"),
        ),
        migrations.AddIndex(
            model_name="dailyproductsales",
            index=models.Index(fields=["in_stock_flag"], name="retail_dai_in_stoc_e9b7a8_idx"),
        ),
    ]
