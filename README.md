# teardown-pairs

Year-over-year evidence packs for page teardowns. For each brand or retailer homepage, category page (PLP) or product page, teardown-pairs puts the archive capture nearest the same date one or more years back next to the page today, and records what changed. It gathers evidence; the critique stays human.

Each pack holds:

- the **then** capture: the Wayback Machine capture nearest the target date, a year (or N years) back;
- the **now** capture: the live page, the newest archive capture, or both;
- full-page and above-the-fold screenshots at desktop (1440 wide) and mobile (390 wide);
- facts read from the rendered page (title, meta, headings, navigation, above-the-fold text and buttons, CTAs, promos, counts, structured data, PDP and PLP heuristics, loaded martech);
- `diff.json` and `diff.md`, and a self-contained side-by-side `compare.html` with PNG composites.

It ships as a CLI (`scripts/teardown_pairs.py`) and an agent skill (`SKILL.md`). Archive lookups go through [wayback-salvage](https://github.com/andrewluxem/wayback-salvage) 0.2.1 or later, called as a subprocess.

Not in scope: writing or scoring the critique, logins, carts, personalised states, crawling beyond the listed URLs, pixel diffs, and any bot-wall evasion.

## Install

Python 3.12 or 3.13 in a virtual environment inside the repo (Playwright's `greenlet` dependency may have no wheel for newer Pythons; system Python is never changed):

```
cd teardown-pairs
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt        # Playwright, pinned to one version
python -m playwright install chromium
python -m unittest discover -s tests             # unit and integration tests, no internet
```

wayback-salvage 0.2.1 or later must be reachable. teardown-pairs looks in this order: `--wayback-salvage PATH`, `$WAYBACK_SALVAGE`, `~/.claude/skills/wayback-salvage/scripts/wayback_salvage.py`, then `../wayback-salvage/scripts/wayback_salvage.py` next to this repo. It runs as `python3 wayback_salvage.py ... --json`.

## pages.csv

A header row is required. Rows starting with `#` are skipped.

| column | required | notes |
|---|---|---|
| `brand` | yes | free text; slugged for paths |
| `page_type` | yes | `home`, `category` or `product` |
| `label` | yes | short name, unique per brand and type (for example `mens-jackets`) |
| `now_url` | yes | the page as it is today |
| `then_url` | no | last year's URL if the page moved; defaults to `now_url` |
| `notes` | no | carried into `pack.json` |

`examples/pages.example.csv` has three homepages and two placeholder rows. One page can also be given directly: `--url URL --brand NAME --type TYPE --label LABEL [--then-url URL]`.

Dates: `--date YYYY-MM-DD` (default today in America/Denver) and `--years-back N[,N...]` (default `1`). The target date is the same month and day N years earlier; 29 Feb maps to 28 Feb. Pass the same `--date` when resuming on another day, because the date picks the pack directory.

## Quick start

```
python scripts/teardown_pairs.py coverage pages.csv
python scripts/teardown_pairs.py capture pages.csv --max-pages 12 -o out
open out/index.html
```

`coverage` makes CDX lookups only: captures within +/-7 and +/-30 days of each target date, the nearest one, its distance and weekday. Category and product rows with no captures get one prefix lookup (20 rows) listing URL variants worth trying as `then_url`.

`capture` builds the packs. Rerun the same command until it exits 0; sides already done are not loaded again.

| Subcommand | Does |
|---|---|
| `coverage` | CDX lookups per row and target date; no page loads. `--wide` also counts +/-90 days. |
| `capture` | Builds packs. `--now-source live\|archive\|both` (default `both`), `--viewports desktop,mobile`, `--images` (also run wayback-salvage `images` for the then capture), `--max-pages N` (default 20), `--delay SECONDS` (default 20), `--wide`, `--dry-run`, `--ignore-robots`, `-o/--out DIR` (default `./teardown-out`), `--quiet`, `--json`. |
| `now-only` | Live pages only, same layout with no `then/`: a baseline for next year. |
| `diff` | Offline: recompute `diff.json`, `diff.md` and `compare.html` from saved facts. |
| `report` | Offline: rebuild `<out>/index.html`. |

## How a capture is chosen

- Candidates come from wayback-salvage `snapshots --status 200`, keeping 200 `text/html` rows. The window is +/-7 days, widened to +/-30 when that is empty, and to +/-90 only with `--wide`.
- Nearest first; ties go to the earlier capture. `diff.md` warns when the capture is more than 7 days from the target.
- A capture that loads as a block, challenge or outage page (suspect), or whose load is throttled (degraded), is replaced by the next candidate, up to 3 loads per side. Later candidates with the same digest as a suspect capture are skipped. Every attempt is recorded in `pack.json`.
- now-archive uses the newest capture from 30 days before `--date` up to `--date`.

## Pack layout

```
<out>/<brand>/<page_type>/<label>/<target-date>/
  pack.json            status per side, rows, attempts, versions, timings
  then/                page.html, analysis.json, facts.json, capture.json, requests.jsonl,
                       desktop.png, desktop-fold.png, mobile.png, mobile-fold.png, images/ (with --images)
  now-live/            the same without analysis.json; page.html is the live main document
  now-archive/         the same as then/
  compare.html         then and now side by side: desktop and mobile, fold and full, facts diff table
  compare-desktop.png  compare-mobile.png
  diff.json  diff.md
<out>/index.html       one row per pack, thumbnails and flags
<out>/.wayback-salvage/  wayback-salvage's own manifest and request log
```

Side status is one of `planned`, `done`, `suspect`, `blocked`, `skipped`, `failed`. A rerun redoes `planned` and `failed` sides and leaves the rest alone: `suspect` (every candidate was a block or outage page) and `blocked` (the live page served a bot wall) are recorded outcomes, and live pages are not retried after a block. `skipped` (robots.txt) is redone only with `--ignore-robots`. To force a redo, delete the pack directory. With several `--years-back` values, the now sides are loaded once and copied into the other packs.

`capture.json` per side records each load: request totals (`requests`, `failed`, `aborted_budget`, `leaked`, `aborted_media`, `http_429`, `bytes`, main document status), consent clicks, the user agent, screenshot sizes and whether a full-page shot was truncated. `requests.jsonl` lists every request with its host, resource type, status, bytes and abort reason.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | usage or unexpected error |
| 2 | `--max-pages` reached (resumable: rerun the same command) |
| 3 | stopped on archive throttling (resumable: rerun later, do not lower `--delay`) |
| 4 | wayback-salvage missing or older than 0.2.1 |
| 5 | Playwright or Chromium missing (prints the install command) |

wayback-salvage's own exits 2 and 3 come through as 2 and 3.

## Politeness defaults

- Archive page loads at least `--delay` seconds apart (default 20), start to start. Do not go below 10.
- wayback-salvage calls at least 6 s apart, start to start (each subprocess has its own limiter).
- Per archive load: at most 4 archive subrequests in flight and 600 in total; the rest are aborted and counted. Requests to any non-archive host are aborted and counted as `leaked`, so last year's page never mixes with today's live web. Video and audio are never fetched.
- More than 10 HTTP 429s marks an archive load degraded; three degraded loads in a row stop the run with exit 3.
- Live: at most one load per host every 30 s, robots.txt honoured for `*` (`--ignore-robots` to override), no retries when blocked.
- `--max-pages` counts page loads, archive and live. A pack with `--now-source both` and both viewports costs 2 archive loads, 2 live loads and 4 to 8 wayback-salvage calls, so a 10-page batch needs about `--max-pages 40` or two runs.
- One Chromium, one page load at a time, Playwright's stock user agents. No stealth plugins, no spoofing, no captcha handling. A blocked live page is recorded as blocked.

## Caveats to state in every critique

- **Crawler view.** Archive captures show what the Wayback Machine crawler received: logged out, no cookies, mostly a desktop user agent, one A/B variant.
- **Mobile basis.** The archived HTML is what a desktop crawler received, so archive mobile screenshots are the same page resized to 390x844 (`mobile_basis=desktop-capture-resized`). Live mobile screenshots use Playwright's iPhone 13 profile (`mobile_basis=mobile-device-profile`). `diff.md` states the mismatch.
- **Holes.** Third-party image hosts are often not archived. High `failed` counts mean blank areas in archive screenshots are missing archive assets, not design choices.
- **Rendered, not raw.** Facts come from the rendered DOM. Martech is the exception: loaded `<script src>` hosts are read from the raw HTML on both sides (wayback-salvage `page --json` for archive sides, the live main document for now-live), and hosts only mentioned inside inline scripts are listed apart.
- **Suspect pages.** Status 403, 429 or 503; titles or short pages saying "Access Denied", "Just a moment", "Attention Required", "Pardon Our Interruption", "Request unsuccessful", "Verify you are human", "Are you a robot" or "Temporarily Offline"; Akamai "Reference #" error pages; captcha frames; or pages with under 300 characters of text and no images above the fold. Archive outage pages are never kept as captures.
- **Heuristics.** PDP and PLP fields (first price after the H1, add-to-cart control, review counts, price-like elements, filter elements) are pattern matches, labelled as heuristics in the output.

## Tests

`python -m unittest discover -s tests`. Unit tests run behind a socket guard that allows loopback only. Integration tests (skipped when Playwright or Chromium is missing) serve fixture pages from a local `http.server` and route archive URLs to it through the renderer's request handler; any request that would leave the machine is aborted and fails the test. The wayback-salvage JSON fixtures in `tests/fixtures/ws/` are recorded offline by `tests/record_ws_fixtures.py ../wayback-salvage`, which drives wayback-salvage's own `main()` with its fake transport.

## License

MIT. Copyright (c) 2026 Andrew Luxem.
