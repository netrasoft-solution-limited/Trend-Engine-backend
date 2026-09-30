"""Enrol or reset an operator's second factor from the command line.

The web flow covers the normal case: sign in with a password, enrol, done. This
exists for the two it cannot.

RESET, because a lost authenticator with no recovery codes left is otherwise
permanent — and a reset endpoint on the web would mean anyone with the password
could replace a working second factor, which is most of what the factor is for.

PRE-ENROLMENT, because self-enrolment on first sign-in means whoever knows the
password binds the authenticator. That is a fine bootstrap for a small team and
a bad one where the password has been shared to get someone started.
"""
from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand, CommandError

from apps.operations import mfa
from apps.operations.models import OperatorUser, RecoveryCode


class Command(BaseCommand):
    help = "Enrol, re-issue recovery codes for, or reset an operator's second factor."

    def add_arguments(self, parser) -> None:
        parser.add_argument("email", help="The operator account.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help=(
                "Remove the second factor so the account re-enrols on its next "
                "sign-in. For a lost authenticator with no recovery codes left."
            ),
        )
        parser.add_argument(
            "--recovery-codes",
            action="store_true",
            help="Issue a fresh set of recovery codes, invalidating the old ones.",
        )

    def handle(self, *args, **options) -> None:
        email = options["email"].strip().lower()
        user = OperatorUser.objects.filter(email=email).first()
        if user is None:
            raise CommandError(f"No operator account for {email}.")

        actor = f"{getpass.getuser()} (shell)"

        if options["reset"]:
            return self._reset(user, actor)
        if options["recovery_codes"]:
            return self._recovery_codes(user)
        return self._enrol(user)

    # ── Actions ─────────────────────────────────────────────────────────────

    def _reset(self, user: OperatorUser, actor: str) -> None:
        mfa.reset(user, actor_label=actor)
        self.stdout.write(
            self.style.WARNING(
                f"{user.email} no longer has a second factor. Until they enrol "
                f"again on their next sign-in, that account is password-only — "
                f"tell them to sign in now rather than later."
            )
        )

    def _recovery_codes(self, user: OperatorUser) -> None:
        if not user.mfa_ready:
            raise CommandError(
                f"{user.email} has no confirmed second factor, so there is "
                f"nothing to issue recovery codes against. Enrol first."
            )
        codes = mfa._issue_recovery_codes(user)
        self._print_codes(user, codes, note="The previous set no longer works.")

    def _enrol(self, user: OperatorUser) -> None:
        if user.mfa_ready:
            raise CommandError(
                f"{user.email} already has a second factor. Use --reset to "
                f"remove it, or --recovery-codes to issue fresh codes."
            )

        enrolment = mfa.begin_enrolment(user)
        self.stdout.write("\nAdd this to an authenticator app:\n")
        self.stdout.write(f"  secret : {enrolment.secret}")
        self.stdout.write(f"  uri    : {enrolment.provisioning_uri}\n")

        code = input("Enter the 6-digit code it shows: ").strip()
        try:
            codes = mfa.confirm_enrolment(user, code)
        except mfa.MfaError as exc:
            # The half-finished enrolment is left in place deliberately: the
            # secret is already in their app, so re-running the command and
            # entering the next code works rather than starting over.
            raise CommandError(f"{exc} Re-run this command to try again.") from exc

        self.stdout.write(self.style.SUCCESS(f"\n{user.email} is enrolled."))
        self._print_codes(user, codes)

    # ── Output ──────────────────────────────────────────────────────────────

    def _print_codes(self, user: OperatorUser, codes: list[str], note: str = "") -> None:
        remaining = RecoveryCode.objects.filter(
            operator_id=user.pk, used_at__isnull=True
        ).count()
        self.stdout.write(
            "\nRecovery codes — each works once, and they are stored hashed, so "
            "this is the only time they can be shown:\n"
        )
        for code in codes:
            self.stdout.write(f"    {code}")
        self.stdout.write(f"\n  {remaining} unused. {note}")
        self.stdout.write(
            self.style.WARNING(
                "  Give these to the operator now and do not keep a copy here — "
                "a terminal scrollback is not a safe place for them."
            )
        )
