# Changelog

## 0.1.0 (24 Oct 2026)

First release.

- `coverage`: CDX lookups through wayback-salvage per row and target date, captures within +/-7 and +/-30 days (+/-90 with `--wide`), nearest capture with distance and weekdays, and a 20-row prefix lookup of URL variants for category and product rows with no captures. No page loads.
- `capture`: evidence packs with a then capture (nearest 200 text/html capture, ties to the earlier one, up to 3 candidates when a capture is suspect or its load is degraded) and now-live, now-archive or both. Screenshots at 1440 and 390 wide, full page (capped at 20,000 px) and above the fold. Facts from the rendered DOM via `scripts/facts.js`; martech from loaded script hosts on both sides. `diff.json`, `diff.md`, `compare.html`, `compare-desktop.png`, `compare-mobile.png`, and `<out>/index.html`.
- `now-only`: live captures in the same layout with no `then/`, as next year's baseline.
- `diff` and `report`: offline regeneration from saved facts.
- Archive loads are paced (`--delay`, default 20 s start to start), budgeted (600 archive subrequests, 4 in flight) and sealed off from the live web (other hosts aborted and counted as `leaked`). Video and audio are never fetched. More than 10 HTTP 429s mark a load degraded; three in a row stop the run with exit 3.
- Live loads honour robots.txt for `*`, one load per host every 30 s, Playwright's stock user agents, no retries when blocked.
- Resumable: reruns skip finished sides and redo planned and failed ones; exit 2 at `--max-pages`, exit 3 on throttling.
- Requires wayback-salvage 0.2.1 or later (exit 4 otherwise) and Playwright 1.64.1 with Chromium (exit 5 otherwise).
