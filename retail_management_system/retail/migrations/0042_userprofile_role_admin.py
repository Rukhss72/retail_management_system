from django.db import migrations, models


def forward_set_admin_role(apps, schema_editor):
    UserProfile = apps.get_model("retail", "UserProfile")
    UserProfile.objects.filter(role="owner").update(role="admin")


def backward_restore_owner_role(apps, schema_editor):
    UserProfile = apps.get_model("retail", "UserProfile")
    UserProfile.objects.filter(role="admin").update(role="owner")


class Migration(migrations.Migration):

    dependencies = [
        ("retail", "0041_supplier_categories_m2m"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(choices=[("admin", "Admin"), ("staff", "Staff")], default="staff", max_length=10),
        ),
        migrations.RunPython(forward_set_admin_role, backward_restore_owner_role),
    ]
