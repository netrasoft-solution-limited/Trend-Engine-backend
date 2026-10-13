"""The operator screens for client setup.

These exist so that onboarding a client stops being something only I can do from
a terminal. What is worth testing is not that a page returns 200 — it is the
handful of refusals that keep a profile trustworthy:

  · a live profile cannot be edited in place, because outputs cite it
  · a role that should not reach these screens does not
  · the client being edited comes from the URL, so two tabs cannot cross
  · a contact list cannot grow a duplicate address
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.clients import services
from apps.clients.models import ClientAsset, ClientContact, ClientProfileVersion, ClientTerm
from apps.operations.models import OperatorUser
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(db):
    return OperatorUser.objects.create_user(
        email="admin@netrasoft.test",
        password="x" * 24,
        name="Platform Admin",
        role=OperatorUser.Role.PLATFORM_ADMIN,
    )


@pytest.fixture
def plain_operator(db):
    return OperatorUser.objects.create_user(
        email="operator@netrasoft.test", password="x" * 24, name="Operator"
    )


@pytest.fixture
def jarrow(db):
    with operator_scope():
        return Organization.objects.create(name="Jarrow Formulas", slug="jarrow")


@pytest.fixture
def draft_profile(jarrow):
    with scoped(jarrow):
        version = services.draft(jarrow, label="first", copy_current=False)
        ClientAsset.objects.create(
            organization=jarrow, profile=version, name="Magnesium",
            terms=["magnesium"], weight=70,
        )
        return version


@pytest.fixture
def live_profile(draft_profile, jarrow):
    with scoped(jarrow):
        return services.activate(draft_profile, actor_label="test")


# ── Who may reach these screens ─────────────────────────────────────────────


def test_a_plain_operator_cannot_reach_the_client_list(client, plain_operator):
    """PRD §3.2 keeps Operator and Platform Admin distinct even where one person
    holds both."""
    client.force_login(plain_operator)

    assert client.get(reverse("ops-clients")).status_code == 403


def test_a_plain_operator_cannot_create_a_client(client, plain_operator):
    client.force_login(plain_operator)

    response = client.post(reverse("ops-clients"), {"name": "Sneaky"})

    assert response.status_code == 403
    with operator_scope():
        assert not Organization.objects.filter(name="Sneaky").exists()


def test_an_anonymous_visitor_is_sent_to_login(client):
    response = client.get(reverse("ops-clients"))

    assert response.status_code in (302, 403)


# ── Creating a client ───────────────────────────────────────────────────────


def test_a_client_can_be_created_from_the_browser(client, admin):
    client.force_login(admin)

    response = client.post(
        reverse("ops-clients"), {"name": "Jarrow Formulas", "slug": ""}, follow=True
    )

    assert response.status_code == 200
    with operator_scope():
        organization = Organization.objects.get(name="Jarrow Formulas")
    assert organization.slug == "jarrow-formulas", "a slug is built from the name"
    assert organization.status == Organization.Status.ONBOARDING


def test_a_duplicate_address_is_refused_with_a_usable_message(client, admin, jarrow):
    client.force_login(admin)

    response = client.post(
        reverse("ops-clients"), {"name": "Jarrow Again", "slug": "jarrow"}, follow=True
    )

    assert "already uses the address" in response.content.decode()
    with operator_scope():
        assert Organization.objects.filter(slug="jarrow").count() == 1


def test_a_nameless_client_is_refused(client, admin):
    client.force_login(admin)

    client.post(reverse("ops-clients"), {"name": "  "}, follow=True)

    with operator_scope():
        assert Organization.objects.count() == 0


# ── The live profile is read-only, which is the point ───────────────────────


def test_a_live_profile_cannot_be_edited_in_place(client, admin, jarrow, live_profile):
    """Outputs cite the profile version they were built from (PRD §8). Editing
    it would change what an already-delivered brief claims about itself."""
    client.force_login(admin)

    response = client.post(
        reverse("ops-client-profile", args=[jarrow.slug, live_profile.number]),
        {"action": "asset.add", "name": "Creatine", "terms": "creatine", "weight": "80"},
        follow=True,
    )

    assert "Draft a new version" in response.content.decode()
    with scoped(jarrow):
        assert not ClientAsset.objects.filter(name="Creatine").exists()


def test_a_live_profile_cannot_have_an_asset_deleted(client, admin, jarrow, live_profile):
    client.force_login(admin)
    with scoped(jarrow):
        asset = live_profile.assets.first()

    client.post(
        reverse("ops-client-profile", args=[jarrow.slug, live_profile.number]),
        {"action": "asset.delete", "pk": asset.pk},
        follow=True,
    )

    with scoped(jarrow):
        assert live_profile.assets.filter(pk=asset.pk).exists()


def test_a_draft_profile_can_be_edited(client, admin, jarrow, draft_profile):
    client.force_login(admin)

    client.post(
        reverse("ops-client-profile", args=[jarrow.slug, draft_profile.number]),
        {"action": "asset.add", "name": "Creatine", "terms": "creatine, monohydrate",
         "weight": "80", "kind": "product"},
        follow=True,
    )

    with scoped(jarrow):
        asset = ClientAsset.objects.get(name="Creatine")
    assert asset.terms == ["creatine", "monohydrate"], "comma-separated, trimmed"
    assert asset.weight == 80


def test_an_asset_cannot_be_saved_with_no_terms_left(client, admin, jarrow, draft_profile):
    """It could never match again, and would sit in the list looking like coverage."""
    client.force_login(admin)
    with scoped(jarrow):
        asset = draft_profile.assets.first()

    response = client.post(
        reverse("ops-client-profile", args=[jarrow.slug, draft_profile.number]),
        {"action": "asset.save", "pk": asset.pk, "name": asset.name, "terms": "  ,  "},
        follow=True,
    )

    assert "could never match" in response.content.decode()
    with scoped(jarrow):
        assert ClientAsset.objects.get(pk=asset.pk).terms == ["magnesium"]


def test_a_weight_outside_the_range_is_clamped_not_refused(client, admin, jarrow, draft_profile):
    """A typo in one field should not lose the rest of the operator's edit."""
    client.force_login(admin)

    client.post(
        reverse("ops-client-profile", args=[jarrow.slug, draft_profile.number]),
        {"action": "asset.add", "name": "Zinc", "terms": "zinc", "weight": "9000"},
        follow=True,
    )

    with scoped(jarrow):
        assert ClientAsset.objects.get(name="Zinc").weight == 100


