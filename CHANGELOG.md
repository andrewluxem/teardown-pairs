# Changelog

## 0.1.1 (24 Oct 2026)

Fixes from the live acceptance run.

- A wayback-salvage failure inside one side (its exit 1, or output that is not JSON) no longer ends the run with a traceback. In the live run it hit the REI category then-side lookup. The side is marked `failed` with wayback-salvage's message as its reason, and the run carries on with the next side and pack; a rerun retries the side. wayback-salvage exits 2 and 3 still stop the run with exit 2 and 3.
- Requires wayback-salvage 0.2.2 or later (exit 4 otherwise). 0.2.2 retries refused connections and resets from archive.org and ends in exit 3 when they persist; 0.2.1 exited 1. The recorded `--json` fixtures come from 0.2.2, with two new records for its refused-connection exit 3 and a DNS-failure exit 1.
- Archive replay loads: subrequests that fail with no HTTP status (connection refused, reset, timed out) count toward the degraded threshold together with HTTP 429s, more than 10 combined. A main document that fails that way makes the load degraded, not failed, so the next candidate is tried and three degraded loads in a row stop with exit 3. `capture.json` and the attempt records gain `network_errors`, `main_error` and `degraded_reason`.
- Robots diagnostics: the now-live side in `pack.json` records `robots_url`, `robots_status` and `robots_error`, and the skip reason carries the HTTP status or the error, for example `robots-forbidden (HTTP 403)` or `robots-unreachable (connection reset)`.
- A now-archive capture more than 7 days before `--date` gets a warning in `diff.md` and `compare.html` with both dates and the gap, like the then-side distance warning, and `now_distance_days` in the `diff.json` header. In the live run: REI men's jackets 20260924040234 (30 days before 2026-10-24) and Patagonia Better Sweater 20261007153002 (17 days). `diff` adds it to existing packs offline.

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
