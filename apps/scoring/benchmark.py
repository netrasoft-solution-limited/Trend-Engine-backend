"""AI benchmark answers — imported from Mark's existing AI Brand Test.

Nothing here asks an assistant anything: no new provider, no new spend (Mark,
21 Oct). One CSV row per answer; the summary counts rows, so every share in a
brief is a count someone can check against the file.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

from django.db import transaction

from .models import BenchmarkAnswer

COLUMNS = ["run_date", "question", "assistant", "model", "brands", "client_named",
           "position", "excerpt"]

EXAMPLE = {
    "run_date": "2026-10-15",
    "question": "What is the best probiotic for bloating?",
    "assistant": "ChatGPT",
    "model": "",
    "brands": "Seed; Culturelle; Jarrow Formulas",
    "client_named": "yes",
    "position": "3",
    "excerpt": "",
}

_TRUE = {"yes", "y", "true", "1", "x"}


class BenchmarkError(ValueError):
    pass


def template_csv() -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerow(EXAMPLE)
    return out.getvalue()


def _brands(text: str) -> list[str]:
    return [b.strip() for b in re.split(r"[;|]", text or "") if b.strip()]


def clean(raw: dict, *, client_names: list[str]) -> dict:
    get = lambda key: str(raw.get(key) or "").strip()  # noqa: E731
    for key in ("run_date", "question", "assistant"):
        if not get(key):
            raise BenchmarkError(f"'{key}' is required.")
    try:
        run_date = date.fromisoformat(get("run_date")[:10])
    except ValueError as exc:
        raise BenchmarkError(f"'run_date' {get('run_date')!r} is not a date (YYYY-MM-DD).") from exc
    brands = _brands(get("brands"))
    lowered = [n.lower() for n in client_names if n]
    found = next(
        # "Jarrow" in an answer names "Jarrow Formulas", and the reverse.
        (i for i, b in enumerate(brands, start=1)
         if any(n in b.lower() or (len(b) >= 4 and b.lower() in n) for n in lowered)), None
    )
    named = get("client_named").lower() in _TRUE if get("client_named") else found is not None
    position = int(get("position")) if get("position").isdigit() else (found if named else None)
    return {
        "run_date": run_date, "question": get("question"), "assistant": get("assistant"),
        "model": get("model"), "brands": brands, "client_named": named,
        "position": position if named else None, "excerpt": get("excerpt"),
    }


def parse_csv(text: str, *, client_names: list[str]) -> tuple[list[dict], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "question" not in [f.strip() for f in reader.fieldnames]:
        return [], ["The file needs the template's header row (download the template)."]
    rows, errors = [], []
    for n, raw in enumerate(reader, start=2):
        if not any(str(v or "").strip() for v in raw.values()):
            continue
        try:
            rows.append(clean({(k or "").strip(): v for k, v in raw.items()},
                              client_names=client_names))
        except BenchmarkError as exc:
            errors.append(f"Row {n}: {exc}")
    return rows, errors


@transaction.atomic
def save_all(organization, rows: list[dict], *, entered_by: str, batch: str) -> int:
    BenchmarkAnswer.objects.bulk_create([
        BenchmarkAnswer(organization=organization, **row, entered_by=entered_by,
                        import_batch=batch)
        for row in rows
    ])
    return len(rows)


@dataclass
class QuestionSummary:
    question: str
    answers: int
    named: int
    assistants: list[str]
    #: Brands most often named, with counts, client included.
    leaders: list[tuple[str, int]]
    run_dates: tuple[date, date]

    @property
    def share(self) -> str:
        return f"{self.named} of {self.answers}"


def summary(organization, questions: list[str] | None = None) -> list[QuestionSummary]:
    answers = BenchmarkAnswer.objects.filter(organization=organization)
    if questions is not None:
        answers = answers.filter(question__in=questions)
    grouped: dict[str, list[BenchmarkAnswer]] = {}
    for a in answers.order_by("question", "run_date"):
        grouped.setdefault(a.question, []).append(a)
    out = []
    for question, rows in grouped.items():
        counts: dict[str, int] = {}
        for row in rows:
            for brand in dict.fromkeys(row.brands):
                counts[brand] = counts.get(brand, 0) + 1
        out.append(QuestionSummary(
            question=question,
            answers=len(rows),
            named=sum(1 for r in rows if r.client_named),
            assistants=sorted({r.assistant for r in rows}),
            leaders=sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5],
            run_dates=(rows[0].run_date, rows[-1].run_date),
        ))
    return out


def brief_lines(organization, questions: list[str]) -> list[str]:
    """What AI answers show today, for a brief. Says so when nothing was imported."""
    if not questions:
        return []
    found = summary(organization, questions)
    if not found:
        return ["AI benchmark not imported for these questions yet."]
    lines = []
    for s in found:
        leaders = ", ".join(f"{b} ({n})" for b, n in s.leaders[:3])
        lines.append(
            f"“{s.question}” — {organization.name} named in {s.share} answers "
            f"({', '.join(s.assistants)}, {s.run_dates[0]:%-d %b}–{s.run_dates[1]:%-d %b %Y}). "
            f"Most named: {leaders}."
        )
    return lines
