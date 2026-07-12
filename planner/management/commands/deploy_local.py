from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the local-first deployment flow: backup, migrate, static assets, checks, and smoke test."

    def add_arguments(self, parser):
        parser.add_argument("--skip-backup", action="store_true")
        parser.add_argument("--skip-collectstatic", action="store_true")
        parser.add_argument("--skip-smoke-test", action="store_true")

    def handle(self, *args, **options):
        if not options["skip_backup"]:
            call_command("backup_data", label="pre-deploy")

        call_command("migrate")
        if not options["skip_collectstatic"]:
            call_command("collectstatic", interactive=False, verbosity=0)

        call_command("check_production")

        if not options["skip_smoke_test"]:
            call_command("smoke_test")

        self.stdout.write(self.style.SUCCESS("Local deployment flow completed."))
