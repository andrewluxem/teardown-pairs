---
name: "teardown-pairs"
description: "Year-over-year evidence packs for brand and retailer page teardowns: archive capture nearest the same date last year next to today's page, screenshots at 1440 and 390, extracted facts and a facts diff. Run coverage first; the critique stays human."
---

# teardown-pairs

Builds evidence for a page teardown: for each homepage, category page (PLP) or product page, the archive capture nearest the same date one or more years back, next to the page today (live, the newest archive capture, or both). Each pack holds full-page and above-the-fold screenshots at desktop (1440) and mobile (390) widths, facts read from the rendered page, `diff.json`, `diff.md` and a side-by-side `compare.html`. It gathers evidence only. Writing the critique is the user's job, and yours only when asked, from the pack.

## When to use

- Year-over-year comparisons of brand or retailer pages for a teardown, critique or merchandising review.
- The user wants this year's page next to the same page a year ago, with what changed in headings, navigation, CTAs, promos, structured data, prices and loaded martech.
- The user wants a baseline of today's pages for next year's comparison (`now-only`).

Not for logins, carts, personalised states, crawling beyond the listed URLs, pixel diffs, scoring pages, or getting past bot walls.

## How to run it

The CLI lives in this skill's `scripts/` directory and needs the repo's `.venv` (Python 3.12 or 3.13, Playwright pinned in `requirements.txt`, Chromium installed). It calls wayback-salvage 0.2.1 or later as a subprocess (found via `--wayback-salvage PATH`, `$WAYBACK_SALVAGE`, the installed skill, or `../wayback-salvage`).

```
source <repo>/.venv/bin/activate
python <repo>/scripts/teardown_pairs.py <subcommand> pages.csv [options]
```

`pages.csv` columns: `brand,page_type,label,now_url[,then_url,notes]`; `page_type` is `home`, `category` or `product`. One page can be given as `--url URL --brand NAME --type TYPE --label LABEL [--then-url URL]`. `--date` defaults to today in America/Denver; `--years-back 1` (or `1,2`) sets the target dates.

## Workflow

1. **Always run `coverage` first.** It makes CDX lookups only, no page loads. Report to the user which rows have usable captures, how many within +/-7 and +/-30 days, the nearest capture and its distance and weekday. Category and product rows with no captures show URL variants worth trying as `then_url`; offer them.
2. **`capture`** builds the packs. Defaults: `--now-source both`, both viewports, `--max-pages 20`, `--delay 20`. A pack with `--now-source both` costs about 4 page loads (2 archive, 2 live), so a 10-page batch needs about 40: say so, and either pass `--max-pages 40` or plan two runs. `--dry-run` prints coverage plus the plan without loading anything.
3. **Read before you write.** Open each pack's `diff.md` and look at the screenshots (`compare-desktop.png`, `compare-mobile.png`, or the files under `then/`, `now-live/`, `now-archive/`) before writing any critique. `<out>/index.html` lists every pack with thumbnails and flags.
4. **State the asymmetries.** Every `diff.md` header lists them (crawler view, mobile basis, live versus archive, distance from the target). Carry them into the critique in plain words, for example "then is the archive crawler's desktop view resized to mobile width; now is a live mobile device profile".
5. **Holes are archive gaps, not design.** When a side's `failed` count is high (see `capture.json` or the diff header), blank boxes and missing images in its screenshots are assets the archive never stored. Do not critique them as design choices. `leaked` counts requests the replay tried to make to the live web, which were blocked.
6. **Distance matters.** A then capture more than 7 days from the target is flagged in `diff.md`; retail merchandising is weekly and seasonal, so say how far off it is.

## Exit codes and reruns

- `0` done. `1` usage or unexpected error. `4` wayback-salvage missing or older than 0.2.1. `5` Playwright or Chromium missing (the message has the install command).
- `2` `--max-pages` reached, `3` stopped on archive throttling. Both are resumable: tell the user to rerun the same command later. Completed sides are never loaded again. Never lower `--delay` below 10 unless the user asks for it, and never retry in a loop.
- A rerun redoes sides that are `planned` or `failed`. A side whose wayback-salvage lookup failed (anything but its exits 2 and 3) is `failed` with wayback-salvage's message as `reason` in `pack.json`; the run went on without it, and a rerun retries it. Sides that ended `suspect` (every archive candidate was a block or outage page) or `blocked` (the live page served a bot wall) are recorded outcomes, not retried; `pack.json` gives the signature.

## Rules

- Never spoof user agents, add stealth plugins, solve captchas, or try any other way past a bot wall. A blocked live page is evidence that the page blocks automated browsers; report it and use the `now-archive` comparison instead.
- `robots.txt` is honoured for live loads. Pass `--ignore-robots` only when the user decides a live capture of that brand is appropriate.
- Never write curl, wget or ad hoc fetch code against archive.org; archive lookups go through wayback-salvage, page loads through this CLI.
- For pages with no archive history, suggest `now-only pages.csv -o baseline` so next year has a baseline to compare against.
- Packs can be large and hold third-party content: keep them out of git repos.