def test_terms_of_each_facet_land_on_the_right_one(client, admin, jarrow, draft_profile):
    client.force_login(admin)
    url = reverse("ops-client-profile", args=[jarrow.slug, draft_profile.number])

    for facet, name in (("audience", "Older adults"), ("competitor", "Thorne")):
        client.post(url, {"action": "term.add", "facet": facet, "name": name,
                          "terms": name.lower(), "weight": "50"}, follow=True)

    with scoped(jarrow):
        assert ClientTerm.objects.get(name="Older adults").facet == ClientTerm.Facet.AUDIENCE
        assert ClientTerm.objects.get(name="Thorne").facet == ClientTerm.Facet.COMPETITOR


# ── Activation ──────────────────────────────────────────────────────────────


def test_activating_an_empty_profile_is_refused_in_the_browser(client, admin, jarrow):
    """The service's refusal reaches the operator verbatim rather than as a 500."""
    client.force_login(admin)
    with scoped(jarrow):
        empty = services.draft(jarrow, copy_current=False)

    response = client.post(
        reverse("ops-client-profile-activate", args=[jarrow.slug, empty.number]), follow=True
    )

    assert "match no claims at all" in response.content.decode()
    with scoped(jarrow):
        assert ClientProfileVersion.objects.get(pk=empty.pk).is_current is False


def test_activating_supersedes_the_previous_version(client, admin, jarrow, live_profile):
    client.force_login(admin)
    with scoped(jarrow):
        second = services.draft(jarrow, copy_current=True)

    client.post(
        reverse("ops-client-profile-activate", args=[jarrow.slug, second.number]), follow=True
    )

    with scoped(jarrow):
        assert services.current_for(jarrow).number == second.number
        assert ClientProfileVersion.objects.get(pk=live_profile.pk).is_current is False


# ── Contacts ────────────────────────────────────────────────────────────────


