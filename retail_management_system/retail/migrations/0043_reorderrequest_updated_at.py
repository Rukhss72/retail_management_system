from django.db import migrations, models


def backfill_reorderrequest_updated_at(apps, schema_editor):
    ReorderRequest = apps.get_model("retail", "ReorderRequest")
    ReorderRequest.objects.filter(updated_at__isnull=True).update(updated_at=models.F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0042_userprofile_role_admin"),
    ]

    operations = [
        migrations.AddField(
            model_name="reorderrequest",
            name="updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_reorderrequest_updated_at, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="reorderrequest",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
    ]
