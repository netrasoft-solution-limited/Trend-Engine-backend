"""Transactional email over Resend's HTTP API.

A Django email backend rather than SMTP, and the reason is diagnosis. SMTP
reports failure as a three-digit code and a free-text line, so "the invitation
never arrived" becomes an afternoon of reading mail logs. This API answers with
a structured error naming the cause — an unverified sending domain, a
malformed address, a rate limit — and the send path logs it against the
recipient. PRD §6.8 makes invitations the only way into the portal, so a
silent email failure is a client who cannot get in.

It sits in `config/` beside `hashers.py` for the same reason: both are
infrastructure adapters that Django loads by settings path, so no application
layer imports them and the dependency rules are unaffected.

ON SENDING DOMAINS. Resend will only send `from` a domain verified in the
account. That is a DNS task on the recipient domain, not something code can
work around, and it is the failure most likely to appear first — so an
unverified sender is called out by name rather than left as a 403.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

API = "https://api.resend.com/emails"

#: Resend's own test sender. Works with no domain verified, but ONLY to the
#: account owner's address — useful for proving the wiring, useless for a
#: client invitation.
TEST_SENDER = "onboarding@resend.dev"


class ResendBackend(BaseEmailBackend):
    """Send via Resend. One request per message.

    Resend has a batch endpoint, and this deliberately does not use it: a batch
    answers with one status for the whole call, so a single bad address would
    make the rest indistinguishable from failures. Volumes here are invitations
    and password resets — a handful a day — so per-message requests cost
    nothing and say exactly which recipient failed.
    """

    def __init__(
        self,
        fail_silently: bool = False,
        api_key: str | None = None,
        transport: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = api_key if api_key is not None else (
            getattr(settings, "RESEND_API_KEY", "") or ""
        )
        self.timeout = getattr(settings, "EMAIL_TIMEOUT", 10) or 10
        # For tests, mirroring the connectors: the suite drives an
        # httpx.MockTransport so the mapping and the error handling are covered
        # without sending anything. `get_connection(transport=...)` passes it.
        self._transport = transport

    def send_messages(self, email_messages) -> int:
        """Returns the number SENT, which is the contract Django relies on.

        A caller that checks the return value is asking "did this go out", and
        counting attempts instead of successes turns a dead mail provider into
        a silent one.
        """
        if not email_messages:
            return 0

        if not self.api_key:
            message = (
                "RESEND_API_KEY is not set, so no mail can be sent. Set it, or "
                "point DJANGO_EMAIL_BACKEND at the console backend for local work."
            )
            if not self.fail_silently:
                raise RuntimeError(message)
            logger.error(message)
            return 0

        import httpx

        sent = 0
        with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
            for message in email_messages:
                if self._send_one(client, message):
                    sent += 1
        return sent

    # ── One message ─────────────────────────────────────────────────────────

    def _send_one(self, client, message) -> bool:
        payload = self._payload(message)
        if not payload["to"]:
            # Django tolerates a message with no recipients; sending one is a
            # wasted request and an error from the API.
            return False

        try:
            response = client.post(
                API,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except Exception as exc:
            logger.error(
                "Resend request failed for %s: %s", payload["to"], exc, exc_info=True
            )
            if not self.fail_silently:
                raise
            return False

        if response.status_code >= 400:
            self._report(response, payload)
            if not self.fail_silently:
                raise RuntimeError(
                    f"Resend refused the message to {payload['to']}: "
                    f"{response.status_code} {response.text[:300]}"
                )
            return False

        logger.info(
            "Sent %r to %s via Resend (id %s)",
            payload.get("subject", ""),
            payload["to"],
            response.json().get("id", "?"),
        )
        return True

    def _report(self, response, payload: dict) -> None:
        """Say what actually went wrong, in terms someone can act on."""
        try:
            body = response.json()
            detail = body.get("message") or body.get("name") or response.text[:200]
        except ValueError:
            detail = response.text[:200]

        sender = payload.get("from", "")
        lowered = str(detail).lower()

        if response.status_code in (401, 403) and "domain" in lowered:
            logger.error(
                "Resend will not send from %s: %s. The sending domain has to be "
                "verified in the Resend dashboard, which means adding its DKIM "
                "and SPF records to DNS. Until then, only %s works, and only to "
                "the account owner's own address.",
                sender, detail, TEST_SENDER,
            )
        elif response.status_code in (401, 403):
            logger.error("Resend rejected the API key: %s", detail)
        elif response.status_code == 422:
            logger.error(
                "Resend rejected the message to %s as invalid: %s",
                payload["to"], detail,
            )
        elif response.status_code == 429:
            logger.warning("Resend rate limit reached sending to %s: %s", payload["to"], detail)
        else:
            logger.error(
                "Resend returned %s sending to %s: %s",
                response.status_code, payload["to"], detail,
            )

    # ── Mapping ─────────────────────────────────────────────────────────────

    @staticmethod
    def _payload(message) -> dict:
        """A Django EmailMessage as Resend's request body."""
        payload: dict[str, Any] = {
            "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
            "to": list(message.to or []),
            "subject": message.subject or "",
        }

        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if message.reply_to:
            payload["reply_to"] = list(message.reply_to)

        # `content_subtype` is "html" when someone built the message as HTML
        # directly; `alternatives` holds the HTML half of a multipart message.
        if message.content_subtype == "html":
            payload["html"] = message.body or ""
        else:
            payload["text"] = message.body or ""
            for content, mimetype in getattr(message, "alternatives", None) or []:
                if mimetype == "text/html":
                    payload["html"] = content
                    break

        attachments = [
            a for a in (ResendBackend._attachment(a) for a in message.attachments or []) if a
        ]
        if attachments:
            payload["attachments"] = attachments

        headers = {
            k: v
            for k, v in (message.extra_headers or {}).items()
            # Resend sets these itself; passing them through is rejected.
            if k.lower() not in {"from", "to", "cc", "bcc", "subject", "reply-to"}
        }
        if headers:
            payload["headers"] = headers

        return payload

    @staticmethod
    def _attachment(attachment) -> dict | None:
        """Django gives a 3-tuple, or a MIMEBase for anything richer.

        A MIMEBase is skipped rather than guessed at: nothing in this system
        attaches one, and inventing a mapping for a case with no caller is how
        untested code ends up in a send path.
        """
        if not isinstance(attachment, (tuple, list)) or len(attachment) < 2:
            logger.warning("Skipping an attachment Resend cannot take: %r", type(attachment))
            return None

        filename, content = attachment[0], attachment[1]
        if isinstance(content, str):
            content = content.encode("utf-8")

        # The mimetype was being discarded, leaving Resend to infer the type
        # from the filename. That is fine for `brief.pdf` and wrong the moment a
        # filename is unusual or absent — and we already hold the answer, so
        # guessing from a string was never the better option.
        mimetype = attachment[2] if len(attachment) > 2 else None
        payload = {
            "filename": filename or "attachment",
            "content": base64.b64encode(content).decode("ascii"),
        }
        if mimetype:
            # Resend takes the media type only, not the charset parameter our
            # text renderers carry (`text/csv; charset=utf-8`).
            payload["content_type"] = str(mimetype).split(";")[0].strip()
        return payload
