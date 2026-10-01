"""URL patterns for the Operations dashboard, mounted at `operations/`.

PRD §6.5's operations screen — connector health, the cost ledger, dead letters
— is not built yet. Sign-in is NOT here: it lives in `auth_urls.py`, mounted at
the origin root, because a module included under two prefixes served every
login page twice.
"""

urlpatterns = []
