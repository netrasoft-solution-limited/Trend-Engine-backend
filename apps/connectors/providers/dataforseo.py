"""DataForSEO: Google search volumes and Google Trends (Mark, 20 Nov).

Two live endpoints, HTTP Basic auth (login:password), one task per call:

  keywords_data/google_ads/search_volume/live   up to 1,000 keywords a task;
      per keyword: search_volume (12-month average), monthly_searches[], competition
  keywords_data/google_trends/explore/live      up to 5 keywords a task;
      items[type=google_trends_graph].data[] = {date_from, date_to, values[], missing_data}

Priced per task, not per keyword: about $0.09 for a volume task and $0.011 for
a Trends task (live, as published Nov 2026), so a brand's monthly refresh costs
cents. The prices are constants here and every call reports its cost, which
`apps.scoring.search_data` records.

Field names were taken from the published documentation; `_check` refuses a
response that does not carry them rather than storing zeros.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

API = "https://api.dataforseo.com/v3"
VOLUME_PRICE = Decimal("0.09")
TRENDS_PRICE = Decimal("0.011")
US = 2840
MAX_VOLUME_KEYWORDS = 1000
MAX_TRENDS_KEYWORDS = 5


class DataForSeoError(RuntimeError):
    pass


@dataclass
class Volume:
    keyword: str
    average: int | None
    #: (year, month, volume), newest first.
    monthly: list[tuple[int, int, int]] = field(default_factory=list)
    competition: str = ""


@dataclass
class TrendSeries:
    keywords: list[str]
    #: (date_from, date_to, values per keyword, missing)
    points: list[tuple[date, date, list[float], bool]]


class DataForSeo:
    def __init__(self, *, login: str, password: str, transport: Any | None = None) -> None:
        if not (login and password):
            raise ValueError("DataForSEO needs both a login and a password.")
        self._auth = (login, password)
        self._transport = transport
        self.cost_usd = Decimal(0)

    # ── Calls ───────────────────────────────────────────────────────────────

    def volumes(self, keywords: list[str], *, location_code: int = US,
                language_code: str = "en") -> list[Volume]:
        out = []
        for start in range(0, len(keywords), MAX_VOLUME_KEYWORDS):
            chunk = [k.lower() for k in keywords[start:start + MAX_VOLUME_KEYWORDS]]
            task = self._call("keywords_data/google_ads/search_volume/live", {
                "keywords": chunk, "location_code": location_code,
                "language_code": language_code,
            }, VOLUME_PRICE)
            for row in task.get("result") or []:
                out.append(Volume(
                    keyword=row.get("keyword", ""),
                    average=row.get("search_volume"),
                    monthly=sorted(
                        ((m["year"], m["month"], m.get("search_volume") or 0)
                         for m in row.get("monthly_searches") or []),
                        reverse=True,
                    ),
                    competition=row.get("competition") or "",
                ))
        return out

    def trends(self, keywords: list[str], *, date_from: date, date_to: date,
               location_code: int = US) -> TrendSeries:
        if not 1 <= len(keywords) <= MAX_TRENDS_KEYWORDS:
            raise ValueError(f"Google Trends compares 1 to {MAX_TRENDS_KEYWORDS} keywords a task.")
        task = self._call("keywords_data/google_trends/explore/live", {
            "keywords": keywords, "location_code": location_code,
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "type": "web", "item_types": ["google_trends_graph"],
        }, TRENDS_PRICE)
        result = (task.get("result") or [{}])[0] or {}
        graph = next((i for i in result.get("items") or []
                      if i.get("type") == "google_trends_graph"), None)
        if graph is None:
            raise DataForSeoError("The Trends response carried no graph.")
        points = []
        for row in graph.get("data") or []:
            values = row.get("values")
            values = values if isinstance(values, list) else [values]
            points.append((
                date.fromisoformat(row["date_from"]), date.fromisoformat(row["date_to"]),
                [float(v) if v is not None else 0.0 for v in values],
                bool(row.get("missing_data")),
            ))
        return TrendSeries(keywords=graph.get("keywords") or keywords, points=points)

    # ── Transport ───────────────────────────────────────────────────────────

    def _call(self, path: str, task: dict, price: Decimal) -> dict:
        body = self._post(f"{API}/{path}", [task])
        tasks = self._check(body)
        self.cost_usd += Decimal(str(tasks[0].get("cost") or price))
        return tasks[0]

    def _post(self, url: str, payload: list) -> dict:
        if self._transport is not None:
            return self._transport(url, payload)
        import httpx

        response = httpx.post(url, json=payload, auth=self._auth, timeout=60)
        if response.status_code == 401:
            raise DataForSeoError("DataForSEO rejected the login or password.")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _check(body: dict) -> list[dict]:
        if body.get("status_code") != 20000:
            raise DataForSeoError(
                f"DataForSEO: {body.get('status_code')} {body.get('status_message')}"
            )
        tasks = body.get("tasks") or []
        if not tasks:
            raise DataForSeoError("DataForSEO returned no task.")
        task = tasks[0]
        if task.get("status_code") != 20000:
            raise DataForSeoError(
                f"DataForSEO task: {task.get('status_code')} {task.get('status_message')}"
            )
        return tasks
