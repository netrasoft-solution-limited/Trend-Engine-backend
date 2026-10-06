"""Client profile — server-rendered operator screens.

These replace `manage.py client_profile`. Onboarding a client is the one thing
the system cannot infer, and it was only reachable from a terminal, which meant
only I could do it. The command stays (it is scriptable and it is what runs in a
recovery), but the browser is now the normal path.

TENANCY COMES FROM THE URL, NOT THE SESSION. `OperatorTenantMiddleware` can
narrow scope from a session key, and every screen here deliberately ignores it:
the client being edited is in the address bar, so a bookmarked page always means
what it said, and no hidden state can have you editing one client's profile
while reading another's name. Each view resolves the `Organization` — a plain L0
model, not tenant-scoped — then does its work inside `scoped(org)`.

The pattern throughout is the one `apps/sources/views.py` established: plain
`View` subclasses, the role check as the first statement, no Django forms, reads
straight from `request.POST`, and POST → mutate → audit → message → redirect.
Row edits dispatch on a hidden `action` field, which is the same trick the
provider screen uses for its "clear" button, widened to a table.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views import View

from apps.operations.permissions import require_platform_admin
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

from . import services
from .models import ClientAsset, ClientContact, ClientProfileVersion, ClientTerm

logger = logging.getLogger(__name__)

FACET_FIELDS = {
    "audience": ClientTerm.Facet.AUDIENCE,
    "priority": ClientTerm.Facet.PRIORITY,
    "competitor": ClientTerm.Facet.COMPETITOR,
}


def _terms(raw: str) -> list[str]:
    """One comma-separated input is the whole widget.

    It needs no JavaScript, it is what `client_profile load` already accepts,
    and it is what an operator can paste from a spreadsheet. Blank entries are
    dropped rather than stored, because a blank term matches nothing and would
    sit in the list looking like coverage.
    """
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _weight(raw: str, *, default: int = 50) -> int:
    """Clamped rather than validated. A weight outside 0–100 is a typo, and
    refusing the whole row for it would lose the rest of the operator's edit."""
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return default


def _organization(slug: str) -> Organization:
    with operator_scope():
        return get_object_or_404(Organization, slug=slug)


# ── The client list, which also creates one ─────────────────────────────────


class ClientListView(View):
    """Every client, and the form that adds one.

    The add form lives on the list rather than on its own screen: creating a
    client is two fields, and a separate page for two fields is a page to
    maintain and a click to make.
    """

    def get(self, request):
        require_platform_admin(request, action="Viewing clients")

        rows = []
        with operator_scope():
            organizations = list(Organization.objects.all())
        for organization in organizations:
            with scoped(organization):
                current = services.current_for(organization)
                rows.append(
                    {
                        "obj": organization,
                        "profile": current,
                        "drafts": ClientProfileVersion.objects.filter(
                            is_current=False
                        ).count(),
                        "contacts": ClientContact.objects.filter(
                            is_active=True, receives_outputs=True
                        ).count(),
                    }
                )
        return render(request, "ops/clients/clients.html", {"clients": rows})

    def post(self, request):
        require_platform_admin(request, action="Adding a client")

        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "A client needs a name.")
            return redirect(reverse("ops-clients"))

        slug = slugify((request.POST.get("slug") or "").strip() or name)
        try:
            # The insert gets its own savepoint. Catching IntegrityError without
            # one leaves the surrounding transaction unusable — every later
            # query raises TransactionManagementError, so the friendly message
            # below would never render. Checking first instead of catching would
            # still race; this both reads clearly and holds under concurrency.
            with operator_scope(), transaction.atomic():
                Organization.objects.create(
                    name=name, slug=slug, status=Organization.Status.ONBOARDING
                )
        except IntegrityError:
            messages.error(
                request,
                f"A client already uses the address “{slug}”. Give this one a "
                f"different short name.",
            )
            return redirect(reverse("ops-clients"))

        messages.success(
            request,
            f"{name} added. Give it a storefront to read, or write the profile by hand.",
        )
        return redirect(reverse("ops-client", args=[slug]))


# ── One client ──────────────────────────────────────────────────────────────


class ClientHubView(View):
    """Everything about one client on one page, and every action from it.

    Except scoring. `apps.scoring` sits ABOVE `apps.clients` in the layer
    contract, so this module may not import it — which is the contract saying
    something true: a profile describes a client, while a score is a judgement
    about evidence made using that profile. The relevance screen lives in
    `apps/scoring/views.py` and this page links to it.
    """

    def get(self, request, slug):
        require_platform_admin(request, action="Viewing a client")
        organization = _organization(slug)

        with scoped(organization):
            current = services.current_for(organization)
            versions = list(ClientProfileVersion.objects.order_by("-number")[:10])
            contacts = list(ClientContact.objects.all())

        return render(
            request,
            "ops/clients/client.html",
            {
                "organization": organization,
                "profile": current,
                "versions": versions,
                "contacts": contacts,
            },
        )


