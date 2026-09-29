"""Request validation for the Triage endpoints."""
from __future__ import annotations

from rest_framework import serializers


class TriageSummaryQuerySerializer(serializers.Serializer):
    """`?from=YYYY-MM-DD&to=YYYY-MM-DD`, both or neither, inclusive.

    Fields are built in `get_fields` because `from` is a Python keyword and
    cannot be declared as a class attribute.
    """

    def get_fields(self):
        return {
            "from": serializers.DateField(required=False, input_formats=["%Y-%m-%d"]),
            "to": serializers.DateField(required=False, input_formats=["%Y-%m-%d"]),
        }

    def validate(self, attrs):
        start, end = attrs.get("from"), attrs.get("to")
        if (start is None) != (end is None):
            raise serializers.ValidationError("Give both `from` and `to`, or neither.")
        if start is not None and start > end:
            raise serializers.ValidationError("`from` must not be after `to`.")
        return attrs
