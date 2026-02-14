from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0025_saleitem_discount_amt_saleitem_discount_pct"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrustedDevice",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("device_id", models.CharField(max_length=64)),
                ("token_hash", models.CharField(max_length=64)),
                ("user_agent", models.TextField(blank=True)),
                ("ip_at_creation", models.GenericIPAddressField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField()),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="trusted_devices", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["user", "device_id"], name="retail_tru_user_id_7b7a6d_idx"),
                    models.Index(fields=["expires_at"], name="retail_tru_expires_2f6f72_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="trusteddevice",
            constraint=models.UniqueConstraint(fields=("user", "device_id"), name="uniq_trusted_device_per_user"),
        ),
    ]
