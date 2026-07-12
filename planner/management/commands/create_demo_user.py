import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create or update a guarded demo login user for demo deployments."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.environ.get("LIF_DEMO_USERNAME", "demo"))
        parser.add_argument("--password", default=os.environ.get("LIF_DEMO_PASSWORD"))
        parser.add_argument("--email", default=os.environ.get("LIF_DEMO_EMAIL", "demo@example.invalid"))
        parser.add_argument("--staff", action="store_true", default=False)

    def handle(self, *args, **options):
        allowed = os.environ.get("LIF_ALLOW_DEMO_USER", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not settings.DEBUG and not allowed:
            raise CommandError("Refusing to create a demo user unless LIF_ALLOW_DEMO_USER=1 is set.")

        username = options["username"].strip()
        password = options["password"]
        email = options["email"].strip()

        if not username:
            raise CommandError("Demo username must not be empty.")
        if not password:
            raise CommandError("Set LIF_DEMO_PASSWORD or pass --password.")
        if len(password) < 12:
            raise CommandError("Demo password must be at least 12 characters long.")

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "is_staff": options["staff"],
                "is_superuser": False,
            },
        )
        user.email = email
        user.is_staff = options["staff"]
        user.is_superuser = False
        user.set_password(password)
        user.save()

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} demo user '{username}'."))
