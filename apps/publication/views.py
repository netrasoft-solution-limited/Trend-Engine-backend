"""Output builder — server-rendered operator screens.

LIVES IN `apps.publication`, NOT `apps.outputs`, and that is the layer contract
being right rather than inconvenient. This screen drives the whole lifecycle,
and the second half of it — publish, withdraw, deliver — is the gate, which
sits ABOVE outputs. A screen in `apps/outputs/views.py` importing
`apps.publication.services` is upward, and `lint-imports` refused it.

The same reasoning already put `draft_from_evidence` here: the thing that
touches the gate belongs on the gate's side of the line.

Everything an output goes through, from drafting it out of the evidence to
sending it to a client. Until now each step was a management command or a
Python shell, which meant only I could do any of it.

The services underneath are unchanged and are not re-implemented here. This
screen calls `outputs.services`, `publication.services` and
`publication.delivery` exactly as the commands do, and when one of them
refuses, its message is shown verbatim — those messages are already written for
a person, and paraphrasing them into "an error occurred" would throw away the
only part that tells the operator what to do.

TWO DELIBERATE SHAPES WORTH KNOWING.

Export is a GET that writes an `AuditEvent`. A download is a read, `<a href>`
is how you offer one without JavaScript, and the audit log is append-only
recording a disclosure that genuinely happened. Do not "fix" it into a POST;
that breaks every link and records no more than it does now.

Withdrawing requires a typed reason, and that requirement IS the confirmation
step. There is no JavaScript here to put a dialog in front of a destructive
action, and having to say why is a better check than having to click twice.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.operations.models import AuditEvent
from apps.operations.permissions import require_platform_admin
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

from apps.outputs import exports, services
from apps.outputs.exports.adapters import document_for_version
from apps.outputs.models import (
    ExportArtifact,
    Output,
    OutputState,
    OutputType,
    OutputVersion,
)

logger = logging.getLogger(__name__)


def _actor(request) -> str:
    return getattr(request.user, "email", str(request.user))


class OutputListView(View):
    """Every output, and the form that drafts one."""

    def get(self, request):
        require_platform_admin(request, action="Viewing outputs")

        rows = []
        with operator_scope():
            organizations = list(Organization.objects.all())
            for output in Output.objects.select_related("organization").all():
                rows.append({"obj": output, "versions": output.versions.count()})

        return render(
            request,
            "ops/outputs/outputs.html",
            {
                "outputs": rows,
                "organizations": organizations,
                "types": OutputType.choices,
            },
        )

    def post(self, request):
        """Draft one from the client's own evidence."""
        require_platform_admin(request, action="Drafting an output")

        title = (request.POST.get("title") or "").strip()
        slug = request.POST.get("organization") or ""
        type_ = request.POST.get("type") or OutputType.TREND_BRIEF

        if not title:
            messages.error(request, "Give the output a title.")
            return redirect(reverse("ops-outputs"))

        with operator_scope():
            organization = Organization.objects.filter(slug=slug).first()
        if organization is None:
            messages.error(request, "Choose a client.")
            return redirect(reverse("ops-outputs"))

        from apps.outputs.drafting import sections_for

        with scoped(organization):
            try:
                sections = sections_for(organization)
                version = services.draft(
                    organization,
                    type=type_,
                    title=title,
                    sections=sections,
                    summary=(request.POST.get("summary") or "").strip(),
                    actor_label=_actor(request),
                )
            except services.DraftingError as exc:
                # Both of its refusals are already operator-facing prose: no
                # active profile, or nothing in the corpus matched one.
                messages.error(request, str(exc))
                return redirect(reverse("ops-outputs"))

        messages.success(
            request,
            f"Drafted “{title}” v{version.number} from {len(sections)} section(s). "
            f"Read it before approving — nothing here has been reviewed yet.",
        )
        return redirect(reverse("ops-output", args=[version.output_id]))


class OutputDetailView(View):
    """One output: its versions, their state, and every action."""

    def get(self, request, pk: int):
        require_platform_admin(request, action="Viewing an output")

        with operator_scope():
            output = get_object_or_404(Output.objects.select_related("organization"), pk=pk)
            organization = output.organization

        with scoped(organization):
            versions = list(output.versions.order_by("-number"))
            publications = list(output.publications.order_by("-published_at")[:5])
            live = next((p for p in publications if p.is_live), None)
            reviews = list(output.expert_reviews.all())
            deliveries = list(live.deliveries.all()[:5]) if live else []
            from apps.clients.contacts import recipients_for

            recipients = recipients_for(organization)

        return render(
            request,
            "ops/outputs/output.html",
            {
                "output": output,
                "organization": organization,
                "versions": versions,
                "latest": versions[0] if versions else None,
                "live": live,
                "publications": publications,
                "reviews": reviews,
                "signed_off": any(r.is_signed_off for r in reviews),
                "deliveries": deliveries,
                "recipients": recipients,
                "formats": [(f, exports.LABELS.get(f, f)) for f in exports.available()],
            },
        )


class OutputApproveView(View):
    def post(self, request, pk: int, number: int):
        require_platform_admin(request, action="Approving an output")
        output, organization = _output(pk)

        with scoped(organization):
            version = get_object_or_404(OutputVersion, output=output, number=number)
            try:
                services.approve(version, actor_label=_actor(request))
            except services.DraftingError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-output", args=[pk]))

        messages.success(
            request,
            f"v{number} approved. That is an internal record — it is not visible "
            f"to the client until you publish it.",
        )
        return redirect(reverse("ops-output", args=[pk]))


