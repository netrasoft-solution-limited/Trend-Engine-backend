"""clients — L5, TENANT-SCOPED. Who each client is.

PRD §6.4: "A client profile carries: domains, typed assets/products,
audiences, priorities, competitors, voice, compliance rules, assigned
reviewers, and output cadence. Client scoring runs independently per tenant.
Another tenant's private context is never available to the prompt or query."

This app is the answer to the question the system could not previously ask:
*who is this for?* Without it, two clients reading briefs built from one shared
corpus receive identical documents, because nothing distinguishes them. The
evidence layer is deliberately shared (PRD §6.7); this is the layer that is
deliberately not.

There is no `Client` model. `Organization` is the client — one row, one tenant,
one client — as `apps/scoring/models.py` already records. Adding a second
identity for the same thing would create two ways to be wrong about which
tenant you are in.

WHY TERMS AND NOT EMBEDDINGS. Matching a claim to a client is done by stored
terms, compared literally. The alternative — a model deciding whether a claim
is relevant — would cost a call per claim per client (169 × N today, and the
corpus only grows), and would produce a number no operator could argue with.
Arch §8.2 requires that a score be explainable at the component level; a term
match can say *which asset matched, on which word, in which sentence*. That is
reviewable, correctable by editing a profile, and free. Embeddings would be the
better instrument for recall, and are not available — Anthropic has no
embeddings endpoint, which is already recorded as the reason clustering is
deferred.

PROFILES ARE VERSIONED AND IMMUTABLE IN USE. PRD §8: "Every output records
exactly one tenant, one client-profile version, and one domain-pack version.
This triple is what makes multi-tenancy and multi-domain auditable." An output
must stay explainable against the profile it was actually built from, not
against whatever the profile has since become — so editing a client's profile
creates a new version rather than mutating the one earlier briefs cite.
"""
from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower

from apps.tenancy.managers import TenantScopedModel


class ClientProfileVersion(TenantScopedModel):
    """One immutable snapshot of a client's commercial context.

    Versions are numbered per organisation and never reused. Exactly one is
    current at a time, enforced in the database rather than by convention —
    "which profile is live?" having two answers is the kind of thing that is
    discovered months later, in a brief that cannot be explained.
    """

    number = models.PositiveIntegerField()
    label = models.CharField(
        max_length=200,
        blank=True,
        help_text="Why this version exists, e.g. 'Added the sleep range, Q4'.",
    )
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by_label = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("organization", "-number")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "number"], name="uniq_profile_version_per_org"
            ),
            # Partial unique: many historical versions, exactly one current.
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_current=True),
                name="one_current_profile_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"profile v{self.number}" + (" (current)" if self.is_current else "")

    @property
    def citation(self) -> str:
        """What an output records, per PRD §8. Carried as a string on
        `Output.client_profile_version`, which is a CharField — so an output
        stays readable after the profile row it names is gone."""
        return f"v{self.number}"


class ClientAsset(TenantScopedModel):
    """Something the client actually sells, or wants to sell.

    This is the heaviest relevance component (PRD §6.3: 20% of the client rank
    score) because it is the most direct answer to "should this client care?".
    A magnesium claim matters to a brand with a magnesium SKU in a way it does
    not to one without.

    `terms` is the vocabulary that means this asset — the SKU name, the
    ingredient, the common misspellings, the abbreviation a podcast host would
    actually say. Operators maintain it. A missed term is a missed claim, which
    is why the Signal Review screen shows what matched: an empty match list on
    a claim that obviously should have matched is the prompt to add a word.
    """

    class Kind(models.TextChoices):
        PRODUCT = "product", "Product or SKU"
        INGREDIENT = "ingredient", "Ingredient or active"
        CATEGORY = "category", "Category or collection"
        SERVICE = "service", "Service"
        CONTENT = "content", "Content property"

    profile = models.ForeignKey(
        ClientProfileVersion, on_delete=models.CASCADE, related_name="assets"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PRODUCT)
    name = models.CharField(max_length=200)
    terms = models.JSONField(
        default=list,
        help_text="Words and phrases that mean this asset. Matched case-insensitively.",
    )
    #: Not every SKU matters equally. A hero product outranks a long-tail one,
    #: and the operator is the only one who knows which is which.
    weight = models.PositiveSmallIntegerField(default=50)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("organization", "-weight", "name")

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"