def test_a_contact_can_be_added(client, admin, jarrow):
    client.force_login(admin)

    client.post(
        reverse("ops-client-contacts", args=[jarrow.slug]),
        {"action": "contact.add", "name": "Mark", "email": "Mark@Jarrow.example",
         "role": "Founder"},
        follow=True,
    )

    with scoped(jarrow):
        contact = ClientContact.objects.get(name="Mark")
    assert contact.email == "mark@jarrow.example", "normalised on the way in"
    assert contact.receives_outputs is True


def test_the_same_address_cannot_be_added_twice(client, admin, jarrow):
    """Two rows for one mailbox would send the same document to one person twice."""
    client.force_login(admin)
    url = reverse("ops-client-contacts", args=[jarrow.slug])
    payload = {"action": "contact.add", "name": "Mark", "email": "mark@jarrow.example"}

    client.post(url, payload, follow=True)
    response = client.post(url, {**payload, "email": "MARK@jarrow.example"}, follow=True)

    assert "already on this client" in response.content.decode()
    with scoped(jarrow):
        assert ClientContact.objects.count() == 1


def test_a_removed_contact_is_deactivated_not_deleted(client, admin, jarrow):
    """A delivery record naming an address no longer in the table is a delivery
    nobody can explain."""
    client.force_login(admin)
    with scoped(jarrow):
        contact = ClientContact.objects.create(
            organization=jarrow, name="Departed", email="old@jarrow.example"
        )

    client.post(
        reverse("ops-client-contacts", args=[jarrow.slug]),
        {"action": "contact.deactivate", "pk": contact.pk},
        follow=True,
    )

    with scoped(jarrow):
        contact.refresh_from_db()
    assert contact.is_active is False


def test_receiving_can_be_turned_off_without_removing_the_person(client, admin, jarrow):
    client.force_login(admin)
    with scoped(jarrow):
        contact = ClientContact.objects.create(
            organization=jarrow, name="Accounts", email="ap@jarrow.example"
        )

    client.post(
        reverse("ops-client-contacts", args=[jarrow.slug]),
        {"action": "contact.toggle", "pk": contact.pk},
        follow=True,
    )

    with scoped(jarrow):
        contact.refresh_from_db()
    assert contact.receives_outputs is False
    assert contact.is_active is True


# ── The tenant comes from the URL ───────────────────────────────────────────


def test_one_clients_screen_never_shows_anothers_contacts(client, admin, jarrow):
    """No session scope selector: the address bar decides, so a bookmarked page
    always means what it said."""
    client.force_login(admin)
    with operator_scope():
        other = Organization.objects.create(name="Second", slug="second")
    with scoped(other):
        ClientContact.objects.create(
            organization=other, name="Someone Else", email="x@second.example"
        )
    with scoped(jarrow):
        ClientContact.objects.create(
            organization=jarrow, name="Mark", email="mark@jarrow.example"
        )

    body = client.get(reverse("ops-client-contacts", args=["jarrow"])).content.decode()

    assert "Mark" in body
    assert "Someone Else" not in body


def test_a_profile_number_from_another_client_is_not_found(client, admin, jarrow, draft_profile):
    """The version number is per-organisation, so v1 exists for everyone. The
    scope, not the number, is what keeps them apart."""
    client.force_login(admin)
    with operator_scope():
        other = Organization.objects.create(name="Second", slug="second")

    response = client.get(
        reverse("ops-client-profile", args=[other.slug, draft_profile.number])
    )

    assert response.status_code == 404


def test_an_unknown_client_is_a_404(client, admin):
    client.force_login(admin)

    assert client.get(reverse("ops-client", args=["nobody"])).status_code == 404


# ── Starting a profile, and importing one ───────────────────────────────────
#
# Mark's 8 October review: Jarrow had no profile, the page promised one could be
# written by hand, and there was no button. The draft service always handled a
# first version; only the page hid the way in.


def test_a_client_with_no_profile_is_offered_a_way_to_start_one(client, admin, jarrow):
    client.force_login(admin)

    page = client.get(reverse("ops-client", args=[jarrow.slug])).content.decode()

    assert "Start a profile by hand" in page
    assert reverse("ops-client-profile-draft", args=[jarrow.slug]) in page


