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

from apps.enrichment.domain import DOMAIN_PACK_VERSION
from apps.operations.models import AuditEvent
from apps.operations.permissions import require_platform_admin
from apps.outputs import exports, services
from apps.outputs.exports.adapters import document_for_version
from apps.outputs.models import (
    ExpertReviewer,
    ExportArtifact,
    Output,
    OutputType,
    OutputVersion,
)
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

logger = logging.getLogger(__name__)


def _actor(request) -> str:
    return getattr(request.user, "email", str(request.user))


#: The output types something can actually draft today. The other types are
#: real states in the model and appear in the form, disabled and marked, rather
#: than producing a trend brief under another type's name — which is what the
#: form used to do, and why the type list said nothing about what was built.
GENERATED_TYPES = frozenset(
    {OutputType.TREND_BRIEF, OutputType.CONTENT_BRIEF, OutputType.PRODUCT_MEMO}
)


class OutputListView(View):
    """Every output, and the form that drafts one."""

    def get(self, request):
        require_platform_admin(request, action="Viewing outputs")

        rows = []
        with operator_scope():
            organizations = list(Organization.objects.all())
            for output in Output.objects.select_related("organization").all():
                rows.append({
                    "obj": output,
                    "versions": output.versions.count(),
                    "notes": output.record_notes.count(),
                })

        return render(
            request,
            "ops/outputs/outputs.html",
            {
                "outputs": rows,
                "organizations": organizations,
                "types": [
                    (value, label, value in GENERATED_TYPES)
                    for value, label in OutputType.choices
                ],
            },
        )

    def post(self, request):
        """Draft a trend brief or a content brief from what has been approved.

        Also the target of the "Draft the brief" button on Triage and the
        "Draft a content brief" button on Signal Review, which post the client
        and type and leave the title to its default.
        """
        require_platform_admin(request, action="Drafting an output")
        back = redirect(reverse("ops-outputs"))

        slug = request.POST.get("organization") or ""
        type_ = request.POST.get("type") or OutputType.TREND_BRIEF
        if type_ not in GENERATED_TYPES:
            label = dict(OutputType.choices).get(type_, type_)
            messages.error(
                request,
                f"{label} cannot be drafted yet — only the trend brief, the content "
                f"brief and the opportunity brief have generators.",
            )
            return back

        with operator_scope():
            organization = Organization.objects.filter(slug=slug).first()
        if organization is None:
            messages.error(request, "Choose a client.")
            return back

        from apps.scoring.models import Recommendation

        from .generate import generate

        title = (request.POST.get("title") or "").strip()
        summary = (request.POST.get("summary") or "").strip()
        recommendation = None
        with scoped(organization):
            try:
                if type_ == OutputType.CONTENT_BRIEF:
                    recommendation = Recommendation.objects.filter(
                        organization=organization, pk=request.POST.get("recommendation") or 0
                    ).select_related("signal", "profile").first()
                built = generate(organization, type_, recommendation=recommendation)
                sections, profile = built.sections, built.profile
                title = title or built.title
                summary = summary or built.summary

                version = services.draft(
                    organization,
                    type=type_,
                    title=title,
                    sections=sections,
                    summary=summary,
                    actor_label=_actor(request),
                    # PRD §8: the profile version the approved recommendations
                    # were made against.
                    client_profile_version=profile,
                    domain_pack_version=DOMAIN_PACK_VERSION,
                    is_illustrative=bool(request.POST.get("is_illustrative")),
                    recommendation=recommendation,
                )
            except services.DraftingError as exc:
                # Every refusal is already written for the operator: nothing
                # approved yet, no recommendation chosen, or the model failed.
                messages.error(request, str(exc))
                return back

        messages.success(
            request,
            f"Drafted “{title}” v{version.number}. Read it before approving — the "
            f"developments in it were approved, this document has not been.",
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
            record_notes = list(output.record_notes.all())
            deliveries = list(live.deliveries.order_by("-sent_at")[:5]) if live else []
            downloads = list(
                ExportArtifact.objects.filter(version__output=output)
                .select_related("version").order_by("-pk")[:12]
            )
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
                "reviewers": list(ExpertReviewer.objects.filter(is_active=True)),
                "record_notes": record_notes,
                "deliveries": deliveries,
                "recipients": recipients,
                "formats": [(f, exports.LABELS.get(f, f)) for f in exports.available()],
                "downloads": downloads,
                "can_regenerate": output.type in GENERATED_TYPES and (
                    output.type != OutputType.CONTENT_BRIEF or output.recommendation_id
                ),
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
                    actor_label=_actor(request),
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


class OutputRegenerateView(View):
    """A new version built from current approved evidence (Mark, 24 Oct).

    Earlier versions are kept exactly as they were. The new one is a draft, so
    it needs approving (and, for health types, an expert sign-off) on its own.
    """

    def post(self, request, pk: int):
        require_platform_admin(request, action="Regenerating an output")
        output, organization = _output(pk)
        back = redirect(reverse("ops-output", args=[pk]))
        from .generate import generate

        with scoped(organization):
            try:
                built = generate(organization, output.type, recommendation=output.recommendation)
                version = services.draft(
                    organization,
                    type=output.type,
                    title=output.title,
                    sections=built.sections,
                    summary=built.summary,
                    actor_label=_actor(request),
                    client_profile_version=built.profile,
                    domain_pack_version=DOMAIN_PACK_VERSION,
                    recommendation=output.recommendation,
                )
            except services.DraftingError as exc:
                messages.error(request, str(exc))
                return back
        messages.success(
            request,
            f"v{version.number} drafted from current evidence. Earlier versions are unchanged; "
            f"this one needs its own review.",
        )
        return back


class OutputEditView(View):
    """Edit a version's sections; saving creates the next version.

    Never edits in place: an approved or published version is a record of what
    was approved or sent, and stays one.
    """

    def get(self, request, pk: int, number: int):
        require_platform_admin(request, action="Editing an output")
        output, organization = _output(pk)
        with scoped(organization):
            version = get_object_or_404(OutputVersion, output=output, number=number)
        return render(request, "ops/outputs/edit.html", {
            "output": output, "organization": organization, "version": version,
            "sections": list(enumerate(version.body)),
        })

    def post(self, request, pk: int, number: int):
        require_platform_admin(request, action="Editing an output")
        output, organization = _output(pk)
        with scoped(organization):
            version = get_object_or_404(OutputVersion, output=output, number=number)
            sections = []
            for i in range(len(version.body)):
                if request.POST.get(f"remove_{i}"):
                    continue
                heading = (request.POST.get(f"heading_{i}") or "").strip()
                text = (request.POST.get(f"text_{i}") or "").replace("\r\n", "\n").strip()
                if heading or text:
                    sections.append({"heading": heading, "text": text})
            if sections == version.body:
                messages.info(request, "No changes, so no new version.")
                return redirect(reverse("ops-output", args=[pk]))
            try:
                new = services.draft(
                    organization,
                    type=output.type,
                    title=output.title,
                    sections=sections,
                    summary=version.summary,
                    actor_label=_actor(request),
                    client_profile_version=output.client_profile_version,
                    domain_pack_version=output.domain_pack_version,
                    recommendation=output.recommendation,
                )
            except services.DraftingError as exc:
                messages.error(request, str(exc))
                return redirect(reverse("ops-output-edit", args=[pk, number]))
            AuditEvent.objects.create(
                kind=AuditEvent.Kind.CONFIG,
                actor_realm=AuditEvent.Realm.OPERATOR,
                actor_label=_actor(request),
                organization=organization,
                message=f"Edited {output.title} v{number} into v{new.number}",
                context={"output_id": output.pk, "from": number, "to": new.number},
            )
            from apps.outputs.content_brief import voice_flags

            flags = voice_flags(sections) if output.type == OutputType.CONTENT_BRIEF else []
        messages.success(request, f"Saved as v{new.number}. v{number} is unchanged.")
        for flag in flags[:5]:
            messages.warning(request, f"Check before review — {flag}")
        return redirect(reverse("ops-output", args=[pk]))
