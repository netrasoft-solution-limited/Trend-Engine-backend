"""PRD §2 — the portability fixture.

A non-supplement domain pack that defines its own taxonomy, asset schema,
signal rules and output template WITHOUT a core database migration.

PRD §5 principle 4: "The core is industry-agnostic. Supplements-specific
concepts live in a versioned domain pack, not the core schema." If loading this
fixture ever requires a migration, that principle has been broken and the claim
that client #2 is an onboarding task rather than an engineering project is no
longer true.
"""