class OutputSignoffView(View):
    def post(self, request, pk: int):
        require_platform_admin(request, action="Recording a sign-off")
        output, organization = _output(pk)

        with scoped(organization):
            try:
                services.record_expert_signoff(
                    output,
                    reviewer_label=(request.POST.get("reviewer") or "").strip(),
                    discipline=(request.POST.get("discipline") or "").strip(),
                    note=(request.POST.get("note") or "").strip(),
                )
            except services.DraftingError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-output", args=[pk]))

        messages.success(request, "Expert sign-off recorded.")
        return redirect(reverse("ops-output", args=[pk]))


class OutputPublishView(View):
    def post(self, request, pk: int, number: int):
        require_platform_admin(request, action="Publishing an output")
        output, organization = _output(pk)

        from . import services as gate

        with scoped(organization):
            version = get_object_or_404(OutputVersion, output=output, number=number)
            try:
                gate.publish(
                    version=version, organization=organization, actor_label=_actor(request)
                )
            except (gate.NotApproved, gate.ExpertReviewMissing) as exc:
                # The gate's refusals name exactly what is missing and why.
                messages.error(request, str(exc))
                return redirect(reverse("ops-output", args=[pk]))

        messages.success(
            request,
            f"v{number} published. Nothing has been sent yet — use Deliver to "
            f"email it to the client.",
        )
        return redirect(reverse("ops-output", args=[pk]))


class OutputWithdrawView(View):
    def post(self, request, pk: int):
        require_platform_admin(request, action="Withdrawing an output")
        output, organization = _output(pk)

        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            # The required reason IS the confirmation step. There is no
            # JavaScript here to put a dialog in front of this, and saying why
            # is a better check than clicking twice.
            messages.error(
                request,
                "Say why it is being withdrawn. It goes in the audit record, and "
                "if the document has already been emailed that record is the only "
                "account of what happened.",
            )
            return redirect(reverse("ops-output", args=[pk]))

        from . import services as gate

        with scoped(organization):
            live = output.publications.filter(unpublished_at__isnull=True).first()
            if live is None:
                messages.error(request, "Nothing is published for this output.")
                return redirect(reverse("ops-output", args=[pk]))
            gate.unpublish(publication=live, actor_label=_actor(request), reason=reason)

        messages.info(request, "Withdrawn. The client can no longer be sent this version.")
        return redirect(reverse("ops-output", args=[pk]))


class OutputDeliverView(View):
    def post(self, request, pk: int):
        require_platform_admin(request, action="Delivering an output")
        output, organization = _output(pk)

        chosen = request.POST.getlist("formats") or list(exports.available())

        with scoped(organization):
            live = output.publications.filter(unpublished_at__isnull=True).first()
            if live is None:
                messages.error(
                    request,
                    "Publish it first. Delivery goes through the gate because an "
                    "email cannot be recalled.",
                )
                return redirect(reverse("ops-output", args=[pk]))

            from . import delivery as delivery_service

            try:
                record = delivery_service.deliver(
                    publication=live,
                    formats=tuple(chosen),
                    actor_label=_actor(request),
                    note=(request.POST.get("note") or "").strip(),
                )
            except delivery_service.DeliveryError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-output", args=[pk]))

        messages.success(
            request,
            f"Sent to {len(record.recipients)} recipient(s): "
            f"{', '.join(record.recipients)}.",
        )
        return redirect(reverse("ops-output", args=[pk]))


class OutputExportView(View):
    """Download one version in one format.

    A GET that writes an audit row, deliberately — see the module docstring.
    Works on ANY version, not only published ones: an unpublished export is
    watermarked DRAFT and is how an operator reads a draft properly before
    approving it.
    """

    def get(self, request, pk: int, number: int, fmt: str):
        require_platform_admin(request, action="Exporting an output")
        output, organization = _output(pk)

        with scoped(organization):
            version = get_object_or_404(OutputVersion, output=output, number=number)
            document = document_for_version(version)
            try:
                rendered = exports.render(document, fmt)
            except exports.ExportError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-output", args=[pk]))

            ExportArtifact.objects.create(
                organization=organization,
                version=version,
                format=fmt,
                filename=rendered.filename,
                byte_size=rendered.byte_size,
                sha256=rendered.sha256(),
                renderer_version=exports.RENDERER_VERSION,
                was_draft=document.is_draft,
                created_by_label=_actor(request),
            )

        AuditEvent.objects.create(
            kind=AuditEvent.Kind.EXPORT,
            actor_realm=AuditEvent.Realm.OPERATOR,
            actor_label=_actor(request),
            organization=organization,
            message=f"Exported {output.title} v{number} as {fmt}",
            context={
                "output_id": output.pk,
                "version": number,
                "format": fmt,
                "sha256": rendered.sha256(),
                "was_draft": document.is_draft,
            },
        )

        response = HttpResponse(rendered.content, content_type=rendered.mimetype)
        response["Content-Disposition"] = f'attachment; filename="{rendered.filename}"'
        return response


def _output(pk: int):
    with operator_scope():
        output = get_object_or_404(Output.objects.select_related("organization"), pk=pk)
        return output, output.organization
