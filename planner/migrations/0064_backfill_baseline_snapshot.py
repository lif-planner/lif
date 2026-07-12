from django.db import migrations


def backfill_baseline_snapshot(apps, schema_editor):
    Household = apps.get_model("planner", "Household")
    Snapshot = apps.get_model("planner", "Snapshot")

    for household in Household.objects.all():
        snapshots = Snapshot.objects.filter(household=household).order_by("snapshot_date", "created_at")
        if not snapshots.exists() or snapshots.filter(is_baseline=True).exists():
            continue
        earliest = snapshots.first()
        earliest.is_baseline = True
        earliest.snapshot_type = "baseline"
        earliest.save(update_fields=["is_baseline", "snapshot_type"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0063_remove_changelogentry_source'),
    ]

    operations = [
        migrations.RunPython(backfill_baseline_snapshot, noop),
    ]
