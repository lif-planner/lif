from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("planner", "0023_snapshotreviewaction"),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="annual_inflation_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("2.00"),
                help_text="Annual inflation assumption used to show long-term projections in today's money.",
                max_digits=5,
            ),
        ),
    ]
