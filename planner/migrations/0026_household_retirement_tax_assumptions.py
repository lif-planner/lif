from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("planner", "0025_backupevent"),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="capital_gains_tax_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("25.00"),
                help_text="Simple planning assumption for tax drag on ETF/capital withdrawals.",
                max_digits=5,
            ),
        ),
        migrations.AddField(
            model_name="household",
            name="church_tax_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Optional church tax planning rate. Keep at 0 when not applicable.",
                max_digits=5,
            ),
        ),
        migrations.AddField(
            model_name="household",
            name="health_insurance_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("11.00"),
                help_text="Simple planning assumption for health and care insurance on retirement income.",
                max_digits=5,
            ),
        ),
        migrations.AddField(
            model_name="household",
            name="pension_tax_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("18.00"),
                help_text="Simple planning assumption for tax and deductions on pension income.",
                max_digits=5,
            ),
        ),
        migrations.AddField(
            model_name="household",
            name="solidarity_surcharge_rate",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Optional solidarity surcharge planning rate. Keep at 0 when not applicable.",
                max_digits=5,
            ),
        ),
    ]
