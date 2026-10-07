"""Transactional email over Resend's HTTP API.

PRD §6.8 makes an invitation the only way into the portal, so a send that fails
quietly is a client who cannot get in. Two things therefore matter more than
the mapping: that `send_messages` counts what was SENT rather than what was
attempted, and that an unverified sending domain — the failure most likely to
appear first on a new deployment — is reported as itself rather than as a 403.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection

BACKEND = "config.email.ResendBackend"


def backend(*responses, **kwargs):
    """A ResendBackend wired to canned HTTP responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen.append(request)
        return responses[min(len(handler.seen) - 1, len(responses) - 1)]

    handler.seen = []

    connection = get_connection(
        backend=BACKEND,
        api_key=kwargs.pop("api_key", "re_test"),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    connection.handler = handler
    return connection


def accepted(email_id="e-1") -> httpx.Response:
    return httpx.Response(200, json={"id": email_id})


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content)


def message(**kwargs) -> EmailMessage:
    defaults = dict(
        subject="You have been invited",
        body="Follow this link to set your password.",
        from_email="Trend Engine <no-reply@example.test>",
        to=["dana@jarrow.example"],
    )
    defaults.update(kwargs)
    return EmailMessage(**defaults)


# ── The count is the contract ───────────────────────────────────────────────


def test_a_sent_message_counts_as_one():
    connection = backend(accepted())

    assert connection.send_messages([message()]) == 1


def test_a_refused_message_counts_as_zero():
    """Counting attempts instead of successes turns a dead mail provider into
    a silent one."""
    connection = backend(
        httpx.Response(403, json={"message": "The domain is not verified"}),
        fail_silently=True,
    )

    assert connection.send_messages([message()]) == 0


def test_each_message_is_its_own_request():
    """Resend has a batch endpoint, and one status for a whole batch would make
    a single bad address indistinguishable from the rest failing."""
    connection = backend(accepted())

    sent = connection.send_messages([message(), message(to=["priya@jarrow.example"])])

    assert sent == 2
    assert len(connection.handler.seen) == 2


def test_a_message_with_no_recipients_is_not_sent():
    connection = backend(accepted())

    assert connection.send_messages([message(to=[])]) == 0
    assert connection.handler.seen == []


def test_no_messages_makes_no_requests():
    connection = backend(accepted())

    assert connection.send_messages([]) == 0
    assert connection.handler.seen == []


# ── Mapping ─────────────────────────────────────────────────────────────────


def test_the_essentials_are_mapped():
    connection = backend(accepted())
    connection.send_messages([message()])

    sent = body_of(connection.handler.seen[0])
    assert sent["from"] == "Trend Engine <no-reply@example.test>"
    assert sent["to"] == ["dana@jarrow.example"]
    assert sent["subject"] == "You have been invited"
    assert sent["text"] == "Follow this link to set your password."


def test_the_api_key_travels_as_a_bearer_token():
    connection = backend(accepted())
    connection.send_messages([message()])

    assert connection.handler.seen[0].headers["Authorization"] == "Bearer re_test"


def test_cc_bcc_and_reply_to_are_carried():
    connection = backend(accepted())
    connection.send_messages(
        [
            message(
                cc=["mark@pureplay.example"],
                bcc=["archive@pureplay.example"],
                reply_to=["support@pureplay.example"],
            )
        ]
    )

    sent = body_of(connection.handler.seen[0])
    assert sent["cc"] == ["mark@pureplay.example"]
    assert sent["bcc"] == ["archive@pureplay.example"]
    assert sent["reply_to"] == ["support@pureplay.example"]


def test_a_multipart_message_sends_both_halves():
    """A client whose reader blocks HTML still needs the link."""
    email = EmailMultiAlternatives(
        subject="Invitation",
        body="Plain text link",
        from_email="no-reply@example.test",
        to=["dana@jarrow.example"],
    )
    email.attach_alternative("<a href='#'>HTML link</a>", "text/html")
    connection = backend(accepted())

    connection.send_messages([email])

    sent = body_of(connection.handler.seen[0])
    assert sent["text"] == "Plain text link"
    assert sent["html"] == "<a href='#'>HTML link</a>"


def test_an_html_only_message_is_sent_as_html():
    email = message()
    email.content_subtype = "html"
    email.body = "<p>Hello</p>"
    connection = backend(accepted())

    connection.send_messages([email])

    sent = body_of(connection.handler.seen[0])
    assert sent["html"] == "<p>Hello</p>"
    assert "text" not in sent


