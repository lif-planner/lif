import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError, BaseCommand


class Command(BaseCommand):
    help = "Create a timestamped backup of the local SQLite database."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", type=Path, default=settings.BACKUP_DIR)
        parser.add_argument("--label", default="")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if database["ENGINE"] != "django.db.backends.sqlite3":
            raise CommandError("backup_data currently supports SQLite only.")

        source = Path(str(database["NAME"]))
        if not source.exists():
            raise CommandError(f"Database file does not exist: {source}")

        output_dir = options["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)

        label = f"-{options['label']}" if options["label"] else ""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = source.suffix if source.suffix else ".sqlite3"
        stem = source.stem
        target = output_dir / f"{stem}-{timestamp}{label}{suffix}"

        shutil.copy2(source, target)
        self.stdout.write(self.style.SUCCESS(f"Backup created: {target}"))
