"""PRD §2 — the second-client fixture.

"A second client organization and a non-supplement domain pack must both load
 and complete an ingestion-to-output cycle without a core schema migration.
 This proves portability and tenancy isolation without onboarding a real second
 client."

This is a test fixture and must never be billable, notifiable or visible as a
live tenant. `Organization.is_fixture` is what keeps that true.

PRD §14 acceptance criterion: one public signal receives DIFFERENT scores for
Jarrow and this fixture. A fixture that scores identically would pass isolation
tests while proving nothing about tenant-specific scoring.
"""
