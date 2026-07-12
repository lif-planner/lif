from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("planner", "0024_household_annual_inflation_rate"),
    ]

    operations = [
        migrations.CreateModel(
            name="BackupEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("backup", "Backup"), ("restore", "Restore")], max_length=20)),
                ("status", models.CharField(choices=[("started", "Started"), ("succeeded", "Succeeded"), ("failed", "Failed")], max_length=20)),
                ("filename", models.CharField(blank=True, max_length=255)),
                ("pre_restore_filename", models.CharField(blank=True, max_length=255)),
                ("detail", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