class ClientDiscoverView(View):
    """Read a storefront and draft a profile from it."""

    def post(self, request, slug):
        require_platform_admin(request, action="Reading a storefront")
        organization = _organization(slug)

        store_url = (request.POST.get("store_url") or "").strip()
        if not store_url:
            messages.error(request, "Enter the client's storefront address.")
            return redirect(reverse("ops-client", args=[slug]))

        from .tasks import discover_storefront

        discover_storefront.delay(
            organization_id=organization.pk,
            store_url=store_url,
            actor_label=getattr(request.user, "email", str(request.user)),
        )
        messages.info(
            request,
            f"Reading {store_url}. This takes a minute or so — refresh this page "
            f"and the draft will appear below. If the storefront cannot be read, "
            f"the reason will appear here instead.",
        )
        return redirect(reverse("ops-client", args=[slug]))


# ── Profile versions ────────────────────────────────────────────────────────


class ClientProfileDraftView(View):
    def post(self, request, slug):
        require_platform_admin(request, action="Drafting a profile")
        organization = _organization(slug)

        with scoped(organization):
            version = services.draft(
                organization,
                label=(request.POST.get("label") or "").strip(),
                actor_label=getattr(request.user, "email", str(request.user)),
                copy_current=True,
            )
        messages.success(request, f"Drafted v{version.number} from the live profile.")
        return redirect(reverse("ops-client-profile", args=[slug, version.number]))


class ClientProfileActivateView(View):
    def post(self, request, slug, number):
        require_platform_admin(request, action="Activating a profile")
        organization = _organization(slug)

        with scoped(organization):
            version = get_object_or_404(ClientProfileVersion, number=number)
            try:
                services.activate(
                    version,
                    actor_label=getattr(request.user, "email", str(request.user)),
                )
            except services.ProfileError as exc:
                # The service's message is already written for a human. Repeating
                # it verbatim keeps one explanation rather than two that drift.
                messages.error(request, str(exc))
                return redirect(reverse("ops-client-profile", args=[slug, number]))

        messages.success(
            request,
            f"v{number} is live. Re-score to apply it — existing scores were "
            f"computed against the previous profile and still belong to it.",
        )
        return redirect(reverse("ops-client", args=[slug]))


class ClientProfileView(View):
    """Review and edit one version: assets, audiences, priorities, competitors."""

    def get(self, request, slug, number):
        require_platform_admin(request, action="Viewing a profile")
        organization = _organization(slug)

        with scoped(organization):
            version = get_object_or_404(ClientProfileVersion, number=number)
            assets = list(version.assets.all())
            terms = list(version.terms.all())

        return render(
            request,
            "ops/clients/profile.html",
            {
                "organization": organization,
                "version": version,
                "assets": assets,
                "kinds": ClientAsset.Kind.choices,
                "facets": [
                    ("audience", "Audiences", [t for t in terms if t.facet == "audience"]),
                    ("priority", "Priorities", [t for t in terms if t.facet == "priority"]),
                    ("competitor", "Competitors", [t for t in terms if t.facet == "competitor"]),
                ],
            },
        )

    def post(self, request, slug, number):
        require_platform_admin(request, action="Editing a profile")
        organization = _organization(slug)
        action = request.POST.get("action") or ""

        with scoped(organization):
            version = get_object_or_404(ClientProfileVersion, number=number)

            # A live profile is what published outputs cite (PRD §8). Editing it
            # in place would change what an already-delivered brief claims to
            # have been built from.
            if version.is_current:
                messages.error(
                    request,
                    f"v{version.number} is live and outputs already cite it. "
                    f"Draft a new version to make changes — the live one has to "
                    f"keep meaning what it meant when those outputs were built.",
                )
                return redirect(reverse("ops-client-profile", args=[slug, number]))

            handler = {
                "asset.add": self._asset_add,
                "asset.save": self._asset_save,
                "asset.delete": self._asset_delete,
                "term.add": self._term_add,
                "term.save": self._term_save,
                "term.delete": self._term_delete,
            }.get(action)

            if handler is None:
                messages.error(request, "Unknown action.")
            else:
                handler(request, organization, version)

        return redirect(reverse("ops-client-profile", args=[slug, number]))

    # Each handler is small and does one thing, so the dispatch table above
    # reads as the list of what this screen can do.

    def _asset_add(self, request, organization, version) -> None:
        name = (request.POST.get("name") or "").strip()
        terms = _terms(request.POST.get("terms"))
        if not name or not terms:
            messages.error(request, "An asset needs a name and at least one term.")
            return
        ClientAsset.objects.create(
            organization=organization,
            profile=version,
            name=name,
            kind=request.POST.get("kind") or ClientAsset.Kind.PRODUCT,
            terms=terms,
            weight=_weight(request.POST.get("weight")),
        )
        messages.success(request, f"Added {name}.")

    def _asset_save(self, request, organization, version) -> None:
        asset = version.assets.filter(pk=request.POST.get("pk")).first()
        if asset is None:
            messages.error(request, "That asset is no longer here.")
            return
        terms = _terms(request.POST.get("terms"))
        if not terms:
            messages.error(
                request,
                f"{asset.name} would have no terms left, so it could never match. "
                f"Delete it instead if that is what you meant.",
            )
            return
        asset.name = (request.POST.get("name") or asset.name).strip()
        asset.kind = request.POST.get("kind") or asset.kind
        asset.terms = terms
        asset.weight = _weight(request.POST.get("weight"), default=asset.weight)
        asset.save(update_fields=["name", "kind", "terms", "weight"])
        messages.success(request, f"Saved {asset.name}.")

    def _asset_delete(self, request, organization, version) -> None:
        deleted, _ = version.assets.filter(pk=request.POST.get("pk")).delete()
        messages.info(request, "Removed." if deleted else "That asset is no longer here.")

    def _term_add(self, request, organization, version) -> None:
        facet = FACET_FIELDS.get(request.POST.get("facet") or "")
        name = (request.POST.get("name") or "").strip()
        terms = _terms(request.POST.get("terms"))
        if facet is None or not name or not terms:
            messages.error(request, "That needs a name and at least one term.")
            return
        ClientTerm.objects.create(
            organization=organization,
            profile=version,
            facet=facet,
            name=name,
            terms=terms,
            weight=_weight(request.POST.get("weight")),
        )
        messages.success(request, f"Added {name}.")

    def _term_save(self, request, organization, version) -> None:
        term = version.terms.filter(pk=request.POST.get("pk")).first()
        if term is None:
            messages.error(request, "That entry is no longer here.")
            return
        terms = _terms(request.POST.get("terms"))
        if not terms:
            messages.error(request, f"{term.name} would have no terms left.")
            return
        term.name = (request.POST.get("name") or term.name).strip()
        term.terms = terms
        term.weight = _weight(request.POST.get("weight"), default=term.weight)
        term.save(update_fields=["name", "terms", "weight"])
        messages.success(request, f"Saved {term.name}.")

    def _term_delete(self, request, organization, version) -> None:
        deleted, _ = version.terms.filter(pk=request.POST.get("pk")).delete()
        messages.info(request, "Removed." if deleted else "That entry is no longer here.")


