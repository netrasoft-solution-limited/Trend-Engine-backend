# Operator guide: the monthly Jarrow cycle

This guide covers the operator site, https://ops.geo.omnialta.com. Sign in with your operator account; it uses a password plus an authenticator code. Every step below happens in the browser, and none needs a developer.

Steps marked **MANUAL** are decisions or entries a person makes. The system records who made each one and when. Everything else runs on its own or behind a single button.

The nav bar at the top of every page has: Triage · Clients · Outputs · Sources · Runs · Observations · Vendor keys.

> **Red banner at the top of a page?** A source's collection or the AI calls are failing right now. It names the cause in plain words (for example "the Anthropic account is out of credit") and links to Runs. It clears itself after the next successful run.

---

## 1. Keep the catalogue current (when Jarrow's range changes)

1. **Clients → Jarrow Formulas → Import a product list or profile** (**MANUAL**). Upload the full-portfolio CSV. Any sheet with `product` and `primary_listening_family` columns is recognised as a catalogue inventory.
   - Each listing is assigned to its family.
   - The live profile's audiences, priorities, competitors and any words you added to a listing by hand are kept.
2. A **draft** version opens. Check the counts and the families, then **Make live** (**MANUAL**). Nothing changes for briefs until you do.
3. To change how a product is matched, open the live version, add words to its terms, and save as a new version.

## 2. Collect evidence (rolling 30-day window)

**Automatic:**
- Podcasts are collected weekly.
- YouTube searches run daily, limited to videos uploaded in the last 30 days.
- Each new item passes a cheap relevance check. Relevant items are transcribed and their claims extracted with timestamps.
- Sponsored segments are marked and left out.
- The same conversation published twice (a podcast and its video, or a clip) counts once.

**By hand (MANUAL):**
- **Sources → Add**: a podcast series or a YouTube search. **Collect now** on a source runs it immediately.
- **Observations**: Reddit, Instagram, TikTok Shop, search estimates and Google Trends readings. Use **Download the template**, fill one row per observation, then **Import**. You can also use **Add one by hand**.
  - Every row needs its link and dates.
  - Search figures need their query, provider, geography and period.
  - Use the label columns for advertisements, brand content, older material and coverage gaps.
  - The `event_key` column ties together observations of the same event, such as two posts about one study, so they count once.
  - A figure with no public page, such as third-party keyword data, may leave `url` empty only if `provenance` says where it came from.
  - If any row is wrong, nothing is imported, and you're told which rows to fix.

**Runs** shows every collection and AI call with its result.

## 3. Find what matters (Triage)

1. **Triage → Jarrow Formulas.** Developments are ranked for Jarrow:
   - **Rank** is 60% how much a development is moving and 40% how it touches Jarrow's products, audiences and priorities.
   - **Confidence** is shown separately.

   **Detect signals now** reads the last 35 days of claims; it also runs every morning.
2. **Status in words:**
   - **Watch item:** not yet enough over time to call it a trend.
   - **Recurring discussion:** raised in more than one week.
   - **Measured rising trend:** only when the movement is actually measured.
3. **Start a development from observations** (**MANUAL**, on Triage): for an analyst-led story built from what you entered.
   - It is labelled analyst-led everywhere.
   - Its status is earned: two independent events in different weeks make a recurring discussion.
   - Product pages and fact sheets attach as references, not as discussion.

## 4. Review a development and decide (Signal Review)

On a development's page you see:
- its evidence (quoted claims with timestamps and links)
- contradicting sources
- attached observations
- which Jarrow products it touches

Then:

1. **Observations** card (**MANUAL**): attach observations by number, for example `O12, O14-16`.
2. **Decide on the signal** (**MANUAL**): approve, watch or reject it.
3. **Recommendation for Jarrow:** **Draft** it (one AI call).
   - Every action cites claims (C) or observations (O).
   - Only Jarrow products are named.
   - What the evidence does not prove is stated.

   Edit it if needed, then **Approve** (**MANUAL**). If you spot a problem after approving, use **Withdraw approval** with a reason, then edit and approve again.
4. **AI benchmark questions:** tie the questions this development should win.
5. **Draft a content brief:** the full content package. Every piece is labelled as a draft for Jarrow Scientific Affairs review:
   - article plan and article draft
   - product-page FAQs
   - video script, email and social posts
   - AI questions to win, and what to measure

   Drafts are written in a neutral voice. Any phrase that invents expert experience or implies an endorsement is listed under **Review flags** for you to fix.

## 5. New-product opportunities

1. **Triage → New-product candidates → Add a candidate** (**MANUAL**): give its name, other names it is sold under, and a theme.
   - The catalogue gap check runs at once: "not found in N listings", or the listings that match and where.
   - Record Jarrow's own check of retail-only lines and the pipeline when it comes back.
2. **Adjacent listings:** suggested from the theme; edit as needed.
3. **Observations** (**MANUAL**): attach by number.
4. **The case:**
   - **Draft from the observations** (one AI call), or write it yourself.
   - Set the status (**Test now / Investigate next / Keep on watch**) and priority (**MANUAL**: commercial judgement, not a score).
   - **Approve.**
   - Changing the case, its observations or its adjacent listings withdraws approval.

## 6. AI benchmark

**Triage → AI benchmark → Import** (**MANUAL**): the AI Brand Test export, one row per answer. Use **Download the template** for the columns. Briefs report how often Jarrow is named, as counts of imported answers. Until an export is imported they say "not imported".

## 7. Produce, edit and review the briefs (Outputs)

1. Draft each output from its starting point:
   - **Portfolio Trend Brief:** **Triage → Draft the brief**, from approved developments.
   - **New Product Opportunity Brief:** **Candidates → Draft the opportunity brief**, from approved candidates.
   - **Content package:** **Draft a content brief**, on Signal Review.
2. On the output's page:
   - **Read / edit** any version: change, add or remove text in any section. Saving creates the next version; the one you edited is never changed.
   - **Regenerate from current evidence:** a new version from what is approved today. Use it after adding or changing evidence.
   - **Download:** PDF, Word, HTML, Markdown or CSV, under Pure Play Ecommerce Consulting branding.
     - Unpublished versions are watermarked **DRAFT — NOT PUBLISHED**.
     - A corrected record carries its correction first.
     - Every download is listed under **Downloads**.
3. **Approve** a version (**MANUAL**). This is an internal record only; nothing is visible to Jarrow.
4. **Expert sign-off** (**MANUAL**, health and scientific outputs): add the reviewer once under **Outputs → Expert reviewers**, then record their sign-off on the output. The operator recording it cannot be the reviewer.
5. **Publish** (**MANUAL**). Then **Send it to the client** (**MANUAL**). "Sent" shows only when a delivery is recorded.

## 8. Next month

1. Collection keeps running on its own.
2. Import the new observations and, if it changed, the catalogue.
3. Review new developments and candidates.
4. On each output, **Regenerate from current evidence**.
5. Compare the new version with last month's, edit, approve and download.

Earlier versions, and anything approved, published or sent, stay exactly as they were.

## Costs and limits

- AI calls run under a hard monthly cap ($50). A call that would go over it is refused before it runs.
- Typical costs:
  - a content package: about $0.30
  - a recommendation: about $0.08
  - the daily detection run: about $0.05
- YouTube collection costs about $0.004 per video.
