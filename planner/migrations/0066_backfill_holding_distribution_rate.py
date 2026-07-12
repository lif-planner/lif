from django.db import migrations


def backfill_holding_distribution_rate(apps, schema_editor):
    """Copy each holdings-valued depot's account-level distribution rate onto
    every one of its holdings, so the aggregate distribution projected for
    that account is unchanged until the user (or demo data) corrects
    individual holdings to their real per-fund rates."""
    AssetAccount = apps.get_model("planner", "AssetAccount")

    accounts = AssetAccount.objects.filter(
        account_type="depot",
        depot_valuation="holdings_sum",
        depot_annual_distribution_rate__gt=0,
    ).prefetch_related("holdings")
    for account in accounts:
        account.holdings.update(
            annual_distribution_rate=account.depot_annual_distribution_rate,
            distribution_cadence=account.depot_distribution_cadence,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0065_depothold_distribution_fields'),
    ]

    operations = [
        migrations.RunPython(backfill_holding_distribution_rate, noop),
    ]