# ── Contacts ────────────────────────────────────────────────────────────────


class ClientContactsView(View):
    """Who receives this client's work.

    A separate screen from the profile on purpose. Contacts are current-state
    operational data and the profile is a historical record outputs cite — so
    adding an email address must not mint a profile version.
    """

    def get(self, request, slug):
        require_platform_admin(request, action="Viewing contacts")
        organization = _organization(slug)
        with scoped(organization):
            contacts = list(ClientContact.objects.all())
        return render(
            request,
            "ops/clients/contacts.html",
            {"organization": organization, "contacts": contacts},
        )

    def post(self, request, slug):
        require_platform_admin(request, action="Editing contacts")
        organization = _organization(slug)
        action = request.POST.get("action") or ""

        with scoped(organization):
            if action == "contact.add":
                self._add(request, organization)
            elif action == "contact.toggle":
                self._toggle(request, organization)
            elif action == "contact.deactivate":
                self._deactivate(request, organization)
            else:
                messages.error(request, "Unknown action.")

        return redirect(reverse("ops-client-contacts", args=[slug]))

    def _add(self, request, organization) -> None:
        name = (request.POST.get("name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        if not name or not email:
            messages.error(
                request,
                "A contact needs a name and an address. “We emailed three "
                "addresses” is not a record of who received the work.",
            )
            return
        try:
            # Savepointed, for the reason given on ClientListView.post.
            with transaction.atomic():
                ClientContact.objects.create(
                    organization=organization,
                    name=name,
                    email=email,
                    role=(request.POST.get("role") or "").strip(),
                    added_by_label=getattr(request.user, "email", str(request.user)),
                )
        except IntegrityError:
            messages.error(request, f"{email} is already on this client's list.")
            return
        messages.success(request, f"{name} will receive this client's outputs.")

    def _toggle(self, request, organization) -> None:
        contact = ClientContact.objects.filter(pk=request.POST.get("pk")).first()
        if contact is None:
            messages.error(request, "That contact is no longer here.")
            return
        contact.receives_outputs = not contact.receives_outputs
        contact.save(update_fields=["receives_outputs"])
        messages.success(
            request,
            f"{contact.name} will "
            f"{'receive' if contact.receives_outputs else 'no longer receive'} outputs.",
        )

    def _deactivate(self, request, organization) -> None:
        contact = ClientContact.objects.filter(pk=request.POST.get("pk")).first()
        if contact is None:
            messages.error(request, "That contact is no longer here.")
            return
        # Deactivated, never deleted: a delivery record naming an address that is
        # no longer in the table is a delivery nobody can explain.
        contact.is_active = False
        contact.save(update_fields=["is_active"])
        messages.info(request, f"{contact.name} removed from the list.")