def test_an_attachment_is_base64_encoded():
    email = message()
    email.attach("evidence.csv", "claim,quote\n1,hello", "text/csv")
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert attachment["filename"] == "evidence.csv"
    assert base64.b64decode(attachment["content"]) == b"claim,quote\n1,hello"


def test_headers_resend_sets_itself_are_not_forwarded():
    """Passing them through is rejected by the API."""
    email = message(headers={"From": "spoofed@example.test", "X-Campaign": "invites"})
    connection = backend(accepted())

    connection.send_messages([email])

    sent = body_of(connection.handler.seen[0])
    assert sent["headers"] == {"X-Campaign": "invites"}
    assert sent["from"] == "Trend Engine <no-reply@example.test>"


# ── Failure ─────────────────────────────────────────────────────────────────


def test_without_fail_silently_a_refusal_raises():
    """Django's contract: the caller asked to be told."""
    connection = backend(httpx.Response(422, json={"message": "Invalid `to` field"}))

    with pytest.raises(RuntimeError, match="Resend refused"):
        connection.send_messages([message()])


def test_an_unverified_sending_domain_is_named_as_such(caplog):
    """The failure most likely to appear first on a new deployment, and a bare
    403 sends someone looking at the API key instead of at DNS."""
    connection = backend(
        httpx.Response(403, json={"message": "The example.test domain is not verified"}),
        fail_silently=True,
    )

    with caplog.at_level("ERROR"):
        connection.send_messages([message()])

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "verified in the Resend dashboard" in logged
    assert "DKIM" in logged and "SPF" in logged


def test_a_rejected_key_is_reported_as_a_key_problem(caplog):
    connection = backend(
        httpx.Response(401, json={"message": "API key is invalid"}), fail_silently=True
    )

    with caplog.at_level("ERROR"):
        connection.send_messages([message()])

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "rejected the API key" in logged


def test_a_rate_limit_is_a_warning_not_an_error(caplog):
    """It resolves by itself; an ERROR would train people to ignore them."""
    connection = backend(
        httpx.Response(429, json={"message": "Too many requests"}), fail_silently=True
    )

    with caplog.at_level("WARNING"):
        connection.send_messages([message()])

    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_a_missing_key_says_so_rather_than_sending_nothing_quietly():
    connection = backend(accepted(), api_key="")

    with pytest.raises(RuntimeError, match="RESEND_API_KEY is not set"):
        connection.send_messages([message()])


def test_a_missing_key_is_logged_when_failing_silently(caplog):
    connection = backend(accepted(), api_key="", fail_silently=True)

    with caplog.at_level("ERROR"):
        assert connection.send_messages([message()]) == 0

    assert any("RESEND_API_KEY" in r.getMessage() for r in caplog.records)


def test_the_secret_never_reaches_a_log(caplog):
    connection = backend(
        httpx.Response(500, text="upstream exploded"), fail_silently=True
    )

    with caplog.at_level("DEBUG"):
        connection.send_messages([message()])

    assert "re_test" not in caplog.text


def test_an_attachment_carries_its_declared_type():
    """The mimetype was being dropped, leaving Resend to infer it from the
    filename. We already hold the answer; guessing from a string was never the
    better option."""
    email = message()
    email.attach(
        "brief.docx",
        b"PK\x03\x04binary",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert attachment["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_a_charset_parameter_is_stripped_from_the_type():
    """Our text renderers declare `text/csv; charset=utf-8`; Resend takes the
    media type alone."""
    email = message()
    email.attach("evidence.csv", "a,b\n1,2", "text/csv; charset=utf-8")
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert attachment["content_type"] == "text/csv"


def test_django_infers_a_type_when_the_caller_omits_one():
    """`attach()` guesses from the extension, so the type reaches Resend even
    when nobody passed one. Worth pinning: it means the common case is covered
    without every caller remembering."""
    email = message()
    email.attach("notes.txt", "hello")
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert attachment["content_type"] == "text/plain"


def test_a_two_tuple_attachment_omits_the_type_rather_than_inventing_one():
    """Nothing in this system builds one, but the backend reads index 2
    positionally — so a shorter tuple must not raise."""
    email = message()
    email.attachments.append(("mystery", b"bytes"))
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert "content_type" not in attachment


def test_binary_attachment_bytes_survive_intact():
    """A DOCX is a zip and a PDF is binary. Round-tripping through base64 is
    the only part of the path we control."""
    import base64 as b64

    raw = bytes(range(256))
    email = message()
    email.attach("thing.pdf", raw, "application/pdf")
    connection = backend(accepted())

    connection.send_messages([email])

    (attachment,) = body_of(connection.handler.seen[0])["attachments"]
    assert b64.b64decode(attachment["content"]) == raw
