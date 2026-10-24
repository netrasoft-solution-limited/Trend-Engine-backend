"""Register the experts who may sign off health and scientific outputs.

    manage.py expert_reviewer list
    manage.py expert_reviewer add --name "Dr Jane Doe" \
        --qualification "RD, PhD nutrition science" --email jane@example.com
    manage.py expert_reviewer deactivate --email jane@example.com

A sign-off must name an active reviewer from this register, and the operator
recording it cannot be that reviewer. Deactivating keeps past sign-offs intact.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.outputs import reviewers
from apps.outputs.models import ExpertReviewer


class Command(BaseCommand):
    help = "List, add or deactivate registered expert reviewers."

    def add_arguments(self, parser) -> None:
        parser.add_argument("action", choices=["list", "add", "deactivate"])
        parser.add_argument("--name")
        parser.add_argument("--qualification")
        parser.add_argument("--email")
        parser.add_argument("--actor", default="", help="Who is registering them.")

    def handle(self, *args, **options) -> None:
        action = options["action"]
        if action == "list":
            for r in ExpertReviewer.objects.all():
                state = "active" if r.is_active else "inactive"
                self.stdout.write(f"{state:<9} {r.name} — {r.qualification} <{r.email}>")
            return

        if not options["email"]:
            raise CommandError("--email is required.")
        actor = options["actor"] or "expert_reviewer command"
        try:
            if action == "add":
                reviewer = reviewers.register(
                    name=options["name"] or "", qualification=options["qualification"] or "",
                    email=options["email"], actor_label=actor,
                )
                message = f"Registered expert reviewer {reviewer.name} ({reviewer.qualification})"
            else:
                reviewer = ExpertReviewer.objects.filter(
                    email=options["email"].strip().lower()
                ).first()
                if reviewer is None:
                    raise CommandError(f"No reviewer {options['email']}.")
                reviewers.set_active(reviewer, False, actor_label=actor)
                message = f"Deactivated expert reviewer {reviewer.name}"
        except reviewers.ReviewerError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(message))
