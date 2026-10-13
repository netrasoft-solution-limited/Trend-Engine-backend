"""`{{ text|linked }}` — body prose with its `[label](https://…)` links as anchors.

Escapes everything else, so a quote containing "<" stays a quote; only the link
syntax `exports.text.spans` recognises (http and https, nothing else) becomes
markup.
"""
from __future__ import annotations

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from apps.outputs.exports.text import spans

register = template.Library()


@register.filter
def linked(text: str) -> str:
    parts = [
        format_html('<a href="{}">{}</a>', span.href, span.text) if span.href else escape(span.text)
        for span in spans(str(text or ""))
    ]
    return mark_safe("".join(parts))  # noqa: S308 — every part is escaped above