def test_starting_a_first_profile_opens_an_empty_draft(client, admin, jarrow):
    client.force_login(admin)

    response = client.post(
        reverse("ops-client-profile-draft", args=[jarrow.slug]), follow=True
    )

    assert "Started v1" in response.content.decode()
    with scoped(jarrow):
        version = ClientProfileVersion.objects.get()
    assert version.number == 1 and not version.is_current


def _upload(name: str, content: str):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content.encode("utf-8"))


PRODUCT_LIST = (
    "Name,Kind,Terms,Weight\n"
    "MagMind,product,magnesium l-threonate; threonate; magmind,90\n"
    "Magnesium,product,,50\n"
    "Women over 40,audience,menopause|perimenopause,60\n"
)


def test_a_csv_product_list_is_added_to_a_copy_of_the_live_profile(
    client, admin, jarrow, live_profile
):
    """A product list is only products. Importing one must not drop the
    audiences and priorities somebody wrote by hand."""
    with scoped(jarrow):
        ClientTerm.objects.create(
            organization=jarrow, profile=live_profile, facet=ClientTerm.Facet.PRIORITY,
            name="Sleep", terms=["sleep"],
        )
    client.force_login(admin)

    client.post(
        reverse("ops-client-profile-import", args=[jarrow.slug]),
        {"file": _upload("jarrow-products.csv", PRODUCT_LIST)},
    )

    with scoped(jarrow):
        draft = ClientProfileVersion.objects.get(number=live_profile.number + 1)
        assets = {a.name: a for a in draft.assets.all()}
        terms = {(t.facet, t.name) for t in draft.terms.all()}
        assert not draft.is_current, "an import is reviewed before it goes live"
        assert set(assets) == {"Magnesium", "MagMind"}, "a name already present is not duplicated"
        assert assets["MagMind"].terms == ["magnesium l-threonate", "threonate", "magmind"]
        assert assets["MagMind"].weight == 90
        assert ("priority", "Sleep") in terms, "the hand-written priority survives"
        assert ("audience", "Women over 40") in terms
        assert ClientProfileVersion.objects.get(number=live_profile.number).is_current


def test_a_csv_without_a_name_column_is_refused_and_drafts_nothing(client, admin, jarrow):
    client.force_login(admin)

    response = client.post(
        reverse("ops-client-profile-import", args=[jarrow.slug]),
        {"file": _upload("wrong.csv", "sku,price\nJF-1,10\n")},
        follow=True,
    )

    assert "needs a &#x27;name&#x27; column" in response.content.decode()
    with scoped(jarrow):
        assert not ClientProfileVersion.objects.exists()


def test_an_exported_profile_imports_back_as_the_same_profile(client, admin, jarrow, live_profile):
    """The round trip: export, send to the client, correct, import."""
    client.force_login(admin)

    exported = client.get(
        reverse("ops-client-profile-export", args=[jarrow.slug, live_profile.number])
    )
    assert exported["Content-Disposition"].endswith('profile-v1.json"')

    client.post(
        reverse("ops-client-profile-import", args=[jarrow.slug]),
        {"file": _upload("jarrow-profile-v1.json", exported.content.decode())},
    )

    with scoped(jarrow):
        reimported = ClientProfileVersion.objects.get(number=2)
        assert services.to_spec(reimported)["assets"] == services.to_spec(live_profile)["assets"]


def test_a_json_file_that_is_not_a_profile_is_refused(client, admin, jarrow):
    client.force_login(admin)

    response = client.post(
        reverse("ops-client-profile-import", args=[jarrow.slug]),
        {"file": _upload("profile.json", "[1, 2, 3]")},
        follow=True,
    )

    assert "one JSON object" in response.content.decode()
    with scoped(jarrow):
        assert not ClientProfileVersion.objects.exists()


def test_a_plain_operator_cannot_import_a_profile(client, plain_operator, jarrow):
    client.force_login(plain_operator)

    response = client.post(
        reverse("ops-client-profile-import", args=[jarrow.slug]),
        {"file": _upload("jarrow-products.csv", PRODUCT_LIST)},
    )

    assert response.status_code == 403