class ClientTerm(TenantScopedModel):
    """Audience, strategic priority, or competitor — the lighter components.

    One table rather than three, because all three are the same shape (a name,
    the words that mean it, a weight) and the scorer treats them the same way.
    Splitting them would be three models, three migrations and three identical
    loops, and would make adding a fourth facet a schema change instead of a
    row.

    The facet decides which component the match feeds:

      AUDIENCE   → audience fit        (PRD §6.3: 10%)
      PRIORITY   → strategic priority  (PRD §6.3: 10%)
      COMPETITOR → strategic priority, documented deviation below

    PRD §6.3's client rank is 60/20/10/10 and names no competitor weight, but
    §6.3's signal types do include "competitor move". Rather than invent a
    fifth weight the PRD does not have, a competitor match feeds the strategic
    priority component — a competitor doing something IS a strategic matter —
    and `ClaimRelevance.matches` records that it was a competitor match, so the
    reason stays visible rather than being folded away.
    """

    class Facet(models.TextChoices):
        AUDIENCE = "audience", "Audience"
        PRIORITY = "priority", "Strategic priority"
        COMPETITOR = "competitor", "Competitor"

    profile = models.ForeignKey(
        ClientProfileVersion, on_delete=models.CASCADE, related_name="terms"
    )
    facet = models.CharField(max_length=20, choices=Facet.choices, db_index=True)
    name = models.CharField(max_length=200)
    terms = models.JSONField(default=list)
    weight = models.PositiveSmallIntegerField(default=50)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("organization", "facet", "-weight", "name")

    def __str__(self) -> str:
        return f"{self.get_facet_display()}: {self.name}"


class ClientContact(TenantScopedModel):
    """Who at the client actually receives the work.

    With no client portal, an output reaches a client as an email with a
    document attached — so this table is the delivery list, and before it
    existed the only client address anywhere in the system was
    `portal.OrgUser.email`, reached through a login nobody will ever use.

    HANGS OFF THE ORGANISATION, NOT THE PROFILE VERSION, and that is the whole
    design decision. `ClientProfileVersion` is immutable in use because outputs
    cite it (PRD §8). If contacts lived on it, adding one email address would
    mint a new profile version that every later output cites — for a change
    with nothing to do with scoring. Contacts are current-state operational
    data; a profile is historical data something points back at. Different
    lifecycles, different parents.

    Competitors go the other way for the same reason: they feed
    `scoring.relevance`, so a score has to stay explainable against the exact
    version it was computed from, and they belong on the profile.

    `is_active` rather than deleting: people leave, and a delivery record that
    names an address no longer in the table is a delivery nobody can explain.
    """

    name = models.CharField(
        max_length=200,
        help_text="Required. 'We emailed it to three addresses' is not a record.",
    )
    email = models.EmailField()
    #: Free text, not choices. You cannot enumerate another company's org chart,
    #: and a wrong enum forces the operator to pick the nearest lie.
    role = models.CharField(max_length=120, blank=True)
    receives_outputs = models.BooleanField(
        default=True,
        help_text="Off for someone who should exist on the record but not be emailed.",
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    added_by_label = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("organization", "name")
        constraints = [
            # Lower(), because "Mark@…" and "mark@…" are one mailbox and two
            # rows here would send the same document twice to one person.
            models.UniqueConstraint(
                Lower("email"), "organization", name="uniq_contact_email_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        # Normalised on the way in, so the constraint above and every lookup
        # agree without each caller remembering to lower it.
        self.email = (self.email or "").strip().lower()
        return super().save(*args, **kwargs)
