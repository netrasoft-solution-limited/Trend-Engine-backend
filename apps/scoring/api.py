"""Triage endpoints of the operator plane's JSON API — `ops.<domain>/api/triage/*`.

Operator authentication comes from the ops API defaults: session auth and
`IsAuthenticated` (REST_FRAMEWORK in base.py), behind LoginRequiredMiddleware.
Nothing here is marked public. Login itself lives in `apps.operations.api`.
"""
from __future__ import annotations

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenancy.context import current_tenant
from apps.tenancy.models import Organization

from .serializers import TriageSummaryQuerySerializer
from .triage import Window, default_window, triage_summary


class TriageSummaryView(APIView):
    """GET triage/summary?from=YYYY-MM-DD&to=YYYY-MM-DD — the Triage home's counts.

    Without from/to, the last collection window. Candidates are ranked for the
    organisation the operator has narrowed scope to, if any
    (`OperatorTenantMiddleware`), and on domain score alone otherwise.
    """

    def get(self, request):
        form = TriageSummaryQuerySerializer(data=request.query_params)
        form.is_valid(raise_exception=True)
        start, end = form.validated_data.get("from"), form.validated_data.get("to")
        window = Window(start, end) if start is not None else default_window()

        scope = current_tenant()
        organization = scope if isinstance(scope, Organization) else None
        return Response(triage_summary(window, organization))
