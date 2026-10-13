"""Not mounted. The Ingestion Runs screen is served by `apps.enrichment.urls`.

A runs page has to show the claims each run produced, and `ingestion` sits
below both `evidence` and `enrichment` in the layer contract, so it cannot read
them. Kept empty rather than deleted so the next screen that genuinely belongs
here has an obvious home.
"""

urlpatterns = []
