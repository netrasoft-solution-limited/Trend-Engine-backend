# Delivery plan: end-to-end workflow (Mark, 20 Nov 2026)

Mark needs one repeatable path inside the application: fresh research in, client-ready briefs out, for any brand. The only work left to people should be expert review.

| Date (11:00am ET) | What is shown |
|---|---|
| Wed 2 Dec | Checkpoint demo: Jarrow end to end |
| Wed 9 Dec | Completion and final demo: Jarrow and Nature's Way |

Each demo starts with a fresh research run, ends with downloaded PDF and Word files, and shows every manual step.

## What is needed from Mark

| Item | Needed by |
|---|---|
| Search-data provider approved, with an account (proposed: DataForSEO) | Tue 24 Nov |
| Nature's Way catalogue: a feed, an export or a site URL | Fri 27 Nov |

If either arrives late, the date that depends on it moves by the same number of days.

## Work items

### 1. Research cycle per brand (`apps/publication/cycle.py`)
- **Brand sources:** a `BrandSource` model (tenant-scoped) links an organisation to the global `Source` rows it draws on.
- **The cycle:** a `ResearchCycle` model (tenant-scoped) records each stage with its status, counts, errors and timestamps. The stages run in this order:
  1. **collect:** poll every source linked to the brand.
  2. **process:** the gate, then transcripts, then extraction. Wait for the queue to clear, re-dispatching anything left pending or unanalysed, with a time limit.
  3. **detect:** enough batches to cover the claims the cycle collected, then ranking.
  4. **recommend:** draft recommendations for the top-ranked signals that have none.
  5. **discover:** propose ingredients the catalogue lacks, then collect current evidence for each candidate.
  6. **search:** refresh search volumes and Trends readings for each story and candidate. This stage waits on the provider.
  7. **review (manual):** list every signal, recommendation and candidate that needs a decision, each with its link.
  8. **briefs:** draft both briefs from what is approved, so they can be exported and sent for review.
- **Orchestration:** one Celery task per stage. A stage that is waiting reschedules itself with a countdown, so no worker is blocked.
- **Status page:** `/cycles/<slug>/` shows the stages, counts, failed or empty sources, links to the runs, the cycle's AI and Virlo cost, and a "Start a research cycle" button.

### 2. Story classification
Each story and candidate gets exactly one label:

| Label | Meaning |
|---|---|
| `existing` | Supports a listing the brand already sells |
| `gap` | An ingredient or product the catalogue lacks |
| `format` | Something the catalogue has, wanted in a new form (gummy, powder, liquid and so on) |
| `audience` | Something the catalogue has, raised for a group its profile does not target |

- It is drafted with the recommendation and checked against the catalogue.
- Both briefs show it, together with the matching listings.

### 3. Search data
- A provider-neutral interface, with DataForSEO as the first implementation:
  - monthly volume for each keyword (Google Ads data)
  - Google Trends, comparing the latest 28 days with the 28 days before
- Results are stored as Observations with the retrieval date, the period, the provider and the uncertainty.
- The work has a per-cycle limit and a monthly cap, and records each call in the cost ledger.

### 4. Narrative writing
- One model call per section writes the decision summary, each story and each opportunity case.
- It writes only from the approved evidence, and every sentence must cite a source number.
- A validator drops any sentence that names no cited source, or a source that is not in the brief.
- The draft is still marked as such, and expert review stays a step.

### 5. Reddit collector
- Replace `shahab_adl~reddit-scraper-no-login`, which has returned 0 posts since 15 Nov.
- Test the replacement on a few posts first, within the Apify budget.

### 6. Second brand onboarding
- Create the organisation from its storefront or catalogue import, so it gets a draft profile.
- Assign its sources from the catalogue families, using a template of queries per family.
- Its first cycle runs the same way as Jarrow's.

### 7. Smaller fixes
- An extraction sent twice for an item with no claims runs twice and is billed twice.

### 8. Handover
`docs/HANDOVER.md` is kept current: architecture, deployment, credentials and where they are kept, the cycle runbook, known limits and open issues.
