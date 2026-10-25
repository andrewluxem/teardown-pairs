#!/usr/bin/env python3
"""teardown-pairs: year-over-year evidence packs for page teardowns.

For each brand or retailer page (homepage, category page, product page) and
target date, build a pack: the archive capture nearest the same date one or
more years back, today's page (live, the newest archive capture, or both),
full-page and above-the-fold screenshots at desktop and mobile widths,
facts extracted from the rendered page, a facts diff and a self-contained
side-by-side compare page. The critique stays human.

Archive lookups go through wayback-salvage 0.2.2 or later, called as a
subprocess with --json. Rendering uses Playwright's Chromium with the stock
user agent: no stealth, no spoofing, nothing that tries to get past a bot
wall. Archive loads are paced (--delay, default 20 s start to start),
budgeted (600 subrequests, 4 in flight) and kept off the live web.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import copy
import csv
import dataclasses
import datetime
import hashlib
import html
import html.parser
import io
import json
import os
import re
import shutil
import sys
import time
import traceback
import unicodedata
import urllib.parse
import urllib.robotparser

VERSION = "0.1.1"
MIN_WS_VERSION = (0, 2, 2)  # 0.2.2 retries refused connections and resets, ending in exit 3
MIN_WS_TEXT = ".".join(map(str, MIN_WS_VERSION))

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MAX_PAGES = 2
EXIT_THROTTLED = 3
EXIT_WAYBACK = 4
EXIT_BROWSER = 5

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FACTS_JS_PATH = os.path.join(HERE, "facts.js")

PAGE_TYPES = ("home", "category", "product")
REQUIRED_COLUMNS = ("brand", "page_type", "label", "now_url")
OPTIONAL_COLUMNS = ("then_url", "notes")
SLUG_MAX = 80
TIMEZONE = "America/Denver"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WINDOWS = (7, 30)  # then-capture windows, days either side of the target
WIDE_WINDOW = 90  # only with --wide
NOW_WINDOW = 30  # now-archive: target -30 to 0 days, newest first
MAX_ATTEMPTS = 3
WARN_DISTANCE_DAYS = 7

DEFAULT_DELAY = 20.0  # archive page loads, start to start
WS_GAP = 6.0  # wayback-salvage subprocess calls, start to start
LIVE_HOST_GAP = 30.0  # live loads per host
DEFAULT_MAX_PAGES = 20
DEGRADED_429S = 10  # more than this many 429s and no-response failures mark an archive load degraded
DEGRADED_STOP = 3  # consecutive degraded loads stop the run (exit 3)

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
VIEWPORTS = ("desktop", "mobile")
MAX_SHOT_HEIGHT = 20000
LIVE_DEVICE = "iPhone 13"

SIDES = ("then", "now-live", "now-archive")
SIDE_STATUSES = ("planned", "done", "suspect", "blocked", "skipped", "failed")
SETTLED = ("done", "suspect", "blocked", "skipped")
NOW_SOURCES = ("live", "archive", "both")
SHOT_NAMES = ("desktop", "desktop-fold", "mobile", "mobile-fold")
WS_DIR = ".wayback-salvage"

INSTALL_HELP = ("python -m pip install -r requirements.txt && python -m playwright install chromium "
                "(inside the repo's .venv on Python 3.12 or 3.13)")

CRAWLER_VIEW = ("archive captures show the Wayback Machine crawler's view: logged out, no cookies, mostly a "
                "desktop user agent, one A/B variant")


class UsageError(Exception):
    pass


class PackConflict(UsageError):
    pass


class WaybackMissing(Exception):
    pass


class WaybackError(Exception):
    pass


class BrowserMissing(Exception):
    pass


class StopRun(Exception):
    """A resumable stop: --max-pages reached (2) or archive throttling (3)."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ dates


def target_date(base: datetime.date, years: int) -> datetime.date:
    """Same month and day `years` earlier; 29 Feb maps to 28 Feb."""
    try:
        return base.replace(year=base.year - years)
    except ValueError:
        return base.replace(year=base.year - years, day=28)


def parse_years_back(text) -> list:
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part.isdigit() or not 1 <= int(part) <= 30:
            raise UsageError("--years-back takes whole numbers from 1 to 30, comma-separated (got %r)" % text)
        if int(part) not in out:
            out.append(int(part))
    return out


def parse_date(text) -> datetime.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(text or "")):
        raise UsageError("dates are YYYY-MM-DD (got %r)" % text)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise UsageError("not a real date: %r" % text)


def today_denver(now=None) -> datetime.date:
    from zoneinfo import ZoneInfo

    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(ZoneInfo(TIMEZONE)).date()


def weekday(d: datetime.date) -> str:
    return WEEKDAYS[d.weekday()]


def days(n) -> str:
    return "%d day" % n if abs(n) == 1 else "%d days" % n


def iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_date(ts: str):
    """Date of a CDX timestamp, or None when it is not one."""
    if not ts or not ts.isdigit() or len(ts) < 8:
        return None
    try:
        return datetime.date(int(ts[0:4]), int(ts[4:6]), int(ts[6:8]))
    except ValueError:
        return None


# ------------------------------------------------------------- pages.csv


@dataclasses.dataclass(frozen=True)
class Row:
    brand: str
    page_type: str
    label: str
    now_url: str
    then_url: str
    notes: str = ""

    def key(self) -> tuple:
        return (slugify(self.brand), self.page_type, slugify(self.label))

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    def name(self) -> str:
        return "%s / %s / %s" % (self.brand, self.page_type, self.label)


def is_http_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _row_problems(brand, page_type, label, now_url, then_url) -> list:
    problems = []
    for name, value in (("brand", brand), ("page_type", page_type), ("label", label), ("now_url", now_url)):
        if not value:
            problems.append("%s is empty" % name)
    if page_type and page_type not in PAGE_TYPES:
        problems.append("page_type %r is not home, category or product" % page_type)
    if now_url and not is_http_url(now_url):
        problems.append("now_url %r is not an http(s) URL" % now_url)
    if then_url and not is_http_url(then_url):
        problems.append("then_url %r is not an http(s) URL" % then_url)
    return problems


def parse_pages_csv(text: str, source: str = "pages.csv") -> list:
    """Rows of a pages.csv. Every problem is collected and raised at once."""
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    header = None
    rows, errors, seen = [], [], {}
    for cells in reader:
        line = reader.line_num
        if not cells or not any(c.strip() for c in cells) or cells[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in cells]
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing:
                raise UsageError("%s: the header row lacks %s (columns: brand,page_type,label,now_url and "
                                 "optionally then_url,notes)" % (source, ", ".join(missing)))
            continue
        rec = {name: (cells[i].strip() if i < len(cells) else "") for i, name in enumerate(header)}
        page_type = rec.get("page_type", "").lower()
        problems = _row_problems(rec.get("brand"), page_type, rec.get("label"), rec.get("now_url"),
                                 rec.get("then_url"))
        if problems:
            errors.append("line %d: %s" % (line, "; ".join(problems)))
            continue
        row = Row(rec["brand"], page_type, rec["label"], rec["now_url"], rec.get("then_url") or rec["now_url"],
                  rec.get("notes", ""))
        if row.key() in seen:
            errors.append("line %d: duplicate label %r for %s %s (first on line %d)"
                          % (line, row.label, row.brand, row.page_type, seen[row.key()]))
            continue
        seen[row.key()] = line
        rows.append(row)
    if header is None:
        raise UsageError("%s: no header row" % source)
    if errors:
        raise UsageError("%s has problems:\n  %s" % (source, "\n  ".join(errors)))
    if not rows:
        raise UsageError("%s has no page rows" % source)
    return rows


def row_from_args(url, brand, page_type, label, then_url) -> Row:
    page_type = (page_type or "").lower()
    problems = _row_problems(brand, page_type, label, url, then_url)
    if problems:
        raise UsageError("single-page form: %s" % "; ".join(problems))
    return Row(brand, page_type, label, url, then_url or url, "")


# ------------------------------------------------------- slugs and paths


def slugify(text, cap: int = SLUG_MAX) -> str:
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(slug) > cap:
        slug = slug[:cap].rstrip("-")
    if not slug:
        slug = "item-" + hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8]
    return slug


def safe_path(out_dir: str, *segments) -> str:
    """out_dir joined with segments, asserted to stay inside out_dir."""
    root = os.path.realpath(out_dir)
    for seg in segments:
        seg = str(seg)
        if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or "\x00" in seg or os.path.isabs(seg):
            raise ValueError("unsafe path segment %r" % seg)
    path = os.path.join(root, *[str(s) for s in segments])
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root:
        raise ValueError("%s escapes %s" % (path, root))
    return path


def pack_dir(out_dir: str, row: Row, target: datetime.date) -> str:
    return safe_path(out_dir, slugify(row.brand), row.page_type, slugify(row.label), target.isoformat())


def write_text(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_json(path: str, obj) -> None:
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------ capture selection


def is_usable_row(r: dict) -> bool:
    """A 200 text/html capture with a real timestamp."""
    mime = (r.get("mimetype") or "").split(";", 1)[0].strip().lower()
    return str(r.get("status", "")) == "200" and mime == "text/html" and ts_date(r.get("timestamp", "")) is not None


def _candidate(r: dict, target: datetime.date, window, offset: int) -> dict:
    d = ts_date(r["timestamp"])
    return {
        "timestamp": r["timestamp"],
        "original": r.get("original", ""),
        "digest": r.get("digest", ""),
        "date": d.isoformat(),
        "offset_days": offset,
        "distance_days": abs(offset),
        "target_date": target.isoformat(),
        "target_weekday": weekday(target),
        "capture_weekday": weekday(d),
        "window_days": window,
    }


def _usable_unique(rows) -> list:
    out, seen = [], set()
    for r in rows:
        key = (r.get("timestamp"), r.get("original"))
        if is_usable_row(r) and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def ranked_within(rows, target: datetime.date, days: int) -> list:
    """Usable rows within +/-days, nearest first, ties to the earlier capture."""
    out = []
    for r in _usable_unique(rows):
        offset = (ts_date(r["timestamp"]) - target).days
        if abs(offset) <= days:
            window = next((w for w in WINDOWS + (WIDE_WINDOW,) if abs(offset) <= w), days)
            out.append(_candidate(r, target, window, offset))
    out.sort(key=lambda c: (c["distance_days"], c["timestamp"]))
    return out


def select_candidates(rows, target: datetime.date, wide: bool = False) -> dict:
    """Candidates from the narrowest window that has any: +/-7, +/-30, and
    +/-90 only with wide."""
    for w in WINDOWS + ((WIDE_WINDOW,) if wide else ()):
        cands = ranked_within(rows, target, w)
        if cands:
            return {"window_days": w, "candidates": cands}
    return {"window_days": None, "candidates": []}


def select_now_candidates(rows, date: datetime.date) -> dict:
    """now-archive: captures from date-30 to date, newest first."""
    out = []
    for r in _usable_unique(rows):
        offset = (ts_date(r["timestamp"]) - date).days
        if -NOW_WINDOW <= offset <= 0:
            out.append(_candidate(r, date, NOW_WINDOW, offset))
    out.sort(key=lambda c: c["timestamp"], reverse=True)
    return {"window_days": NOW_WINDOW if out else None, "candidates": out}


def cdx_window(target: datetime.date, days: int) -> tuple:
    d = datetime.timedelta(days=days)
    return (target - d).strftime("%Y%m%d"), (target + d).strftime("%Y%m%d")


def now_window(date: datetime.date) -> tuple:
    return (date - datetime.timedelta(days=NOW_WINDOW)).strftime("%Y%m%d"), date.strftime("%Y%m%d")


async def archive_candidates(ws, url: str, target: datetime.date, wide: bool):
    """Then-capture candidates, querying lazily: +/-7 first, then +/-30,
    then +/-90 with --wide, each wider query yielding only new rows."""
    seen = set()
    for w in WINDOWS + ((WIDE_WINDOW,) if wide else ()):
        frm, to = cdx_window(target, w)
        rows = await ws.snapshots(url, frm, to)
        for cand in ranked_within(rows, target, w):
            key = (cand["timestamp"], cand["original"])
            if key not in seen:
                seen.add(key)
                yield cand


async def now_archive_candidates(ws, url: str, date: datetime.date):
    frm, to = now_window(date)
    rows = await ws.snapshots(url, frm, to)
    for cand in select_now_candidates(rows, date)["candidates"]:
        yield cand


def archive_url(ts: str, original: str) -> str:
    """Replay URL without the archive banner."""
    quoted = urllib.parse.quote(original, safe="%/:=&?~#+!$,;'@()*[]")
    return "https://web.archive.org/web/%sif_/%s" % (ts, quoted)


def is_archive_host(host) -> bool:
    host = (host or "").lower()
    return host == "archive.org" or host.endswith(".archive.org")


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


_ARCHIVE_PREFIX_RE = re.compile(r"^https?://web\.archive\.org/web/\d{1,14}(?:[a-z]{2}_)?/+(.*)$", re.I | re.S)


def unwrap_archive(value: str) -> str:
    """The original URL inside a Wayback replay URL; other strings unchanged."""
    m = _ARCHIVE_PREFIX_RE.match(value)
    if not m:
        return value
    rest = m.group(1)
    s = re.match(r"^(https?):/+(.*)$", rest, re.I | re.S)
    if s:
        return s.group(1).lower() + "://" + s.group(2)
    return "http://" + rest.lstrip("/")


def normalize_facts(facts):
    """Unwrap archive replay URLs everywhere except page.final_url."""
    if not isinstance(facts, dict):
        return facts

    def walk(v):
        if isinstance(v, str):
            return unwrap_archive(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    final_url = (facts.get("page") or {}).get("final_url")
    out = walk(facts)
    if isinstance(out.get("page"), dict):
        out["page"]["final_url"] = final_url
    return out


# ---------------------------------------------------- suspect detection

BLOCK_PHRASES = ("Access Denied", "Just a moment", "Attention Required", "Pardon Our Interruption",
                 "Request unsuccessful", "Verify you are human", "Are you a robot", "Temporarily Offline")
# Block and challenge pages are short. Phrases in the visible text, the
# Akamai reference block and the form captchas count only on pages with
# less visible text than this, so a long retail page that says "temporarily
# offline" or loads reCAPTCHA for its newsletter form is not flagged.
BLOCK_TEXT_MAX = 2000
THIN_TEXT = 300
CAPTCHA_MARKERS = ("hcaptcha", "recaptcha", "px-captcha", "captcha-delivery")
BLOCK_ONLY_CAPTCHAS = ("px-captcha", "captcha-delivery")
REFERENCE_RE = re.compile(r"Reference\s*#\s*\d+(?:\.[0-9a-f]+)+", re.I)


def _short(s) -> bool:
    return s.get("text_chars") is not None and s["text_chars"] < BLOCK_TEXT_MAX


def _status_test(code):
    return lambda s: "main document status %d" % code if s.get("status") == code else None


def _phrase_test(phrase):
    low = phrase.lower()

    def test(s):
        if low in (s.get("title") or "").lower():
            return "title %r contains %r" % (s.get("title"), phrase)
        if _short(s) and low in (s.get("text") or "").lower():
            return "visible text (%d characters) contains %r" % (s["text_chars"], phrase)
        return None

    return test


def _akamai_test(s):
    if _short(s) and (s.get("reference_block") or REFERENCE_RE.search(s.get("text") or "")):
        return "Akamai-style Reference # block on a %d-character error page" % s["text_chars"]
    return None


def _captcha_test(marker):
    def test(s):
        if marker in (s.get("captcha") or []) and (marker in BLOCK_ONLY_CAPTCHAS or _short(s)):
            return "captcha iframe or script (%s)" % marker
        return None

    return test


def _thin_test(s):
    if s.get("text_chars") is not None and s["text_chars"] < THIN_TEXT and not s.get("fold_images"):
        return "%d characters of visible text and no images in the first viewport" % s["text_chars"]
    return None


SUSPECT_SIGNATURES = tuple(
    [("status-%d" % c, "main document status %d" % c, _status_test(c)) for c in (403, 429, 503)]
    + [("phrase-" + slugify(p), 'title or visible text contains "%s"' % p, _phrase_test(p)) for p in BLOCK_PHRASES]
    + [("akamai-reference", "Akamai-style Reference # block with an error-page layout", _akamai_test)]
    + [(m if m.startswith("captcha") else "captcha-" + m, "captcha iframe or script: %s" % m, _captcha_test(m))
       for m in CAPTCHA_MARKERS]
    + [("thin-page", "under 300 characters of visible text and no images in the first viewport", _thin_test)]
)


def suspect_signals(facts, status) -> dict:
    if not facts:
        return {"status": status, "title": "", "text": "", "text_chars": None, "fold_images": None,
                "captcha": [], "reference_block": False}
    sig = facts.get("signals") or {}
    return {
        "status": status,
        "title": (facts.get("page") or {}).get("title") or "",
        "text": sig.get("text_sample") or "",
        "text_chars": sig.get("text_chars"),
        "fold_images": (facts.get("above_fold") or {}).get("image_count"),
        "captcha": list(sig.get("captcha") or []),
        "reference_block": bool(sig.get("reference_block")),
    }


def detect_suspect(signals: dict):
    """First matching signature as {signature, detail}, else None."""
    for sid, _desc, test in SUSPECT_SIGNATURES:
        detail = test(signals)
        if detail:
            return {"signature": sid, "detail": detail}
    return None


def classify_load(res: dict, kind: str) -> tuple:
    """(outcome, suspect) for one load: done, suspect, degraded or failed.
    On archive loads a main-document 429 is throttling, so it counts as
    degraded rather than as a property of the capture."""
    if res.get("degraded"):
        return "degraded", None
    facts = res.get("facts")
    if res.get("error") and not facts:
        return "failed", None
    sus = detect_suspect(suspect_signals(facts, res.get("main_status")))
    if sus:
        if kind == "archive" and sus["signature"] == "status-429":
            return "degraded", sus
        return "suspect", sus
    if not facts:
        return "failed", None
    return "done", None


# ----------------------------------------------------------------- robots


def robots_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/robots.txt", "", ""))


def robots_verdict(url: str, status, text: str) -> tuple:
    """(allowed, reason) for user agent * given robots.txt's status and body."""
    if status is None or status >= 500:
        return False, "robots-unreachable"
    if status in (401, 403):
        return False, "robots-forbidden"
    if status >= 300:
        return True, "missing"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse((text or "").splitlines())
    rp.modified()
    return (True, "allowed") if rp.can_fetch("*", url) else (False, "disallowed")


def robots_detail(status, error) -> str:
    """What the robots.txt request got: "HTTP 403", or the error in plain
    words ("connection reset") when there was no response."""
    return "HTTP %d" % status if status is not None else network_error_text(error)


# ------------------------------------------------------------ vendor map

VENDOR_CATEGORIES = ("analytics", "tag management", "testing and personalisation", "reviews and UGC", "chat",
                     "ads and affiliates", "consent", "other")

_VENDORS = {
    "analytics": (
        "google-analytics.com", "analytics.google.com", "omtrdc.net", "2o7.net", "hotjar.com", "segment.com",
        "segment.io", "mixpanel.com", "amplitude.com", "heapanalytics.com", "fullstory.com", "quantummetric.com",
        "contentsquare.net", "clarity.ms", "nr-data.net", "newrelic.com", "datadoghq-browser-agent.com",
        "browser-intake-datadoghq.com", "dynatrace.com", "go-mpulse.net", "akstat.io", "glassboxdigital.io",
        "decibelinsight.net", "mouseflow.com", "crazyegg.com", "chartbeat.com", "parsely.com", "scorecardresearch.com",
        "imrworldwide.com", "sentry.io", "sentry-cdn.com", "bugsnag.com", "speedcurve.com", "logrocket.io",
        "lr-ingest.io", "smartlook.com", "luckyorange.com", "pendo.io", "plausible.io", "matomo.cloud",
        "tealiumiq.com", "adobedc.net",
    ),
    "tag management": (
        "googletagmanager.com", "tagmanager.google.com", "tiqcdn.com", "adobedtm.com", "ensighten.com",
        "tagcommander.com", "commander1.com",
    ),
    "testing and personalisation": (
        "optimizely.com", "monetate.net", "dynamicyield.com", "evergage.com", "abtasty.com",
        "visualwebsiteoptimizer.com", "kameleoon.com", "kameleoon.eu", "tt.omtrdc.net", "qubit.com",
        "qubitproducts.com", "certona.net", "richrelevance.com", "nosto.com", "brcdn.com", "brsrvr.com",
        "bloomreach.com", "launchdarkly.com", "split.io", "convertexperiments.com", "sitespect.com", "unbxd.io",
        "constructor.io", "cnstrc.com", "algolia.net", "algolianet.com", "searchspring.net", "klevu.com",
        "groupbycloud.com", "coveo.com",
    ),
    "reviews and UGC": (
        "bazaarvoice.com", "powerreviews.com", "yotpo.com", "trustpilot.com", "reviews.io", "stamped.io",
        "judge.me", "okendo.io", "turnto.com", "curalate.com", "olapic.com", "olapic-cdn.com", "pixlee.com",
        "feefo.com", "reevoo.com", "dashhudson.com", "foursixty.com", "emplifi.io",
    ),
    "chat": (
        "intercom.io", "intercomcdn.com", "zdassets.com", "zopim.com", "zendesk.com", "drift.com", "driftt.com",
        "liveperson.net", "lpsnmedia.net", "salesforceliveagent.com", "gladly.com", "kustomerapp.com",
        "gorgias.chat", "gorgias.io", "tidio.co", "olark.com", "ada.support", "nuance.com", "egain.cloud",
        "livechatinc.com", "freshchat.com", "crisp.chat", "tawk.to", "helpshift.com",
    ),
    "ads and affiliates": (
        "doubleclick.net", "googlesyndication.com", "googleadservices.com", "facebook.net", "facebook.com",
        "bat.bing.com", "criteo.com", "criteo.net", "pinimg.com", "pinterest.com", "analytics.tiktok.com",
        "snapchat.com", "sc-static.net", "ads-twitter.com", "licdn.com", "amazon-adsystem.com", "taboola.com",
        "outbrain.com", "linksynergy.com", "emjcd.com", "impactradius-event.com", "dwin1.com", "awin1.com",
        "pepperjam.com", "pjtra.com", "prfct.co", "rtbhouse.com", "adroll.com", "adsrvr.org", "demdex.net",
        "everesttech.net", "rlcdn.com", "liadm.com", "agkn.com", "mathtag.com", "rubiconproject.com",
        "pubmatic.com", "adnxs.com", "quantserve.com", "attn.tv", "redditstatic.com", "yimg.com",
    ),
    "consent": (
        "cookielaw.org", "onetrust.com", "cookiebot.com", "consensu.org", "trustarc.com", "truste.com",
        "didomi.io", "usercentrics.eu", "osano.com", "termly.io", "evidon.com", "privacy-mgmt.com",
        "cookieyes.com", "iubenda.com", "transcend-cdn.com", "ketchcdn.com",
    ),
}
VENDOR_MAP = {suffix: cat for cat, suffixes in _VENDORS.items() for suffix in suffixes}


def vendor_category(host: str) -> str:
    """Category of a script host by its longest matching suffix in the map."""
    parts = (host or "").lower().strip(".").split(".")
    for i in range(len(parts)):
        cat = VENDOR_MAP.get(".".join(parts[i:]))
        if cat:
            return cat
    return "other"


def group_hosts(hosts) -> dict:
    groups = collections.defaultdict(set)
    for h in hosts:
        groups[vendor_category(h)].add(h)
    return {cat: sorted(groups[cat]) for cat in VENDOR_CATEGORIES if groups.get(cat)}


# ---------------------------------------- martech: loaded script hosts
# The same rule as wayback-salvage 0.2.1's host report, applied to the live
# main document so then and now are read the same way: hosts of the
# <script src> JavaScript the page loads, minus the page's own domain, and
# separately the hosts only mentioned inside inline JavaScript.

JS_SCRIPT_TYPES = frozenset((
    "", "module", "text/javascript", "application/javascript", "text/ecmascript",
    "application/ecmascript", "application/x-javascript", "application/x-ecmascript",
    "text/x-javascript", "text/x-ecmascript", "text/jscript", "text/livescript",
    "text/javascript1.0", "text/javascript1.1", "text/javascript1.2",
    "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
))
_JS_ESCAPE_RE = re.compile(r"\\(?:u00([0-9a-fA-F]{2})|x([0-9a-fA-F]{2})|(/))")
_JS_ESCAPE_OK = frozenset("/=&:?%+,;~@-._#")
_ABS_URL_RE = re.compile(r"""https?://[^\s"'`<>\\()\[\]{}|^;]+""", re.I)


def _unescape_js(text: str) -> str:
    def rep(m):
        if m.group(3):
            return "/"
        ch = chr(int(m.group(1) or m.group(2), 16))
        return ch if ch in _JS_ESCAPE_OK else m.group(0)

    return _JS_ESCAPE_RE.sub(rep, text)


class _ScriptCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.srcs = []
        self.js_texts = []
        self._buf = None
        self._is_js = True

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        a = {}
        for k, v in attrs:
            a.setdefault(k, v or "")
        self._is_js = (a.get("type") or "").split(";", 1)[0].strip().lower() in JS_SCRIPT_TYPES
        if a.get("src") and self._is_js:
            self.srcs.append(a["src"])
        self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._buf is not None:
            text = "".join(self._buf)
            self._buf = None
            if text.strip() and self._is_js:
                self.js_texts.append(text)

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)


def own_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def script_hosts_from_html(text: str, page_url: str) -> tuple:
    """(loaded, mentioned) as [[host, count], ...], most frequent first."""
    col = _ScriptCollector()
    try:
        col.feed(text or "")
        col.close()
    except Exception:  # noqa: BLE001  (a parse error leaves what was read)
        pass
    if col._buf is not None:
        col.handle_endtag("script")
    own = own_domain(host_of(page_url))

    def count(urls):
        counts = collections.Counter()
        for url in urls:
            host = host_of(unwrap_archive(url))
            if not host or is_archive_host(host) or host == own or host.endswith("." + own):
                continue
            counts[host] += 1
        return counts

    def ranked(counts):
        return [[h, n] for h, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    loaded = count(col.srcs)
    mentioned_urls = []
    for js in col.js_texts:
        for m in _ABS_URL_RE.finditer(_unescape_js(js)):
            mentioned_urls.append(m.group(0).rstrip(".,:"))
    mentioned = count(mentioned_urls)
    return ranked(loaded), ranked({h: n for h, n in mentioned.items() if h not in loaded})


def martech_from_analysis(info: dict) -> dict:
    return {"source": "wayback-salvage page --json", "loaded": list(info.get("script_src_host") or []),
            "mentioned": list(info.get("script_text_host") or [])}


def martech_from_live(body: bytes, url: str) -> dict:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else (body or "")
    loaded, mentioned = script_hosts_from_html(text, url)
    return {"source": "live main document", "loaded": loaded, "mentioned": mentioned}


# ----------------------------------------------------------------- pacing


class Pacer:
    """At least `gap` seconds between calls to wait(), start to start."""

    def __init__(self, gap: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap = float(gap)
        self.clock = clock
        self.sleep = sleep
        self.last = None

    async def wait(self) -> None:
        if self.last is not None:
            remaining = self.last + self.gap - self.clock()
            if remaining > 0:
                await self.sleep(remaining)
        self.last = self.clock()


class HostPacer:
    def __init__(self, gap: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self.pacers = {}

    async def wait(self, host: str) -> None:
        pacer = self.pacers.get(host)
        if pacer is None:
            pacer = self.pacers[host] = Pacer(self.gap, self.clock, self.sleep)
        await pacer.wait()


# ---------------------------------------------------- wayback-salvage


def resolve_ws_path(arg, env, home: str, repo: str) -> str:
    """--wayback-salvage, else $WAYBACK_SALVAGE, else the installed skill,
    else ../wayback-salvage next to this repo. An explicit path that does
    not exist is an error, not a reason to fall back."""
    for given, name in ((arg, "--wayback-salvage"), ((env or {}).get("WAYBACK_SALVAGE"), "WAYBACK_SALVAGE")):
        if given:
            path = os.path.expanduser(given)
            if os.path.isfile(path):
                return path
            raise WaybackMissing("%s points at %s, which does not exist" % (name, given))
    candidates = [
        os.path.join(home, ".claude", "skills", "wayback-salvage", "scripts", "wayback_salvage.py"),
        os.path.join(os.path.dirname(repo), "wayback-salvage", "scripts", "wayback_salvage.py"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise WaybackMissing("wayback-salvage not found; tried %s. Install %s or later, or pass --wayback-salvage "
                         "PATH or set WAYBACK_SALVAGE." % (", ".join(candidates), MIN_WS_TEXT))


def parse_ws_version(text: str):
    m = re.search(r"wayback-salvage\s+(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


class WaybackSalvage:
    """wayback-salvage as a subprocess with --json. Calls are at least 6 s
    apart start to start, because each subprocess has its own limiter.
    Its exit 2 (request cap) and 3 (throttled or outage) become StopRun."""

    def __init__(self, path: str, out_dir: str, runner=None, clock=time.monotonic, sleep=asyncio.sleep,
                 gap: float = WS_GAP, python=None):
        self.path = path
        self.out_dir = out_dir
        self.runner = runner or self._subprocess
        self.python = python or shutil.which("python3") or sys.executable
        self.pacer = Pacer(gap, clock, sleep)
        self.version = None
        self.calls = 0

    async def _subprocess(self, argv):
        proc = await asyncio.create_subprocess_exec(self.python, self.path, *argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def check_version(self) -> str:
        try:
            code, out, _err = await self.runner(["--version"])
        except OSError as e:
            raise WaybackMissing("could not run wayback-salvage at %s: %s" % (self.path, e))
        version = parse_ws_version(out) if code == 0 else None
        if version is None or version < MIN_WS_VERSION:
            got = ".".join(map(str, version)) if version else (out.strip() or "no version (exit %s)" % code)
            raise WaybackMissing("wayback-salvage at %s reports %s; teardown-pairs needs %s or later"
                                 % (self.path, got, MIN_WS_TEXT))
        self.version = ".".join(map(str, version))
        return self.version

    async def _call(self, argv, out_dir=None):
        await self.pacer.wait()
        self.calls += 1
        code, out, err = await self.runner(list(argv) + ["-o", out_dir or self.out_dir, "--json", "--quiet"])
        lines = [ln for ln in (err or "").strip().splitlines() if ln.strip()]
        detail = lines[-1].strip() if lines else ""
        if code == 0:
            try:
                return json.loads(out)
            except ValueError:
                raise WaybackError("wayback-salvage %s printed no JSON" % argv[0])
        if code == 2:
            raise StopRun(EXIT_MAX_PAGES, "wayback-salvage reached its request cap (%s); rerun to resume" % detail)
        if code == 3:
            raise StopRun(EXIT_THROTTLED, "archive.org kept throttling or failing (wayback-salvage exit 3: %s); "
                                          "rerun later and do not lower --delay" % detail)
        raise WaybackError("wayback-salvage %s exited %s: %s" % (argv[0], code, detail or "no message"))

    async def snapshots(self, url, from_ts, to_ts, match=None, limit=None, collapse=None, status="200") -> list:
        argv = ["snapshots", url, "--from", from_ts, "--to", to_ts]
        if status:
            argv += ["--status", status]
        if match:
            argv += ["--match", match]
        if limit is not None:
            argv += ["--limit", str(limit)]
        if collapse:
            argv += ["--collapse", collapse]
        rows = await self._call(argv)
        if not isinstance(rows, list):
            raise WaybackError("wayback-salvage snapshots did not print a list")
        return rows

    async def fetch(self, url, ts, save) -> dict:
        return await self._call(["fetch", url, "-t", ts, "--save", save])

    async def page(self, url, ts) -> dict:
        return await self._call(["page", url, "-t", ts])

    async def images(self, url, ts, out_dir) -> dict:
        return await self._call(["images", url, "-t", ts], out_dir=out_dir)


# --------------------------------------------------------------- renderer


@dataclasses.dataclass
class RenderConfig:
    budget: int = 600
    concurrency: int = 4
    goto_timeout_ms: int = 90000
    idle_timeout_ms: int = 15000
    scroll_step: float = 0.8
    scroll_pause_ms: int = 300
    max_scroll: int = MAX_SHOT_HEIGHT
    settle_ms: int = 2000
    max_height: int = MAX_SHOT_HEIGHT
    request_timeout_ms: int = 30000
    consent_timeout_ms: int = 2000


CONSENT_SELECTORS = (
    ("OneTrust", "#onetrust-accept-btn-handler"),
    ("Cookiebot", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"),
    ("Cookiebot", "#CybotCookiebotDialogBodyButtonAccept"),
    ("TrustArc", "#truste-consent-button"),
)
CONSENT_TEXTS = ("Accept all", "Accept All Cookies", "Accept")
FREEZE_CSS = ("*, *::before, *::after { animation-duration: 0s !important; animation-delay: 0s !important; "
              "animation-iteration-count: 1 !important; transition-duration: 0s !important; "
              "transition-delay: 0s !important; caret-color: transparent !important; "
              "scroll-behavior: auto !important; }")
MEDIA_EXTS = (".mp4", ".webm", ".m3u8", ".mpd", ".m4s", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".mov")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_HOPS = 5
DROP_HEADERS = ("content-encoding", "content-length", "transfer-encoding", "connection")
IMAGES_READY_JS = ("() => Array.from(document.images).filter(i => i.offsetParent !== null)"
                   ".every(i => i.complete)")
PAGE_HEIGHT_JS = ("() => Math.max(document.documentElement ? document.documentElement.scrollHeight : 0, "
                  "document.body ? document.body.scrollHeight : 0)")


def _first_line(e) -> str:
    return (str(e).strip().splitlines() or [""])[0][:300]


# Error codes in Playwright's network errors (Node for route.fetch and
# APIRequestContext, Chromium for page loads), in plain words.
NETWORK_ERRORS = (
    ("ECONNREFUSED", "connection refused"), ("ERR_CONNECTION_REFUSED", "connection refused"),
    ("ECONNRESET", "connection reset"), ("ERR_CONNECTION_RESET", "connection reset"),
    ("socket hang up", "connection reset"), ("EPIPE", "connection reset"),
    ("ECONNABORTED", "connection aborted"), ("ERR_CONNECTION_CLOSED", "connection closed"),
    ("ETIMEDOUT", "timed out"), ("ERR_TIMED_OUT", "timed out"), ("Timeout ", "timed out"),
    ("ENOTFOUND", "DNS lookup failed"), ("EAI_AGAIN", "DNS lookup failed"), ("ERR_NAME_NOT_RESOLVED", "DNS lookup failed"),
    ("EHOSTUNREACH", "host unreachable"), ("ENETUNREACH", "network unreachable"),
)


def network_error_text(text) -> str:
    """Plain words for a network error ("connection reset"), else its first line."""
    for code, phrase in NETWORK_ERRORS:
        if code in (text or ""):
            return phrase
    return _first_line(text) or "no response"


def degraded_verdict(http_429: int, network_errors: int, main_error) -> tuple:
    """(degraded, reason) for an archive load. Throttling shows as 429s and
    as requests that get no HTTP response at all (refused, reset, timed
    out): more than DEGRADED_429S of them together, or a main document that
    got no response, mark the load degraded."""
    if main_error:
        return True, "main document got no HTTP response (%s)" % network_error_text(main_error)
    if http_429 + network_errors > DEGRADED_429S:
        return True, "%d HTTP 429s and %d requests with no HTTP response (more than %d together)" % (
            http_429, network_errors, DEGRADED_429S)
    return False, None


class _Tracker:
    """Request routing and accounting for one page load.

    Archive loads: archive.org requests are fetched by the handler under a
    semaphore (4 in flight) and a per-load budget, redirects followed by
    hand so every hop stays on archive.org; requests to any other host are
    aborted as leaked. Live loads pass through. Video and audio are aborted
    on every load. `resolve` maps a URL to the one actually fetched (the
    identity in production; tests point archive URLs at a local server)
    and returns None for URLs that must not be fetched at all."""

    def __init__(self, kind: str, label: str, cfg: RenderConfig, resolve, unrouted: list):
        self.kind = kind
        self.label = label
        self.cfg = cfg
        self.resolve = resolve
        self.unrouted = unrouted
        self.records = {}
        self.order = []
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.budget_used = 0
        self.http_429 = 0
        self.network_errors = 0  # archive fetches that got no HTTP status
        self.main_error = None  # the main document's, when it got none
        self.tasks = []
        self.main_status = None
        self.main_response = None
        self.page = None

    def _rec(self, request) -> dict:
        rec = self.records.get(request)
        if rec is None:
            url = request.url
            rec = {"load": self.label, "url": url[:2000], "host": host_of(url), "resource_type": request.resource_type,
                   "status": None, "bytes": 0, "aborted": None, "failure": None}
            self.records[request] = rec
            self.order.append(rec)
        return rec

    def _is_main(self, request) -> bool:
        try:
            return request.is_navigation_request() and self.page is not None and request.frame == self.page.main_frame
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_media(request) -> bool:
        if request.resource_type == "media":
            return True
        path = urllib.parse.urlsplit(request.url).path.lower()
        return path.endswith(MEDIA_EXTS)

    async def _abort(self, route, rec, reason):
        rec["aborted"] = reason
        try:
            await route.abort()
        except Exception:  # noqa: BLE001
            pass

    async def handle(self, route):
        request = route.request
        rec = self._rec(request)
        try:
            if self._is_media(request):
                return await self._abort(route, rec, "media")
            if self.kind == "archive":
                return await self._archive(route, request, rec)
            if self.resolve(request.url) is None:
                self.unrouted.append(request.url)
                return await self._abort(route, rec, "unrouted")
            await route.continue_()
        except Exception as e:  # noqa: BLE001  (page closed, request cancelled)
            rec["failure"] = rec["failure"] or _first_line(e)

    async def _archive(self, route, request, rec):
        url = request.url
        if not is_archive_host(host_of(url)):
            return await self._abort(route, rec, "leaked")
        if self.budget_used >= self.cfg.budget:
            return await self._abort(route, rec, "budget")
        self.budget_used += 1
        async with self.sem:
            current = url
            for hop in range(MAX_HOPS + 1):
                target = self.resolve(current)
                if target is None:
                    self.unrouted.append(current)
                    return await self._abort(route, rec, "unrouted")
                try:
                    resp = await route.fetch(url=target, max_redirects=0, timeout=self.cfg.request_timeout_ms)
                except Exception as e:  # noqa: BLE001  (refused, reset, timed out: no HTTP status)
                    rec["failure"] = _first_line(e)
                    self.network_errors += 1
                    if self._is_main(request):
                        self.main_error = rec["failure"]
                    try:
                        await route.abort("failed")
                    except Exception:  # noqa: BLE001
                        pass
                    return
                location = resp.headers.get("location")
                if resp.status in REDIRECT_STATUSES and location and hop < MAX_HOPS:
                    nxt = urllib.parse.urljoin(current, location.strip())
                    await resp.dispose()
                    if not is_archive_host(host_of(nxt)):
                        return await self._abort(route, rec, "leaked")
                    if self.budget_used >= self.cfg.budget:
                        return await self._abort(route, rec, "budget")
                    self.budget_used += 1
                    rec["hops"] = rec.get("hops", 0) + 1
                    current = nxt
                    continue
                break
            body = await resp.body()
            status = resp.status
            headers = {k: v for k, v in resp.headers.items() if k.lower() not in DROP_HEADERS}
            await resp.dispose()
        rec["status"] = status
        rec["bytes"] = len(body)
        if status == 429:
            self.http_429 += 1
        if self._is_main(request):
            self.main_status = status
        await route.fulfill(status=status, headers=headers, body=body)

    def on_response(self, response):
        rec = self._rec(response.request)
        if self._is_main(response.request):
            self.main_response = response
            if self.kind == "live":
                self.main_status = response.status
        if self.kind == "live" and rec["status"] is None:
            rec["status"] = response.status
            if response.status == 429:
                self.http_429 += 1

    def on_finished(self, request):
        if self.kind == "live":
            self.tasks.append(asyncio.ensure_future(self._sizes(request)))

    async def _sizes(self, request):
        try:
            sizes = await request.sizes()
            self._rec(request)["bytes"] = int(sizes.get("responseBodySize") or 0)
        except Exception:  # noqa: BLE001
            pass

    def on_failed(self, request):
        rec = self._rec(request)
        if rec["aborted"] is None and not rec["failure"]:
            rec["failure"] = (request.failure or "failed")[:300]

    async def settle(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def summary(self) -> dict:
        recs = self.order
        aborted = collections.Counter(r["aborted"] for r in recs if r["aborted"])
        failed = sum(1 for r in recs if r["aborted"] is None and (r["failure"] or (r["status"] or 0) >= 400))
        return {
            "requests": len(recs),
            "failed": failed,
            "aborted_budget": aborted.get("budget", 0),
            "leaked": aborted.get("leaked", 0),
            "aborted_media": aborted.get("media", 0),
            "unrouted": aborted.get("unrouted", 0),
            "http_429": self.http_429,
            "network_errors": self.network_errors,
            "bytes": sum(r["bytes"] or 0 for r in recs),
            "main_status": self.main_status,
            "main_error": self.main_error,
        }

    def write_log(self, side_dir: str) -> None:
        with open(os.path.join(side_dir, "requests.jsonl"), "a", encoding="utf-8") as fh:
            for rec in self.order:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Renderer:
    """One Chromium browser, one page load at a time."""

    def __init__(self, cfg: RenderConfig = None, resolve=None):
        self.cfg = cfg or RenderConfig()
        self.resolve = resolve or (lambda url: url)
        self.unrouted = []
        self.chromium_version = None
        self.playwright_version = None
        self._pw = None
        self.browser = None
        self._facts_js = None
        self._errors = None

    async def start(self):
        try:
            from playwright.async_api import Error, TimeoutError as PwTimeout, async_playwright
        except ImportError:
            raise BrowserMissing("Playwright is not installed for %s. Install: %s" % (sys.executable, INSTALL_HELP))
        self._errors = (Error, PwTimeout)
        try:
            from importlib.metadata import version

            self.playwright_version = version("playwright")
        except Exception:  # noqa: BLE001
            self.playwright_version = None
        with open(FACTS_JS_PATH, encoding="utf-8") as fh:
            self._facts_js = fh.read().strip()
        self._pw = await async_playwright().start()
        try:
            self.browser = await self._pw.chromium.launch()
        except Error as e:
            await self._pw.stop()
            self._pw = None
            raise BrowserMissing("Chromium did not launch (%s). Install: %s" % (_first_line(e), INSTALL_HELP))
        self.chromium_version = self.browser.version

    async def close(self):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass
            self.browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def _context(self, mobile: bool):
        if mobile:
            dev = dict(self._pw.devices[LIVE_DEVICE])
            dev.pop("default_browser_type", None)
            dev["viewport"] = dict(MOBILE)
            return await self.browser.new_context(**dev, service_workers="block")
        return await self.browser.new_context(viewport=dict(DESKTOP), service_workers="block")

    async def _page(self, context, tracker):
        page = await context.new_page()
        tracker.page = page
        await page.route("**/*", tracker.handle)
        page.on("response", tracker.on_response)
        page.on("requestfinished", tracker.on_finished)
        page.on("requestfailed", tracker.on_failed)
        return page

    async def _page_height(self, page) -> int:
        try:
            return int(await page.evaluate(PAGE_HEIGHT_JS))
        except Exception:  # noqa: BLE001
            return page.viewport_size["height"]

    async def _scroll(self, page):
        """Down in steps of 0.8 viewport with pauses, up to 20,000 px, then top."""
        vh = page.viewport_size["height"]
        step = max(1, int(vh * self.cfg.scroll_step))
        y = 0
        try:
            while True:
                limit = min(await self._page_height(page), self.cfg.max_scroll)
                if y + vh >= limit:
                    break
                y = min(y + step, limit)
                await page.evaluate("y => window.scrollTo(0, y)", y)
                await page.wait_for_timeout(self.cfg.scroll_pause_ms)
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass

    async def _consent(self, page) -> dict:
        """Try the constant consent list once, press Escape once, click nothing else."""
        record = {"clicked": None, "vendor": None, "escape": False}
        for vendor, sel in CONSENT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=self.cfg.consent_timeout_ms)
                    record.update(clicked=sel, vendor=vendor)
                    break
            except Exception as e:  # noqa: BLE001
                record["error"] = _first_line(e)
        if not record["clicked"]:
            for text in CONSENT_TEXTS:
                try:
                    loc = page.locator("button, [role=button]").filter(
                        has_text=re.compile(r"^\s*%s\s*$" % re.escape(text)))
                    for i in range(min(await loc.count(), 5)):
                        item = loc.nth(i)
                        if await item.is_visible():
                            await item.click(timeout=self.cfg.consent_timeout_ms)
                            record.update(clicked='button text "%s"' % text, vendor="button text")
                            break
                except Exception as e:  # noqa: BLE001
                    record["error"] = _first_line(e)
                if record["clicked"]:
                    break
        try:
            await page.keyboard.press("Escape")
            record["escape"] = True
        except Exception:  # noqa: BLE001
            pass
        return record

    async def _sequence(self, page, tracker, url) -> dict:
        """goto, network idle, consent, Escape, freeze CSS, scroll, top, settle, facts."""
        res = {"url": url, "final_url": None, "main_status": None, "load_timeout": False, "error": None,
               "consent": {"clicked": None, "vendor": None, "escape": False}, "facts": None, "user_agent": None,
               "main_body": None, "screenshots": {}}
        Error, PwTimeout = self._errors
        response = None
        try:
            response = await page.goto(url, wait_until="load", timeout=self.cfg.goto_timeout_ms)
        except PwTimeout:
            res["load_timeout"] = True
        except Error as e:
            res["error"] = _first_line(e)
        if res["error"] is None and (response is None and tracker.main_response is None and not res["load_timeout"]):
            res["error"] = "no document"
        if res["error"]:
            return res
        try:
            await page.wait_for_load_state("networkidle", timeout=self.cfg.idle_timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        res["consent"] = await self._consent(page)
        try:
            await page.add_style_tag(content=FREEZE_CSS)
        except Exception:  # noqa: BLE001  (a CSP may refuse it; screenshots still freeze animations)
            pass
        await self._scroll(page)
        await page.wait_for_timeout(self.cfg.settle_ms)
        try:
            res["facts"] = normalize_facts(await page.evaluate(self._facts_js, {"url": url}))
        except Exception as e:  # noqa: BLE001
            res["facts_error"] = _first_line(e)
        try:
            res["user_agent"] = await page.evaluate("() => navigator.userAgent")
        except Exception:  # noqa: BLE001
            pass
        res["final_url"] = page.url
        main = response or tracker.main_response
        if main is not None and tracker.main_status is None:
            tracker.main_status = main.status
        if tracker.kind == "live" and main is not None:
            try:
                res["main_body"] = await main.body()
            except Exception:  # noqa: BLE001
                pass
        return res

    async def _shots(self, page, side_dir: str, name: str) -> dict:
        vw, vh = page.viewport_size["width"], page.viewport_size["height"]
        height = await self._page_height(page)
        cap = min(max(height, vh), self.cfg.max_height)
        out = {}
        try:
            await page.screenshot(path=os.path.join(side_dir, name + ".png"), full_page=True,
                                  clip={"x": 0, "y": 0, "width": vw, "height": cap}, animations="disabled",
                                  caret="hide", scale="css", timeout=120000)
            out[name] = {"file": name + ".png", "width": vw, "height": cap, "page_height": height,
                         "truncated": height > self.cfg.max_height}
        except Exception as e:  # noqa: BLE001
            out[name] = {"file": None, "error": _first_line(e)}
        try:
            await page.screenshot(path=os.path.join(side_dir, name + "-fold.png"), animations="disabled",
                                  caret="hide", scale="css", timeout=60000)
            out[name + "-fold"] = {"file": name + "-fold.png", "width": vw, "height": vh, "page_height": height,
                                   "truncated": False}
        except Exception as e:  # noqa: BLE001
            out[name + "-fold"] = {"file": None, "error": _first_line(e)}
        return out

    async def _finish(self, page, tracker, side_dir, res):
        try:
            await page.unroute_all(behavior="ignoreErrors")
        except Exception:  # noqa: BLE001
            pass
        await tracker.settle()
        res["requests"] = tracker.summary()
        res["main_status"] = tracker.main_status
        res["degraded"], res["degraded_reason"] = degraded_verdict(
            tracker.http_429, tracker.network_errors, tracker.main_error) if tracker.kind == "archive" else (False, None)
        tracker.write_log(side_dir)

    async def load_archive(self, url: str, side_dir: str, viewports) -> dict:
        """One archive load: desktop screenshots, then the same page resized
        to 390x844 for mobile (the archived HTML is what a desktop crawler
        received, so this avoids a second archive load)."""
        tracker = _Tracker("archive", "archive", self.cfg, self.resolve, self.unrouted)
        context = await self._context(mobile=False)
        res = {}
        page = None
        try:
            page = await self._page(context, tracker)
            res = await self._sequence(page, tracker, url)
            if not res["error"]:
                if "desktop" in viewports:
                    res["screenshots"].update(await self._shots(page, side_dir, "desktop"))
                if "mobile" in viewports:
                    await page.set_viewport_size(dict(MOBILE))
                    await page.wait_for_timeout(self.cfg.settle_ms)
                    await self._scroll(page)
                    res["screenshots"].update(await self._shots(page, side_dir, "mobile"))
            res["mobile_basis"] = "desktop-capture-resized" if "mobile" in viewports else None
            res["viewport"] = "+".join(v for v in VIEWPORTS if v in viewports)
            res["kind"] = "archive"
        finally:
            if page is not None:
                await self._finish(page, tracker, side_dir, res)
            await context.close()
        return res

    async def load_live(self, url: str, side_dir: str, viewport: str) -> dict:
        """One live load in a desktop context, or Playwright's iPhone 13
        profile for mobile. User agents are whatever Playwright ships."""
        tracker = _Tracker("live", "live-" + viewport, self.cfg, self.resolve, self.unrouted)
        mobile = viewport == "mobile"
        context = await self._context(mobile=mobile)
        res = {}
        page = None
        try:
            page = await self._page(context, tracker)
            res = await self._sequence(page, tracker, url)
            if not res["error"]:
                res["screenshots"].update(await self._shots(page, side_dir, viewport))
            res["mobile_basis"] = "mobile-device-profile" if mobile else None
            res["viewport"] = viewport
            res["kind"] = "live"
        finally:
            if page is not None:
                await self._finish(page, tracker, side_dir, res)
            await context.close()
        return res

    async def fetch_robots(self, url: str) -> tuple:
        """(status, text, error): error is the first line of the exception
        when there was no response, else None."""
        context = await self.browser.new_context()
        try:
            resp = await context.request.get(url, timeout=20000, max_redirects=5)
            return resp.status, await resp.text(), None
        except Exception as e:  # noqa: BLE001
            return None, "", _first_line(e)
        finally:
            await context.close()

    async def screenshot_html(self, html_path: str, out_png: str, width: int, height: int, fragment: str = ""):
        """Screenshot a local HTML file (compare.html); nothing but file:// loads."""
        context = await self.browser.new_context(viewport={"width": width, "height": height})
        try:
            page = await context.new_page()

            async def guard(route):
                if route.request.url.startswith("file://"):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", guard)
            url = "file://" + urllib.parse.quote(os.path.abspath(html_path)) + ("#" + fragment if fragment else "")
            await page.goto(url, wait_until="load", timeout=60000)
            try:
                await page.wait_for_function(IMAGES_READY_JS, timeout=15000)
            except Exception:  # noqa: BLE001  (screenshot what is there)
                pass
            await page.wait_for_timeout(300)
            h = min(await self._page_height(page), self.cfg.max_height)
            await page.screenshot(path=out_png, full_page=True, clip={"x": 0, "y": 0, "width": width, "height": h},
                                  scale="css", animations="disabled", timeout=120000)
        finally:
            await context.close()


# ------------------------------------------------------------------- diff

SCALAR_FIELDS = (("title", "Title"), ("description", "Description"), ("canonical", "Canonical"),
                 ("robots", "Meta robots"), ("lang", "Lang"), ("og_title", "og:title"), ("og_image", "og:image"))
LIST_FIELDS = (("h1", "H1"), ("h2", "H2 (first 30)"), ("nav", "Navigation"), ("ctas", "CTAs"),
               ("above_fold_text", "Above-the-fold text"), ("above_fold_buttons", "Above-the-fold buttons"),
               ("promos", "Promo candidates"))
COUNT_FIELDS = (("images", "Images"), ("links", "Links"), ("forms", "Forms"), ("iframes", "Iframes"),
                ("price_like", "Price-like elements"), ("above_fold_images", "Images above the fold"),
                ("text_chars", "Visible text characters"), ("structured_data_blocks", "ld+json blocks"))
PRODUCT_FIELDS = ("name", "brand", "sku", "price", "currency", "availability", "rating", "review_count")
_SCHEMA_PREFIX_RE = re.compile(r"^https?://schema\.org/", re.I)
HIGH_FAILED = 20


def _get(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _norm(value) -> str:
    return " ".join(str(value).split()).casefold()


def _scalar(a, b) -> dict:
    if isinstance(a, str) or isinstance(b, str):
        changed = _norm(a if a is not None else "") != _norm(b if b is not None else "") or (a is None) != (b is None)
    else:
        changed = a != b
    return {"then": a, "now": b, "changed": changed}


def _count(a, b) -> dict:
    delta = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    return {"then": a, "now": b, "delta": delta}


def _dedupe(items) -> list:
    out, seen = [], set()
    for x in items or []:
        if x is None or not str(x).strip():
            continue
        k = _norm(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _list_diff(then_items, now_items) -> dict:
    t, n = _dedupe(then_items), _dedupe(now_items)
    tk, nk = [_norm(x) for x in t], [_norm(x) for x in n]
    tset, nset = set(tk), set(nk)
    return {
        "added": [x for x, k in zip(n, nk) if k not in tset],
        "removed": [x for x, k in zip(t, tk) if k not in nset],
        "kept": [x for x, k in zip(t, tk) if k in nset],
        "order_changed": [k for k in tk if k in nset] != [k for k in nk if k in tset],
    }


def _lists(f) -> dict:
    return {
        "h1": _get(f, "headings", "h1") or [],
        "h2": _get(f, "headings", "h2") or [],
        "nav": [x.get("text") for x in (f.get("nav") or []) if isinstance(x, dict)],
        "ctas": f.get("ctas") or [],
        "above_fold_text": [b.get("text") for b in (_get(f, "above_fold", "text_blocks") or []) if isinstance(b, dict)],
        "above_fold_buttons": _get(f, "above_fold", "buttons") or [],
        "promos": f.get("promos") or [],
    }


def _counts(f) -> dict:
    c = f.get("counts") or {}
    return {
        "images": c.get("images"), "links": c.get("links"), "forms": c.get("forms"), "iframes": c.get("iframes"),
        "price_like": c.get("price_like"), "above_fold_images": _get(f, "above_fold", "image_count"),
        "text_chars": _get(f, "signals", "text_chars"), "structured_data_blocks": _get(f, "structured_data", "blocks"),
    }


def _product_value(prod, field):
    v = (prod or {}).get(field)
    if field == "availability" and isinstance(v, str):
        v = _SCHEMA_PREFIX_RE.sub("", v)
    return v


def diff_facts(then, now) -> dict:
    t, n = then or {}, now or {}
    tl, nl = _lists(t), _lists(n)
    tc, nc = _counts(t), _counts(n)
    ts, ns = t.get("structured_data") or {}, n.get("structured_data") or {}
    ttypes, ntypes = ts.get("types") or [], ns.get("types") or []
    tprod, nprod = ts.get("product"), ns.get("product")
    product = None
    if tprod or nprod:
        product = {f: _scalar(_product_value(tprod, f), _product_value(nprod, f)) for f in PRODUCT_FIELDS}
    tcrumbs, ncrumbs = ts.get("breadcrumbs") or [], ns.get("breadcrumbs") or []
    tm, nm = t.get("martech") or {}, n.get("martech") or {}
    tload = [h for h, _ in (tm.get("loaded") or [])]
    nload = [h for h, _ in (nm.get("loaded") or [])]
    tpdp, npdp = t.get("pdp_heuristic") or {}, n.get("pdp_heuristic") or {}
    tplp, nplp = t.get("plp_heuristic") or {}, n.get("plp_heuristic") or {}
    return {
        "scalars": {k: _scalar(_get(t, "page", k), _get(n, "page", k)) for k, _ in SCALAR_FIELDS},
        "lists": {k: _list_diff(tl[k], nl[k]) for k, _ in LIST_FIELDS},
        "structured_data": {
            "types_added": [x for x in ntypes if x not in ttypes],
            "types_removed": [x for x in ttypes if x not in ntypes],
            "product": product,
            "breadcrumbs": {"then": tcrumbs, "now": ncrumbs, "changed": tcrumbs != ncrumbs},
            "item_list_count": _scalar(_get(ts, "item_list", "count"), _get(ns, "item_list", "count")),
            "errors": {"then": len(ts.get("errors") or []), "now": len(ns.get("errors") or [])},
        },
        "martech": {
            "added": group_hosts([h for h in nload if h not in tload]),
            "removed": group_hosts([h for h in tload if h not in nload]),
            "kept": sorted(set(tload) & set(nload)),
            "mentioned_only": {"then": sorted(h for h, _ in (tm.get("mentioned") or [])),
                               "now": sorted(h for h, _ in (nm.get("mentioned") or []))},
            "sources": {"then": tm.get("source"), "now": nm.get("source")},
        },
        "counts": {k: _count(tc[k], nc[k]) for k, _ in COUNT_FIELDS},
        "heuristics": {
            "label": "heuristic",
            "pdp": {
                "first_price_after_h1": _scalar(tpdp.get("first_price_after_h1"), npdp.get("first_price_after_h1")),
                "add_to_cart": _scalar(tpdp.get("add_to_cart"), npdp.get("add_to_cart")),
                "review_count_text": _scalar(tpdp.get("review_count_text"), npdp.get("review_count_text")),
            },
            "plp": {
                "price_like_in_main": _count(tplp.get("price_like_in_main"), nplp.get("price_like_in_main")),
                "filter_elements": _count(tplp.get("filter_elements"), nplp.get("filter_elements")),
                "filter_labels": _list_diff(tplp.get("filter_labels"), nplp.get("filter_labels")),
            },
        },
    }


def _flags(cap: dict) -> dict:
    f = cap.get("flags") or {}
    return {"suspect": f.get("suspect"), "blocked": bool(f.get("blocked")), "degraded": bool(f.get("degraded")),
            "truncated": bool(f.get("truncated"))}


def _req(cap: dict) -> dict:
    r = cap.get("requests") or {}
    return {"failed": r.get("failed", 0), "leaked": r.get("leaked", 0)}


def _side_shots(side: str, cap: dict) -> dict:
    out = {}
    for name in SHOT_NAMES:
        f = (cap.get("screenshots") or {}).get(name)
        if f:
            out[name] = "%s/%s" % (side, f)
    return out


def days_before(earlier, later):
    """Whole days from one YYYY-MM-DD date to a later one, or None."""
    try:
        return (datetime.date.fromisoformat(later) - datetime.date.fromisoformat(earlier)).days
    except (TypeError, ValueError):
        return None


def build_comparison(pack: dict, then: dict, now: dict, now_side: str, source: str) -> dict:
    tc, nc = then["capture"], now["capture"]
    row = pack.get("row") or {}
    tcap, ncap = tc.get("capture") or {}, nc.get("capture") or {}
    distance = tcap.get("distance_days")
    # now-archive is the newest capture up to --date, which can be weeks old
    now_distance = days_before(ncap.get("date"), pack.get("date")) if source == "archive" else None
    header = {
        "brand": row.get("brand"), "page_type": row.get("page_type"), "label": row.get("label"),
        "target_date": pack.get("target_date"), "then_date": tcap.get("date"), "then_timestamp": tcap.get("timestamp"),
        "now_date": ncap.get("date"), "now_timestamp": ncap.get("timestamp"), "distance_days": distance,
        "now_distance_days": now_distance, "date": pack.get("date"), "now_source": source,
        "mobile_basis": {"then": tc.get("mobile_basis"), "now": nc.get("mobile_basis")},
        "flags": {"then": _flags(tc), "now": _flags(nc)},
        "requests": {"then": _req(tc), "now": _req(nc)},
    }
    asym, warn = [], []
    asym.append("Then is the Wayback Machine crawler's view: logged out, no cookies, mostly a desktop user agent, "
                "one A/B variant.")
    if source == "live":
        asym.append("Now is a live load made today from this machine with Playwright's stock browser; then is an "
                    "archive replay, so location, cookies and A/B bucket can differ.")
    else:
        asym.append("Now-archive is the same crawler view, so this comparison is like for like.")
    mb_then, mb_now = tc.get("mobile_basis"), nc.get("mobile_basis")
    if mb_then == "desktop-capture-resized" and mb_now == "mobile-device-profile":
        asym.append("Then is the archive crawler's desktop view resized to mobile width; now is a live mobile "
                    "device profile.")
    elif mb_then == "desktop-capture-resized" and mb_now == "desktop-capture-resized":
        asym.append("Both mobile screenshots are the archive crawler's desktop view resized to mobile width, not "
                    "what a phone received.")
    tr = tc.get("requests") or {}
    total = tr.get("requests") or 0
    failed = tr.get("failed") or 0
    if failed >= HIGH_FAILED or (total and failed / total >= 0.2):
        asym.append("Then had %d failed subrequests out of %d; holes in its screenshots are most likely missing "
                    "archive assets, not design choices." % (failed, total))
    if tr.get("leaked"):
        asym.append("%d requests from the then replay tried to reach live hosts and were blocked, so anything they "
                    "would have drawn is missing." % tr["leaked"])
    if (now.get("capture") or {}).get("kind") == "archive":
        nr = nc.get("requests") or {}
        if nr.get("leaked"):
            asym.append("%d requests from the now-archive replay tried to reach live hosts and were blocked."
                        % nr["leaked"])
    if distance is not None and distance > WARN_DISTANCE_DAYS:
        warn.append("Then capture is %s from the target date (%s vs %s); retail merchandising changes weekly "
                    "and seasonally." % (days(distance), tcap.get("date"), pack.get("target_date")))
    if now_distance is not None and now_distance > WARN_DISTANCE_DAYS:
        warn.append("Now-archive capture is %s before --date (%s vs %s); retail merchandising changes weekly and "
                    "seasonally, so it may not be the page as it is now." % (days(now_distance), ncap.get("date"),
                                                                              pack.get("date")))
    for name, cap in (("Then", tc), ("Now-" + source, nc)):
        f = _flags(cap)
        if f["blocked"]:
            sig = f["suspect"] or {}
            warn.append("%s was blocked (%s: %s); its screenshots show the block page. Use the now-archive comparison."
                        % (name, sig.get("signature"), sig.get("detail")))
        elif f["suspect"]:
            warn.append("%s is flagged suspect (%s: %s)." % (name, f["suspect"].get("signature"),
                                                             f["suspect"].get("detail")))
        if f["degraded"]:
            warn.append("%s load was degraded by archive throttling (more than %d HTTP 429s and requests with no "
                        "response, or a main document that got none)." % (name, DEGRADED_429S))
        if f["truncated"]:
            warn.append("%s full-page screenshot is truncated at %s px." % (name, format(MAX_SHOT_HEIGHT, ",")))
    return {
        "name": "then-vs-" + now_side,
        "now_side": now_side,
        "header": header,
        "asymmetries": asym,
        "warnings": warn,
        "facts": diff_facts(then.get("facts"), now.get("facts")),
        "screenshots": {"then": _side_shots("then", tc), "now": _side_shots(now_side, nc)},
    }


def build_diff(pack: dict, sides: dict) -> dict:
    """diff.json: one comparison per now side that has facts on both ends."""
    comps = []
    then = sides.get("then")
    for now_side, source in (("now-live", "live"), ("now-archive", "archive")):
        now = sides.get(now_side)
        if then and now and then.get("facts") and now.get("facts") and then.get("capture") and now.get("capture"):
            comps.append(build_comparison(pack, then, now, now_side, source))
    row = pack.get("row") or {}
    return {
        "tool": "teardown-pairs",
        "version": VERSION,
        "pack": {"brand": row.get("brand"), "page_type": row.get("page_type"), "label": row.get("label"),
                 "target_date": pack.get("target_date"), "date": pack.get("date"), "kind": pack.get("kind")},
        "sides": {s: (pack.get("sides") or {}).get(s, {}).get("status") for s in SIDES
                  if s in (pack.get("sides") or {})},
        "comparisons": comps,
    }


def _md(value) -> str:
    if value is None or value == "":
        return "(none)"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\u2014", "-")


def render_diff_md(diff: dict) -> str:
    p = diff.get("pack") or {}
    lines = ["# Teardown diff: %s / %s / %s" % (p.get("brand"), p.get("page_type"), p.get("label")), ""]
    lines.append("Target date %s, captured for %s. Side status: %s." % (
        p.get("target_date"), p.get("date"), ", ".join("%s %s" % kv for kv in (diff.get("sides") or {}).items())))
    lines.append("")
    if not diff.get("comparisons"):
        lines.append("No comparison yet: a then side and at least one now side need facts. See pack.json for "
                     "each side's status and reason.")
        lines.append("")
    for c in diff.get("comparisons") or []:
        h = c["header"]
        lines += ["## %s" % c["name"], ""]
        lines.append("| | Then | Now (%s) |" % h["now_source"])
        lines.append("|---|---|---|")
        lines.append("| Date | %s (%s) | %s |" % (_md(h["then_date"]), _md(h["then_timestamp"]), _md(h["now_date"])))
        lines.append("| Mobile basis | %s | %s |" % (_md(h["mobile_basis"]["then"]), _md(h["mobile_basis"]["now"])))
        for flag in ("suspect", "blocked", "degraded", "truncated"):
            tv, nv = h["flags"]["then"][flag], h["flags"]["now"][flag]
            if isinstance(tv, dict):
                tv = tv.get("signature")
            if isinstance(nv, dict):
                nv = nv.get("signature")
            lines.append("| %s | %s | %s |" % (flag.capitalize(), _md(tv or "no"), _md(nv or "no")))
        lines.append("| Failed requests | %s | %s |" % (h["requests"]["then"]["failed"], h["requests"]["now"]["failed"]))
        lines.append("| Leaked requests (blocked) | %s | %s |" % (h["requests"]["then"]["leaked"],
                                                                 h["requests"]["now"]["leaked"]))
        lines.append("")
        dist = h.get("distance_days")
        line = "Target %s. Distance: %s." % (h["target_date"], days(dist) if dist is not None else "unknown")
        if h.get("now_distance_days") is not None:
            line += " Now-archive capture %s: %s before --date %s." % (h["now_date"], days(h["now_distance_days"]),
                                                                       h.get("date"))
        lines.append(line)
        lines.append("")
        lines += ["### Asymmetries", ""] + ["- %s" % s for s in c["asymmetries"]] + [""]
        if c["warnings"]:
            lines += ["### Warnings", ""] + ["- %s" % s for s in c["warnings"]] + [""]
        f = c["facts"]
        lines += ["### Page", "", "| Field | Then | Now | Changed |", "|---|---|---|---|"]
        for key, label in SCALAR_FIELDS:
            s = f["scalars"][key]
            lines.append("| %s | %s | %s | %s |" % (label, _md(s["then"]), _md(s["now"]), "changed" if s["changed"]
                                                   else "same"))
        lines += ["", "### Lists", ""]
        for key, label in LIST_FIELDS:
            d = f["lists"][key]
            lines.append("- **%s**: %d added, %d removed, %d kept%s" % (
                label, len(d["added"]), len(d["removed"]), len(d["kept"]), ", order changed" if d["order_changed"]
                else ""))
            if d["added"]:
                lines.append("  - added: %s" % "; ".join(_md(x) for x in d["added"]))
            if d["removed"]:
                lines.append("  - removed: %s" % "; ".join(_md(x) for x in d["removed"]))
        sd = f["structured_data"]
        lines += ["", "### Structured data", ""]
        lines.append("- Types added: %s" % (", ".join(sd["types_added"]) or "none"))
        lines.append("- Types removed: %s" % (", ".join(sd["types_removed"]) or "none"))
        if sd["product"]:
            for field in ("price", "currency", "availability", "rating", "review_count"):
                s = sd["product"][field]
                lines.append("- Product %s: %s -> %s%s" % (field.replace("_", " "), _md(s["then"]), _md(s["now"]),
                                                          " (changed)" if s["changed"] else ""))
        if sd["item_list_count"]["then"] is not None or sd["item_list_count"]["now"] is not None:
            lines.append("- ItemList count: %s -> %s" % (_md(sd["item_list_count"]["then"]),
                                                         _md(sd["item_list_count"]["now"])))
        if sd["breadcrumbs"]["changed"]:
            lines.append("- Breadcrumbs: %s -> %s" % (" > ".join(sd["breadcrumbs"]["then"]) or "(none)",
                                                      " > ".join(sd["breadcrumbs"]["now"]) or "(none)"))
        m = f["martech"]
        lines += ["", "### Martech (loaded script hosts)", ""]
        lines.append("Then from %s; now from %s. Hosts only mentioned in inline scripts are listed apart and never "
                     "counted." % (m["sources"]["then"] or "unknown", m["sources"]["now"] or "unknown"))
        for verb in ("added", "removed"):
            if m[verb]:
                for cat, hosts in m[verb].items():
                    lines.append("- %s, %s: %s" % (verb.capitalize(), cat, ", ".join(hosts)))
            else:
                lines.append("- %s: none" % verb.capitalize())
        lines.append("- Kept: %s" % (", ".join(m["kept"]) or "none"))
        lines += ["", "### Counts", "", "| Count | Then | Now | Delta |", "|---|---|---|---|"]
        for key, label in COUNT_FIELDS:
            s = f["counts"][key]
            delta = s["delta"]
            lines.append("| %s | %s | %s | %s |" % (label, _md(s["then"]), _md(s["now"]),
                                                  ("%+d" % delta) if isinstance(delta, int) else _md(delta)))
        hp, hl = f["heuristics"]["pdp"], f["heuristics"]["plp"]
        lines += ["", "### Heuristics (labelled heuristic: pattern matches, not ground truth)", ""]
        lines.append("- PDP first price after H1: %s -> %s" % (_md(hp["first_price_after_h1"]["then"]),
                                                               _md(hp["first_price_after_h1"]["now"])))
        lines.append("- PDP add to cart: %s -> %s" % (_md(hp["add_to_cart"]["then"]), _md(hp["add_to_cart"]["now"])))
        lines.append("- PDP review count text: %s -> %s" % (_md(hp["review_count_text"]["then"]),
                                                            _md(hp["review_count_text"]["now"])))
        lines.append("- PLP price-like elements in main: %s -> %s" % (_md(hl["price_like_in_main"]["then"]),
                                                                      _md(hl["price_like_in_main"]["now"])))
        lines.append("- PLP filter/facet elements: %s -> %s" % (_md(hl["filter_elements"]["then"]),
                                                                _md(hl["filter_elements"]["now"])))
        lines += ["", "### Screenshots", ""]
        for side in ("then", "now"):
            shots = c["screenshots"][side]
            if shots:
                lines.append("- %s: %s" % (side.capitalize(), ", ".join("[%s](%s)" % (k, v) for k, v in shots.items())))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------- compare page

COMPARE_CSS = """
:root { --bg: #f6f5f2; --fg: #1d1d1b; --muted: #5d5b55; --line: #d9d6cf; --card: #fff; --accent: #1f4e79;
  --add: #1d6b3a; --del: #9b2c2c; --warn: #8a5a00; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 14px/1.45 -apple-system, "Helvetica Neue",
  Arial, sans-serif; }
header.top { padding: 18px 24px 10px; border-bottom: 1px solid var(--line); background: var(--card); }
h1 { margin: 0 0 4px; font-size: 22px; } h1 span { color: var(--muted); font-weight: normal; }
h2 { font-size: 17px; margin: 22px 0 8px; } h3 { font-size: 15px; margin: 16px 0 6px; }
.meta { color: var(--muted); margin: 0 0 10px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.seg button { border: 0; background: var(--card); padding: 6px 12px; font: inherit; cursor: pointer; color: var(--fg); }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent); color: #fff; }
main { padding: 12px 24px 40px; }
.hidden { display: none !important; }
.flags { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.flag { border: 1px solid var(--line); background: var(--card); border-radius: 12px; padding: 2px 10px; font-size: 12px; }
.flag.bad { border-color: var(--del); color: var(--del); } .flag.warn { border-color: var(--warn); color: var(--warn); }
ul.notes { margin: 6px 0 10px; padding-left: 20px; } ul.notes.warn li { color: var(--warn); }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
.pair.single { grid-template-columns: 1fr; }
figure { margin: 0; background: var(--card); border: 1px solid var(--line); padding: 8px; }
figcaption { font-weight: 600; margin-bottom: 6px; } figcaption small { font-weight: normal; color: var(--muted); }
figure img { display: block; width: 100%; height: auto; border: 1px solid var(--line); }
.mobile figure img { max-width: 390px; margin: 0 auto; }
.missing { padding: 40px 12px; text-align: center; color: var(--muted); border: 1px dashed var(--line); }
table { border-collapse: collapse; width: 100%; background: var(--card); margin: 4px 0 10px; }
th, td { border: 1px solid var(--line); padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eeece6; }
td.changed { color: var(--accent); font-weight: 600; }
.add { color: var(--add); } .del { color: var(--del); }
.facts { margin-top: 18px; }
"""

COMPARE_JS = """
(function () {
  var params = new URLSearchParams((location.hash || '').replace(/^#/, ''));
  var state = {now: params.get('now'), view: params.get('view') || 'desktop', shot: params.get('shot') || 'fold',
               table: params.get('table') !== '0'};
  var sections = Array.prototype.slice.call(document.querySelectorAll('section.cmp'));
  if (!state.now || !sections.some(function (s) { return s.dataset.now === state.now; })) {
    state.now = sections.length ? sections[0].dataset.now : '';
  }
  function apply() {
    sections.forEach(function (s) { s.classList.toggle('hidden', s.dataset.now !== state.now); });
    document.querySelectorAll('.pair').forEach(function (p) {
      p.classList.toggle('hidden', p.dataset.view !== state.view || p.dataset.shot !== state.shot);
    });
    document.querySelectorAll('.facts').forEach(function (f) { f.classList.toggle('hidden', !state.table); });
    document.querySelectorAll('.seg').forEach(function (seg) {
      seg.querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-pressed', String(state[seg.dataset.key] === b.dataset.val));
      });
    });
  }
  document.querySelectorAll('.seg button').forEach(function (b) {
    b.addEventListener('click', function () { state[b.parentNode.dataset.key] = b.dataset.val; apply(); });
  });
  apply();
})();
"""


def _esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _figure(pack_path, side, side_state, cap, name, caption) -> str:
    file = ((cap or {}).get("screenshots") or {}).get(name)
    if file and os.path.exists(os.path.join(pack_path, side, file)):
        # full-page shots can be 20,000 px tall, so only they load lazily
        lazy = ' loading="lazy"' if not name.endswith("-fold") else ""
        img = '<img%s src="%s/%s" alt="%s %s screenshot">' % (lazy, _esc(side), _esc(file), _esc(side), _esc(name))
    else:
        status = (side_state or {}).get("status", "not captured")
        reason = (side_state or {}).get("reason") or ""
        img = '<div class="missing">No %s screenshot (%s%s)</div>' % (_esc(name), _esc(status),
                                                                       ": " + _esc(reason) if reason else "")
    return "<figure><figcaption>%s</figcaption>%s</figure>" % (caption, img)


def _side_caption(label, side_state, cap) -> str:
    c = (cap or {}).get("capture") or {}
    bits = []
    if c.get("date"):
        bits.append(c["date"])
    if c.get("timestamp") and (cap or {}).get("kind") == "archive":
        bits.append("archive %s" % c["timestamp"])
    if c.get("distance_days") is not None and label == "Then":
        bits.append("%s from target" % days(c["distance_days"]))
    if not bits:
        bits.append((side_state or {}).get("status", "not captured"))
    return "%s <small>%s</small>" % (_esc(label), _esc(", ".join(bits)))


def _facts_html(c) -> str:
    f = c["facts"]
    out = ['<div class="facts"><h3>Facts diff: %s</h3>' % _esc(c["name"])]
    out.append("<table><tr><th>Field</th><th>Then</th><th>Now</th></tr>")
    for key, label in SCALAR_FIELDS:
        s = f["scalars"][key]
        cls = ' class="changed"' if s["changed"] else ""
        out.append("<tr><td>%s</td><td>%s</td><td%s>%s</td></tr>" % (_esc(label), _esc(s["then"]), cls, _esc(s["now"])))
    out.append("</table><table><tr><th>List</th><th>Added</th><th>Removed</th><th>Kept</th></tr>")
    for key, label in LIST_FIELDS:
        d = f["lists"][key]
        out.append("<tr><td>%s%s</td><td class=\"add\">%s</td><td class=\"del\">%s</td><td>%d</td></tr>" % (
            _esc(label), " (order changed)" if d["order_changed"] else "",
            "<br>".join(_esc(x) for x in d["added"]), "<br>".join(_esc(x) for x in d["removed"]), len(d["kept"])))
    out.append("</table>")
    sd = f["structured_data"]
    out.append("<table><tr><th>Structured data</th><th>Then</th><th>Now</th></tr>")
    out.append("<tr><td>Types</td><td class=\"del\">%s</td><td class=\"add\">%s</td></tr>" % (
        _esc(", ".join(sd["types_removed"]) or "no types removed"), _esc(", ".join(sd["types_added"]) or "no types added")))
    if sd["product"]:
        for field in ("price", "currency", "availability", "rating", "review_count"):
            s = sd["product"][field]
            cls = ' class="changed"' if s["changed"] else ""
            out.append("<tr><td>Product %s</td><td>%s</td><td%s>%s</td></tr>" % (
                _esc(field.replace("_", " ")), _esc(s["then"]), cls, _esc(s["now"])))
    out.append("<tr><td>ItemList count</td><td>%s</td><td>%s</td></tr>" % (_esc(sd["item_list_count"]["then"]),
                                                                        _esc(sd["item_list_count"]["now"])))
    out.append("</table>")
    m = f["martech"]
    out.append("<table><tr><th>Martech</th><th>Removed</th><th>Added</th></tr>")
    for cat in VENDOR_CATEGORIES:
        rem, add = m["removed"].get(cat, []), m["added"].get(cat, [])
        if rem or add:
            out.append("<tr><td>%s</td><td class=\"del\">%s</td><td class=\"add\">%s</td></tr>" % (
                _esc(cat), "<br>".join(_esc(h) for h in rem), "<br>".join(_esc(h) for h in add)))
    out.append("<tr><td>Kept</td><td colspan=\"2\">%s</td></tr></table>" % _esc(", ".join(m["kept"]) or "none"))
    out.append("<table><tr><th>Count</th><th>Then</th><th>Now</th><th>Delta</th></tr>")
    for key, label in COUNT_FIELDS:
        s = f["counts"][key]
        delta = s["delta"]
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            _esc(label), _esc(s["then"]), _esc(s["now"]), ("%+d" % delta) if isinstance(delta, int) else ""))
    out.append("</table></div>")
    return "".join(out)


def render_compare_html(pack_path: str, pack: dict, diff: dict, sides: dict) -> str:
    row = pack.get("row") or {}
    states = pack.get("sides") or {}
    comps = {c["now_side"]: c for c in diff.get("comparisons") or []}
    now_sides = [s for s in ("now-live", "now-archive") if s in states]
    has_then = "then" in states
    title = "%s / %s / %s" % (row.get("brand"), row.get("page_type"), row.get("label"))
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             "<title>%s: %s vs %s</title>" % (_esc(title), _esc(pack.get("target_date")), _esc(pack.get("date"))),
             "<style>%s</style></head><body>" % COMPARE_CSS,
             '<header class="top"><h1>%s <span>%s / %s</span></h1>' % (_esc(row.get("brand")),
                                                                      _esc(row.get("page_type")), _esc(row.get("label")))]
    then_cap = (sides.get("then") or {}).get("capture")
    meta = ["Captured for %s." % _esc(pack.get("date"))]
    if has_then:
        meta.insert(0, "Target %s (%s)." % (_esc(pack.get("target_date")),
                                            _esc(weekday(datetime.date.fromisoformat(pack["target_date"])))))
    parts.append('<p class="meta">%s %s</p>' % (" ".join(meta), _esc(CRAWLER_VIEW[0].upper() + CRAWLER_VIEW[1:] + ".")))
    parts.append('<div class="controls">')
    if len(now_sides) > 1:
        parts.append('<div class="seg" data-key="now">%s</div>' % "".join(
            '<button type="button" data-val="%s">Now: %s</button>' % (s, s[4:]) for s in now_sides))
    parts.append('<div class="seg" data-key="view"><button type="button" data-val="desktop">Desktop 1440</button>'
                 '<button type="button" data-val="mobile">Mobile 390</button></div>')
    parts.append('<div class="seg" data-key="shot"><button type="button" data-val="fold">Above the fold</button>'
                 '<button type="button" data-val="full">Full page</button></div></div></header><main>')
    sections = now_sides or (["then"] if has_then else [])
    for now_side in sections:
        comp = comps.get(now_side)
        now_cap = (sides.get(now_side) or {}).get("capture") if now_side != "then" else None
        parts.append('<section class="cmp" data-now="%s">' % _esc(now_side))
        flags = []
        for label, side in (("then", "then"), (now_side, now_side)):
            if side not in states or (label == now_side and now_side == "then"):
                continue
            st = states[side]
            cls = "bad" if st.get("status") in ("failed", "blocked", "suspect") else ""
            flags.append('<span class="flag %s">%s: %s</span>' % (cls, _esc(side), _esc(st.get("status"))))
            cap = (sides.get(side) or {}).get("capture") or {}
            r = cap.get("requests") or {}
            if r:
                flags.append('<span class="flag%s">%s failed %s, leaked %s</span>' % (
                    " warn" if r.get("failed") else "", _esc(side), _esc(r.get("failed")), _esc(r.get("leaked"))))
            if (cap.get("flags") or {}).get("truncated"):
                flags.append('<span class="flag warn">%s truncated at 20,000 px</span>' % _esc(side))
        parts.append('<div class="flags">%s</div>' % "".join(flags))
        if comp:
            parts.append('<ul class="notes">%s</ul>' % "".join("<li>%s</li>" % _esc(s) for s in comp["asymmetries"]))
            if comp["warnings"]:
                parts.append('<ul class="notes warn">%s</ul>' % "".join("<li>%s</li>" % _esc(s)
                                                                       for s in comp["warnings"]))
        for view in VIEWPORTS:
            for shot in ("fold", "full"):
                name = view + ("-fold" if shot == "fold" else "")
                figs = []
                if has_then:
                    figs.append(_figure(pack_path, "then", states.get("then"), then_cap, name,
                                        _side_caption("Then", states.get("then"), then_cap)))
                if now_side != "then":
                    figs.append(_figure(pack_path, now_side, states.get(now_side), now_cap, name,
                                        _side_caption("Now (%s)" % now_side[4:], states.get(now_side), now_cap)))
                parts.append('<div class="pair %s%s" data-view="%s" data-shot="%s">%s</div>' % (
                    view, "" if len(figs) > 1 else " single", view, shot, "".join(figs)))
        if comp:
            parts.append(_facts_html(comp))
        elif has_then and now_side != "then":
            parts.append('<div class="facts"><p>No facts diff yet: both sides need facts. Then is %s, %s is %s.</p>'
                         '</div>' % (_esc(states["then"].get("status")), _esc(now_side),
                                     _esc(states[now_side].get("status"))))
        parts.append("</section>")
    parts.append("</main><script>%s</script></body></html>" % COMPARE_JS)
    return "\n".join(parts) + "\n"


def render_index_html(out_dir: str, packs: list) -> str:
    rows = []
    for path, pack in packs:
        row = pack.get("row") or {}
        states = pack.get("sides") or {}
        rel = os.path.relpath(path, out_dir)
        thumbs = []
        for side in ("then", "now-live", "now-archive"):
            f = os.path.join(path, side, "desktop-fold.png")
            if os.path.exists(f):
                thumbs.append('<figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>' % (
                    _esc(os.path.relpath(f, out_dir)), _esc(side), _esc(side)))
        then_cap = (states.get("then") or {}).get("capture") or {}
        flags = []
        for side, st in states.items():
            for key in ("signature", "reason"):
                if st.get(key) and st.get("status") != "done":
                    flags.append("%s: %s" % (side, st[key]))
            cap = read_json(os.path.join(path, side, "capture.json")) or {}
            r = cap.get("requests") or {}
            if r.get("failed") or r.get("leaked"):
                flags.append("%s failed %s, leaked %s" % (side, r.get("failed", 0), r.get("leaked", 0)))
            if (cap.get("flags") or {}).get("truncated"):
                flags.append("%s truncated" % side)
        links = []
        for name in ("compare.html", "diff.md", "pack.json"):
            if os.path.exists(os.path.join(path, name)):
                links.append('<a href="%s/%s">%s</a>' % (_esc(rel), name, name))
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "<td class=\"thumbs\">%s</td><td>%s</td></tr>" % (
                        _esc(row.get("brand")), _esc(row.get("page_type")), _esc(row.get("label")),
                        _esc(pack.get("target_date")),
                        _esc("%s (%s)" % (then_cap["date"], days(then_cap.get("distance_days") or 0))
                             if then_cap.get("date") else "-"),
                        "<br>".join("%s: %s" % (_esc(s), _esc(st.get("status"))) for s, st in states.items()),
                        "<br>".join(_esc(x) for x in flags) or "-", "".join(thumbs), " ".join(links)))
    css = COMPARE_CSS + ("td.thumbs { display: flex; gap: 6px; } td.thumbs figure { padding: 4px; width: 180px; }"
                         " td.thumbs figcaption { font-size: 11px; margin: 0; }")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>Teardown packs</title><style>%s</style></head><body>"
            '<header class="top"><h1>Teardown packs <span>%d packs</span></h1><p class="meta">%s.</p></header><main>'
            "<table><tr><th>Brand</th><th>Type</th><th>Label</th><th>Target</th><th>Then capture</th>"
            "<th>Sides</th><th>Flags</th><th>Desktop, above the fold</th><th>Files</th></tr>%s</table>"
            "</main></body></html>\n" % (css, len(packs), _esc(CRAWLER_VIEW[0].upper() + CRAWLER_VIEW[1:]),
                                         "".join(rows)))


def find_packs(path: str) -> list:
    """(pack_dir, pack.json) under path, sorted; hidden directories skipped."""
    found = []
    if os.path.isfile(os.path.join(path, "pack.json")):
        data = read_json(os.path.join(path, "pack.json"))
        return [(path, data)] if isinstance(data, dict) else []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if "pack.json" in files:
            data = read_json(os.path.join(root, "pack.json"))
            if isinstance(data, dict) and data.get("tool") == "teardown-pairs":
                found.append((root, data))
            dirs[:] = [d for d in dirs if d not in SIDES]
    return sorted(found, key=lambda x: x[0])


def load_sides(path: str, pack: dict) -> dict:
    sides = {}
    for side in SIDES:
        if side not in (pack.get("sides") or {}):
            continue
        sides[side] = {"facts": read_json(os.path.join(path, side, "facts.json")),
                       "capture": read_json(os.path.join(path, side, "capture.json"))}
    return sides


def write_pack_outputs(path: str, pack: dict) -> dict:
    """diff.json, diff.md and compare.html from the pack's saved files."""
    sides = load_sides(path, pack)
    diff = build_diff(pack, sides)
    write_json(os.path.join(path, "diff.json"), diff)
    write_text(os.path.join(path, "diff.md"), render_diff_md(diff))
    write_text(os.path.join(path, "compare.html"), render_compare_html(path, pack, diff, sides))
    return diff


def write_report(out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    packs = find_packs(out_dir)
    path = os.path.join(os.path.realpath(out_dir), "index.html")
    write_text(path, render_index_html(os.path.realpath(out_dir), [(os.path.realpath(p), d) for p, d in packs]))
    return path


# ------------------------------------------------------------- packs


class Pack:
    def __init__(self, path: str, data: dict, new: bool):
        self.path = path
        self.data = data
        self.new = new

    @property
    def sides(self) -> dict:
        return self.data["sides"]

    def side_dir(self, side: str) -> str:
        path = os.path.join(self.path, side)
        os.makedirs(path, exist_ok=True)
        return path

    def save(self) -> None:
        self.data.setdefault("timings", {})["updated_at"] = iso_now()
        settled = all(self.sides[s]["status"] in SETTLED for s in self.data.get("requested_sides") or self.sides)
        self.data["status"] = "complete" if settled else "incomplete"
        os.makedirs(self.path, exist_ok=True)
        write_json(os.path.join(self.path, "pack.json"), self.data)

    @classmethod
    def open(cls, path: str, row: Row, target: datetime.date, date: datetime.date, kind: str, years: int):
        existing = read_json(os.path.join(path, "pack.json"))
        if isinstance(existing, dict):
            if existing.get("tool") != "teardown-pairs" or existing.get("kind") != kind or \
                    existing.get("date") != date.isoformat() or existing.get("target_date") != target.isoformat():
                raise PackConflict("%s already holds a different pack (kind %s, captured for %s); use another --out "
                                   "or pass --date %s" % (path, existing.get("kind"), existing.get("date"),
                                                          existing.get("date")))
            existing["row"] = row.as_dict()
            existing["notes"] = row.notes
            return cls(path, existing, new=False)
        data = {
            "tool": "teardown-pairs", "schema": 1, "kind": kind, "row": row.as_dict(), "notes": row.notes,
            "target_date": target.isoformat(), "date": date.isoformat(), "years_back": years,
            "status": "incomplete", "requested_sides": [], "sides": {}, "versions": {},
            "timings": {"created_at": iso_now()},
            "crawler_view": CRAWLER_VIEW if kind == "pair" else None,
        }
        return cls(path, data, new=True)


def _shot_files(res: dict) -> dict:
    return {name: info["file"] for name, info in (res.get("screenshots") or {}).items()
            if isinstance(info, dict) and info.get("file")}


def _load_record(res: dict, outcome: str, sus) -> dict:
    return {
        "viewport": res.get("viewport"), "url": res.get("url"), "final_url": res.get("final_url"),
        "main_status": res.get("main_status"), "load_timeout": bool(res.get("load_timeout")),
        "error": res.get("error"), "degraded": bool(res.get("degraded")),
        "degraded_reason": res.get("degraded_reason"), "outcome": outcome, "suspect": sus,
        "consent": res.get("consent"), "user_agent": res.get("user_agent"), "mobile_basis": res.get("mobile_basis"),
        "requests": res.get("requests") or {}, "screenshots": res.get("screenshots") or {},
    }


def _sum_requests(loads) -> dict:
    keys = ("requests", "failed", "aborted_budget", "leaked", "aborted_media", "unrouted", "http_429",
            "network_errors", "bytes")
    total = {k: 0 for k in keys}
    for load in loads:
        r = load.get("requests") or {}
        for k in keys:
            total[k] += r.get(k) or 0
    total["main_status"] = next(((l.get("requests") or {}).get("main_status") for l in loads
                                 if (l.get("requests") or {}).get("main_status") is not None), None)
    return total


def build_capture_json(side, kind, status, url, capture, loads, warnings=(), original_url=None) -> dict:
    shots = {}
    truncated = False
    for load in loads:
        for name, info in (load.get("screenshots") or {}).items():
            if isinstance(info, dict) and info.get("file"):
                shots[name] = info["file"]
                truncated = truncated or bool(info.get("truncated"))
    suspect = next((l["suspect"] for l in loads if l.get("suspect")), None)
    return {
        "side": side, "kind": kind, "status": status, "url": url, "original_url": original_url or url,
        "capture": capture,
        "mobile_basis": next((l.get("mobile_basis") for l in loads if l.get("mobile_basis")), None),
        "crawler_view": CRAWLER_VIEW if kind == "archive" else None,
        "loads": loads, "requests": _sum_requests(loads),
        "flags": {"suspect": suspect, "blocked": kind == "live" and status == "blocked",
                  "degraded": any(l.get("degraded") for l in loads), "truncated": truncated},
        "screenshots": shots, "warnings": list(warnings),
    }


class Capture:
    """Runs capture and now-only over rows; owns pacing, budgets and resume."""

    def __init__(self, settings, ws, renderer, clock, sleep, log):
        self.s = settings
        self.ws = ws
        self.renderer = renderer
        self.renderer_started = False
        self.clock = clock
        self.log = log
        self.loads = 0
        self.consecutive_degraded = 0
        self.archive_pacer = Pacer(settings.delay, clock, sleep)
        self.live_pacer = HostPacer(settings.live_gap, clock, sleep)
        self.robots_cache = {}
        self.touched = {}
        self.packs = []
        self.errors = []
        self.now_done = {}

    async def ensure_renderer(self):
        if not self.renderer_started:
            await self.renderer.start()
            self.renderer_started = True

    async def close(self):
        if self.renderer_started:
            await self.renderer.close()

    def versions(self) -> dict:
        return {"teardown-pairs": VERSION, "wayback-salvage": self.ws.version if self.ws else None,
                "playwright": getattr(self.renderer, "playwright_version", None),
                "chromium": getattr(self.renderer, "chromium_version", None)}

    async def before_load(self, kind: str, host: str = None):
        if self.loads >= self.s.max_pages:
            raise StopRun(EXIT_MAX_PAGES, "--max-pages %d reached; rerun the same command to resume"
                          % self.s.max_pages)
        await self.ensure_renderer()
        if kind == "archive":
            await self.archive_pacer.wait()
        else:
            await self.live_pacer.wait(host)
        self.loads += 1

    def requested_sides(self) -> list:
        if self.s.mode == "now-only":
            return ["now-live"]
        sides = ["then"]
        if self.s.now_source in ("live", "both"):
            sides.append("now-live")
        if self.s.now_source in ("archive", "both"):
            sides.append("now-archive")
        return sides

    def needs_work(self, state: dict) -> bool:
        if state["status"] in ("planned", "failed"):
            return True
        return state["status"] == "skipped" and state.get("live_skipped") == "robots" and self.s.ignore_robots

    async def run(self, rows) -> tuple:
        code, message = EXIT_OK, ""
        try:
            for row in rows:
                for years in (self.s.years_back if self.s.mode == "capture" else [0]):
                    target = target_date(self.s.date, years) if years else self.s.date
                    try:
                        await self.do_pack(row, target, years)
                    except PackConflict as e:
                        self.errors.append(str(e))
                        self.log("error: %s" % e)
        except StopRun as e:
            code, message = e.code, str(e)
            self.log("stopped: %s" % message)
        finally:
            for path, pack in list(self.touched.items()):
                try:
                    await self.finish_pack(pack)
                except StopRun:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.log("warning: could not write the diff or compare page for %s: %s" % (path, _first_line(e)))
            write_report(self.s.out)
        if code == EXIT_OK and self.errors:
            code = EXIT_ERROR
        return code, message

    async def do_pack(self, row: Row, target: datetime.date, years: int):
        path = pack_dir(self.s.out, row, target)
        kind = "now-only" if self.s.mode == "now-only" else "pair"
        pack = Pack.open(path, row, target, self.s.date, kind, years)
        self.packs.append(pack)
        requested = self.requested_sides()
        for side in requested:
            pack.sides.setdefault(side, {"status": "planned", "attempts": []})
            if side not in pack.data["requested_sides"]:
                pack.data["requested_sides"].append(side)
        if pack.new:
            pack.save()
        work = [s for s in requested if self.needs_work(pack.sides[s])]
        if work:
            self.log("%s %s: %s" % (row.name(), target.isoformat(), ", ".join(work)))
        for side in work:
            self.touched[path] = pack
            pack.data["versions"] = self.versions()
            if side != "then" and self.reuse_now(pack, side, row):
                continue
            if side == "then":
                await self.do_archive_side(pack, "then", row.then_url, target, "then")
            elif side == "now-archive":
                await self.do_archive_side(pack, "now-archive", row.now_url, self.s.date, "now")
            else:
                await self.do_live_side(pack, row.now_url)
            if side != "then" and pack.sides[side]["status"] in ("done", "blocked"):
                self.now_done[(row.key(), side, self.s.date.isoformat())] = path
        if path not in self.touched and not os.path.exists(os.path.join(path, "diff.json")):
            self.touched[path] = pack

    def reuse_now(self, pack: Pack, side: str, row: Row) -> bool:
        """A now side finished for this row and date in another pack (another
        --years-back value) is copied instead of loaded again."""
        src = self.now_done.get((row.key(), side, self.s.date.isoformat()))
        if src is None:
            parent = os.path.dirname(pack.path)
            for other, data in find_packs(parent) if os.path.isdir(parent) else []:
                st = ((data.get("sides") or {}).get(side) or {})
                if other != pack.path and data.get("date") == self.s.date.isoformat() and \
                        st.get("status") in ("done", "blocked") and "reused_from" not in st:
                    src = other
                    break
        if src is None or src == pack.path:
            return False
        data = read_json(os.path.join(src, "pack.json")) or {}
        state = ((data.get("sides") or {}).get(side))
        if not state or state.get("status") not in ("done", "blocked"):
            return False
        dst = os.path.join(pack.path, side)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(src, side), dst)
        state = copy.deepcopy(state)
        state["reused_from"] = os.path.relpath(src, os.path.realpath(self.s.out))
        pack.sides[side] = state
        pack.save()
        self.log("  %s: reused from %s" % (side, state["reused_from"]))
        return True

    def _reset_side(self, pack: Pack, side: str) -> tuple:
        sdir = pack.side_dir(side)
        state = pack.sides[side]
        state["status"] = "planned"
        state.setdefault("attempts", [])
        state["started_at"] = iso_now()
        for key in ("reason", "finished_at", "signature", "live_blocked", "live_skipped", "warnings",
                    "skipped_candidates"):
            state.pop(key, None)
        for name in ("facts.json", "requests.jsonl"):
            try:
                os.remove(os.path.join(sdir, name))
            except FileNotFoundError:
                pass
        pack.save()
        return sdir, state

    async def do_archive_side(self, pack: Pack, side: str, url: str, target: datetime.date, mode: str):
        sdir, state = self._reset_side(pack, side)
        cands = archive_candidates(self.ws, url, target, self.s.wide) if mode == "then" else \
            now_archive_candidates(self.ws, url, target)
        attempts, skipped, suspect_digests = 0, [], set()
        last = None
        ws_error = None
        started = self.clock()
        try:
            async for cand in cands:
                if attempts >= MAX_ATTEMPTS:
                    break
                if cand["digest"] and cand["digest"] in suspect_digests:
                    skipped.append({"timestamp": cand["timestamp"], "digest": cand["digest"],
                                    "reason": "same digest as a suspect capture"})
                    continue
                await self.before_load("archive")
                attempts += 1
                try:
                    os.remove(os.path.join(sdir, "requests.jsonl"))
                except FileNotFoundError:
                    pass
                aurl = archive_url(cand["timestamp"], cand["original"] or url)
                t0 = self.clock()
                self.log("  %s: archive load %s (%s from target)" % (side, cand["timestamp"],
                                                                    days(cand["distance_days"])))
                res = await self.renderer.load_archive(aurl, sdir, self.s.viewports)
                outcome, sus = classify_load(res, "archive")
                attempt = {k: cand[k] for k in ("timestamp", "original", "date", "distance_days", "window_days",
                                                 "digest")}
                attempt.update(url=aurl, started_at=iso_now(), seconds=round(self.clock() - t0, 1), outcome=outcome,
                               signature=(sus or {}).get("signature"), detail=(sus or {}).get("detail") or
                               res.get("degraded_reason") or res.get("error"), main_status=res.get("main_status"),
                               requests=res.get("requests") or {})
                state["attempts"].append(attempt)
                self.consecutive_degraded = self.consecutive_degraded + 1 if outcome == "degraded" else 0
                last = (cand, res, outcome, sus)
                if outcome == "done":
                    state["skipped_candidates"] = skipped
                    await self.accept_archive(pack, side, sdir, cand, aurl, res, started)
                    return
                self.log("  %s: %s%s" % (side, outcome, " (%s)" % sus["signature"] if sus else ""))
                if outcome == "suspect" and cand["digest"]:
                    suspect_digests.add(cand["digest"])
                if outcome == "degraded" and self.consecutive_degraded >= DEGRADED_STOP:
                    state.update(status="failed", reason="stopped after %d consecutive degraded archive loads"
                                 % DEGRADED_STOP, finished_at=iso_now())
                    pack.save()
                    raise StopRun(EXIT_THROTTLED, "archive throttling: %d consecutive degraded loads (more than %d "
                                                  "HTTP 429s and requests with no response each, or a main document "
                                                  "that got a 429 or no response). Rerun later; do not lower --delay."
                                  % (DEGRADED_STOP, DEGRADED_429S))
                pack.save()
        except WaybackError as e:
            # a failed lookup fails this side only; exits 2 and 3 are StopRun
            ws_error = str(e)
        finally:
            await cands.aclose()
        state["skipped_candidates"] = skipped
        state["finished_at"] = iso_now()
        if last is None and ws_error:
            state.update(status="failed", reason=ws_error)
        elif last is None:
            span = (WIDE_WINDOW if self.s.wide else WINDOWS[-1]) if mode == "then" else NOW_WINDOW
            where = "within +/-%d days of %s" % (span, target.isoformat()) if mode == "then" else \
                "in the %d days up to %s" % (NOW_WINDOW, target.isoformat())
            state.update(status="failed", reason="no 200 text/html capture %s%s" % (
                where, "" if mode == "now" or self.s.wide else " (try --wide, or coverage for URL variants)"))
        else:
            cand, res, outcome, sus = last
            status = "suspect" if outcome == "suspect" or (sus and outcome != "degraded") else "failed"
            reason = "%d attempt(s), last %s%s" % (attempts, outcome, ": %s" % (
                sus["detail"] if sus else res.get("degraded_reason") or res.get("error") or ""))
            if ws_error:
                status, reason = "failed", ws_error
            state.update(status=status, reason=reason, signature=(sus or {}).get("signature"))
            # keep the last attempt's screenshots and facts as evidence
            load = _load_record(res, outcome, sus)
            if res.get("facts"):
                write_json(os.path.join(sdir, "facts.json"), res["facts"])
            write_json(os.path.join(sdir, "capture.json"), build_capture_json(
                side, "archive", status, archive_url(cand["timestamp"], cand["original"] or url), cand, [load],
                original_url=cand["original"] or url))
        self.log("  %s: %s (%s)" % (side, state["status"], state["reason"]))
        pack.save()

    async def accept_archive(self, pack, side, sdir, cand, aurl, res, started):
        state = pack.sides[side]
        warnings = []
        original = cand["original"] or pack.data["row"]["then_url"]
        martech = {"source": "wayback-salvage page --json", "loaded": [], "mentioned": []}
        try:
            info = await self.ws.fetch(original, cand["timestamp"], os.path.join(sdir, "page.html"))
            if info.get("status") not in (None, "ok"):
                warnings.append("wayback-salvage fetch status %s" % info.get("status"))
        except WaybackError as e:
            warnings.append(str(e))
        try:
            analysis = await self.ws.page(original, cand["timestamp"])
            write_json(os.path.join(sdir, "analysis.json"), analysis)
            martech = martech_from_analysis(analysis)
        except WaybackError as e:
            warnings.append(str(e))
            martech["error"] = str(e)
        if self.s.images and side == "then":
            try:
                summary = await self.ws.images(original, cand["timestamp"], os.path.join(sdir, "images"))
                state["images"] = {"status": summary.get("status"), "candidates": summary.get("candidates")}
            except WaybackError as e:
                warnings.append(str(e))
        facts = dict(res["facts"])
        facts["martech"] = martech
        write_json(os.path.join(sdir, "facts.json"), facts)
        load = _load_record(res, "done", None)
        write_json(os.path.join(sdir, "capture.json"), build_capture_json(
            side, "archive", "done", aurl, cand, [load], warnings, original_url=original))
        state.update(status="done", capture=cand, url=aurl, finished_at=iso_now(),
                     seconds=round(self.clock() - started, 1), warnings=warnings)
        pack.save()
        self.log("  %s: done (%s, %s from target)" % (side, cand["timestamp"], days(cand["distance_days"])))

    async def robots(self, url: str) -> tuple:
        """(allowed, reason, record) for user agent *; record holds the
        robots.txt URL and its HTTP status or error text."""
        key = urllib.parse.urlsplit(url).netloc
        rurl = robots_url(url)
        if key not in self.robots_cache:
            await self.ensure_renderer()
            self.robots_cache[key] = await self.renderer.fetch_robots(rurl)
        status, text, error = self.robots_cache[key]
        allowed, reason = robots_verdict(url, status, text)
        return allowed, reason, {"robots_url": rurl, "robots_status": status, "robots_error": error}

    async def do_live_side(self, pack: Pack, url: str):
        sdir, state = self._reset_side(pack, "now-live")
        started = self.clock()
        for key in ("robots_url", "robots_status", "robots_error"):
            state.pop(key, None)
        if self.s.ignore_robots:
            state["robots"] = "ignored (--ignore-robots)"
        else:
            allowed, reason, record = await self.robots(url)
            state["robots"] = reason
            state.update(record)
            if not allowed:
                said = "%s (%s)" % (reason, robots_detail(record["robots_status"], record["robots_error"]))
                state.update(status="skipped", live_skipped="robots", finished_at=iso_now(),
                             reason="robots.txt for * says %s; rerun with --ignore-robots to load anyway" % said)
                pack.save()
                self.log("  now-live: skipped (robots.txt: %s)" % said)
                return
        host = host_of(url)
        loads, blocked, results = [], None, []
        for vp in self.s.viewports:
            if blocked:
                loads.append({"viewport": vp, "outcome": "skipped",
                              "error": "not loaded: the desktop load was blocked and blocked pages are not retried"})
                continue
            await self.before_load("live", host)
            t0 = self.clock()
            self.log("  now-live: %s load %s" % (vp, url))
            res = await self.renderer.load_live(url, sdir, vp)
            outcome, sus = classify_load(res, "live")
            state["attempts"].append({"viewport": vp, "url": url, "started_at": iso_now(),
                                      "seconds": round(self.clock() - t0, 1), "outcome": outcome,
                                      "signature": (sus or {}).get("signature"),
                                      "detail": (sus or {}).get("detail") or res.get("error"),
                                      "main_status": res.get("main_status"), "requests": res.get("requests") or {}})
            loads.append(_load_record(res, outcome, sus))
            results.append((vp, res, outcome))
            if outcome == "suspect":
                blocked = sus
            pack.save()
        warnings = []
        body = next((r.get("main_body") for _, r, _ in results if r.get("main_body")), None)
        if body is not None:
            with open(os.path.join(sdir, "page.html"), "wb") as fh:
                fh.write(body)
        facts_src = next((r for vp, r, _ in results if vp == "desktop" and r.get("facts")), None) or \
            next((r for _, r, _ in results if r.get("facts")), None)
        if facts_src is not None:
            facts = dict(facts_src["facts"])
            facts["martech"] = martech_from_live(body or b"", url) if body is not None else \
                {"source": "live main document", "loaded": [], "mentioned": [], "error": "no main document body"}
            facts["facts_viewport"] = facts_src.get("viewport")
            write_json(os.path.join(sdir, "facts.json"), facts)
        outcomes = [o for _, _, o in results]
        if blocked:
            status = "blocked"
            state.update(live_blocked=True, signature=blocked["signature"],
                         reason="live load blocked: %s (%s)" % (blocked["signature"], blocked["detail"]))
        elif outcomes and all(o == "done" for o in outcomes):
            status = "done"
            state["live_blocked"] = False
        else:
            status = "failed"
            state["live_blocked"] = False
            state["reason"] = "; ".join("%s %s" % (vp, (r.get("error") or o)) for vp, r, o in results if o != "done")
        capture = {"date": self.s.date.isoformat(), "timestamp": time.strftime("%Y%m%d%H%M%S", time.gmtime()),
                   "kind": "live"}
        write_json(os.path.join(sdir, "capture.json"),
                   build_capture_json("now-live", "live", status, url, capture, loads, warnings))
        state.update(status=status, capture=capture, url=url, finished_at=iso_now(),
                     seconds=round(self.clock() - started, 1))
        pack.save()
        self.log("  now-live: %s%s" % (status, " (%s)" % state.get("reason") if status != "done" else ""))

    async def finish_pack(self, pack: Pack):
        write_pack_outputs(pack.path, pack.data)
        shots = set()
        for side in SIDES:
            cap = read_json(os.path.join(pack.path, side, "capture.json")) or {}
            shots.update(cap.get("screenshots") or {})
        targets = [("desktop", "compare-desktop.png", 1600, 1000), ("mobile", "compare-mobile.png", 1000, 1000)]
        for view, name, w, h in targets:
            if view not in shots:
                continue
            try:
                await self.ensure_renderer()
                await self.renderer.screenshot_html(os.path.join(pack.path, "compare.html"),
                                                    os.path.join(pack.path, name), w, h,
                                                    "view=%s&shot=fold&table=0" % view)
            except BrowserMissing:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("warning: %s not written: %s" % (name, _first_line(e)))

    def summary(self) -> list:
        out = []
        for pack in self.packs:
            row = pack.data["row"]
            out.append({"path": pack.path, "brand": row["brand"], "page_type": row["page_type"], "label": row["label"],
                        "target_date": pack.data["target_date"], "status": pack.data.get("status"),
                        "sides": {s: pack.sides[s]["status"] for s in pack.data.get("requested_sides") or []},
                        "reasons": {s: pack.sides[s].get("reason") for s in pack.data.get("requested_sides") or []
                                    if pack.sides[s].get("reason")}})
        return out


# --------------------------------------------------------------- coverage


def strip_query(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))


async def coverage(settings, ws, rows) -> list:
    results = []
    windows = WINDOWS + ((WIDE_WINDOW,) if settings.wide else ())
    for row in rows:
        for years in settings.years_back:
            target = target_date(settings.date, years)
            item = {"brand": row.brand, "page_type": row.page_type, "label": row.label, "then_url": row.then_url,
                    "target_date": target.isoformat(), "target_weekday": weekday(target), "windows": {},
                    "nearest": None, "variants": [], "error": None}
            try:
                frm, to = cdx_window(target, windows[-1])
                cdx = await ws.snapshots(row.then_url, frm, to)
                for w in windows:
                    item["windows"]["%d" % w] = len(ranked_within(cdx, target, w))
                sel = select_candidates(cdx, target, settings.wide)
                if sel["candidates"]:
                    c = sel["candidates"][0]
                    item["nearest"] = {k: c[k] for k in ("timestamp", "date", "distance_days", "offset_days",
                                                         "capture_weekday", "window_days")}
                elif row.page_type in ("category", "product"):
                    prefix = strip_query(row.then_url)
                    var = await ws.snapshots(prefix, frm, to, match="prefix", limit=20, collapse="urlkey")
                    item["variants_prefix"] = prefix
                    item["variants"] = [{"original": r.get("original"), "timestamp": r.get("timestamp"),
                                         "date": (ts_date(r.get("timestamp", "")) or "").__str__()}
                                        for r in var if is_usable_row(r)]
            except WaybackError as e:
                item["error"] = str(e)
            results.append(item)
    return results


def format_coverage(results, wide: bool) -> str:
    lines = []
    for it in results:
        lines.append("%s / %s / %s  target %s (%s)  %s" % (it["brand"], it["page_type"], it["label"], it["target_date"],
                                                           it["target_weekday"], it["then_url"]))
        if it["error"]:
            lines.append("  error: %s" % it["error"])
            continue
        counts = "  ".join("+/-%sd: %d" % (w, n) for w, n in it["windows"].items())
        n = it["nearest"]
        if n:
            lines.append("  %s  nearest %s (%s), %s %s" % (
                counts, n["timestamp"], n["capture_weekday"], days(n["distance_days"]),
                "before" if n["offset_days"] < 0 else ("after" if n["offset_days"] > 0 else "on the day")))
        else:
            lines.append("  %s  nearest: none%s" % (counts, "" if wide else " (try --wide for +/-90 days)"))
        if it.get("variants_prefix") is not None:
            if it["variants"]:
                lines.append("  URL variants under %s worth trying as then_url:" % it["variants_prefix"])
                for v in it["variants"]:
                    lines.append("    %s  %s" % (v["timestamp"], v["original"]))
            else:
                lines.append("  no URL variants under %s in the same window" % it["variants_prefix"])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- CLI

EXIT_CODES_HELP = """\
exit codes:
  0  done
  1  usage or unexpected error
  2  --max-pages reached (resumable: rerun the same command)
  3  stopped on archive throttling (resumable: rerun later, do not lower --delay)
  4  wayback-salvage missing or older than %s
  5  Playwright or Chromium missing (prints the install command)
""" % MIN_WS_TEXT


@dataclasses.dataclass
class Settings:
    mode: str
    out: str
    date: datetime.date
    years_back: list
    now_source: str = "both"
    viewports: tuple = VIEWPORTS
    images: bool = False
    max_pages: int = DEFAULT_MAX_PAGES
    delay: float = DEFAULT_DELAY
    wide: bool = False
    dry_run: bool = False
    ignore_robots: bool = False
    quiet: bool = False
    json: bool = False
    ws_gap: float = WS_GAP
    live_gap: float = LIVE_HOST_GAP


class _ParserExit(Exception):
    def __init__(self, status):
        super().__init__(status)
        self.status = status


def _viewports_arg(value: str) -> tuple:
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if not parts or any(p not in VIEWPORTS for p in parts):
        raise argparse.ArgumentTypeError("expected desktop, mobile or desktop,mobile")
    return tuple(v for v in VIEWPORTS if v in parts)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a whole number, got %r" % value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _seconds(value: str) -> float:
    try:
        f = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected seconds, got %r" % value)
    if not f >= 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return f


def build_parser(stdout, stderr) -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def _print_message(self, message, file=None):
            if message:
                (stderr if file is sys.stderr else stdout).write(message)

        def exit(self, status=0, message=None):
            if message:
                stderr.write(message)
            raise _ParserExit(status)

        def error(self, message):
            self.print_usage(stderr)
            stderr.write("%s: error: %s\n" % (self.prog, message))
            raise _ParserExit(EXIT_ERROR)

    fmt = argparse.RawDescriptionHelpFormatter
    common = Parser(add_help=False)
    g = common.add_argument_group("output")
    g.add_argument("-o", "--out", metavar="DIR", default="./teardown-out", help="pack directory (default: ./teardown-out)")
    g.add_argument("--quiet", action="store_true", help="no progress messages on stderr")
    g.add_argument("--json", action="store_true", help="print the result as JSON on stdout")

    pages = Parser(add_help=False)
    g = pages.add_argument_group("pages (a pages.csv, or the single-page form)")
    g.add_argument("pages_csv", nargs="?", metavar="PAGES_CSV",
                   help="CSV with brand,page_type,label,now_url[,then_url,notes]")
    g.add_argument("--url", metavar="URL", help="single page: the page as it is today")
    g.add_argument("--brand", metavar="NAME", help="single page: brand")
    g.add_argument("--type", dest="page_type", metavar="TYPE", help="single page: home, category or product")
    g.add_argument("--label", metavar="LABEL", help="single page: short unique name, e.g. mens-jackets")
    g.add_argument("--then-url", metavar="URL", help="single page: last year's URL if the page moved")
    g.add_argument("--date", metavar="YYYY-MM-DD", help="capture date (default: today in America/Denver)")

    years = Parser(add_help=False)
    g = years.add_argument_group("then date")
    g.add_argument("--years-back", metavar="N[,N...]", default="1",
                   help="target dates N years before --date, same month and day (default: 1)")
    g.add_argument("--wayback-salvage", metavar="PATH",
                   help="wayback_salvage.py (default: $WAYBACK_SALVAGE, the installed skill, ../wayback-salvage)")

    parser = Parser(prog="teardown_pairs.py", formatter_class=fmt, epilog=EXIT_CODES_HELP,
                    description="Year-over-year evidence packs (screenshots, extracted facts, diffs) for brand and "
                                "retailer homepages, category pages and product pages. Run coverage first.")
    parser.add_argument("--version", action="version", version="teardown-pairs " + VERSION)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def add(name, parents, help_text, description):
        return sub.add_parser(name, parents=parents, help=help_text, description=description, epilog=EXIT_CODES_HELP,
                              formatter_class=fmt)

    p = add("coverage", [pages, years, common], "count archive captures near each target date (no page loads)",
            "For each row and target date: CDX lookups through wayback-salvage over +/-7 and +/-30 days, the nearest "
            "capture, its distance and weekday. Category and product rows with no captures get one prefix lookup "
            "(20 rows) showing URL variants worth trying as then_url. No page loads.")
    p.add_argument("--wide", action="store_true", help="also count +/-90 days")

    p = add("capture", [pages, years, common], "build packs (archive then, live and/or archive now)",
            "Build evidence packs: the then capture nearest the target date, the now capture, screenshots at "
            "1440 and 390 wide, facts, diff.json, diff.md and compare.html. Reruns resume: sides marked done are "
            "not loaded again.")
    p.add_argument("--now-source", choices=NOW_SOURCES, default="both", help="now side: live, archive or both "
                   "(default: both)")
    p.add_argument("--viewports", type=_viewports_arg, default=VIEWPORTS, metavar="LIST",
                   help="desktop,mobile (default: both)")
    p.add_argument("--images", action="store_true", help="also run wayback-salvage images for the then capture "
                   "into then/images/ (costs requests; off by default)")
    p.add_argument("--max-pages", type=_positive_int, default=DEFAULT_MAX_PAGES, metavar="N",
                   help="page loads per run, archive and live (default: 20; exit 2 when reached, resumable)")
    p.add_argument("--delay", type=_seconds, default=DEFAULT_DELAY, metavar="SECONDS",
                   help="minimum gap between archive page loads, start to start (default: 20)")
    p.add_argument("--wide", action="store_true", help="allow a +/-90 day window when +/-30 finds nothing")
    p.add_argument("--dry-run", action="store_true", help="coverage plus the plan; no page loads")
    p.add_argument("--ignore-robots", action="store_true", help="live loads only: load pages robots.txt disallows")

    p = add("now-only", [pages, common], "capture live pages only, as next year's baseline",
            "Capture live pages only, into the same pack layout with no then/, so next year has a baseline.")
    p.add_argument("--viewports", type=_viewports_arg, default=VIEWPORTS, metavar="LIST",
                   help="desktop,mobile (default: both)")
    p.add_argument("--max-pages", type=_positive_int, default=DEFAULT_MAX_PAGES, metavar="N",
                   help="page loads per run (default: 20; exit 2 when reached, resumable)")
    p.add_argument("--ignore-robots", action="store_true", help="load pages robots.txt disallows")

    p = add("diff", [common], "recompute diff.json and diff.md from saved facts (offline)",
            "Offline: recompute diff.json, diff.md and compare.html from each pack's saved facts. PACK can be a "
            "pack directory or any directory above packs; default is --out.")
    p.add_argument("packs", nargs="*", metavar="PACK", help="pack directories (default: every pack under --out)")

    add("report", [common], "rebuild <out>/index.html across all packs (offline)",
        "Offline: rebuild <out>/index.html with one row per pack, thumbnails and flags.")
    return parser


def _rows_from_args(args) -> list:
    single = args.url or args.brand or args.page_type or args.label or args.then_url
    if args.pages_csv and single:
        raise UsageError("give a pages.csv or the single-page form (--url --brand --type --label), not both")
    if args.pages_csv:
        try:
            with open(args.pages_csv, encoding="utf-8-sig", newline="") as fh:
                text = fh.read()
        except OSError as e:
            raise UsageError("cannot read %s: %s" % (args.pages_csv, e.strerror or e))
        return parse_pages_csv(text, args.pages_csv)
    if not args.url:
        raise UsageError("give a pages.csv or --url URL --brand NAME --type TYPE --label LABEL")
    return [row_from_args(args.url, args.brand, args.page_type, args.label, args.then_url)]


def _settings(args, deps) -> Settings:
    mode = args.command
    date = parse_date(args.date) if getattr(args, "date", None) else today_denver()
    s = Settings(mode=mode, out=args.out, date=date,
                 years_back=parse_years_back(getattr(args, "years_back", "1")) if mode != "now-only" else [0],
                 quiet=args.quiet, json=args.json)
    for name in ("now_source", "viewports", "images", "max_pages", "delay", "wide", "dry_run", "ignore_robots"):
        if hasattr(args, name):
            setattr(s, name, getattr(args, name))
    s.ws_gap = deps.get("ws_gap", WS_GAP)
    s.live_gap = deps.get("live_gap", LIVE_HOST_GAP)
    return s


def _make_ws(args, s, deps, clock, sleep):
    path = resolve_ws_path(getattr(args, "wayback_salvage", None), deps.get("env", os.environ),
                           deps.get("home", os.path.expanduser("~")), REPO)
    ws_out = safe_path(s.out, WS_DIR)
    os.makedirs(ws_out, exist_ok=True)
    return WaybackSalvage(path, ws_out, runner=deps.get("runner"), clock=clock, sleep=sleep, gap=s.ws_gap)


async def _dispatch(args, deps, stdout, stderr) -> int:
    log = (lambda m: None) if args.quiet else (lambda m: stderr.write(m + "\n"))
    clock = deps.get("clock", time.monotonic)
    sleep = deps.get("sleep", asyncio.sleep)

    if args.command == "report":
        path = write_report(args.out)
        stdout.write((json.dumps({"index": path}) if args.json else path) + "\n")
        return EXIT_OK

    if args.command == "diff":
        targets = args.packs or [args.out]
        done = []
        for t in targets:
            if not os.path.isdir(t):
                raise UsageError("%s is not a directory" % t)
            for path, pack in find_packs(t):
                diff = write_pack_outputs(path, pack)
                done.append({"path": path, "comparisons": [c["name"] for c in diff["comparisons"]]})
        if args.json:
            stdout.write(json.dumps({"packs": done}, indent=2) + "\n")
        else:
            for d in done:
                stdout.write("%s: %s\n" % (d["path"], ", ".join(d["comparisons"]) or "no comparison yet"))
        return EXIT_OK

    rows = _rows_from_args(args)
    s = _settings(args, deps)
    os.makedirs(s.out, exist_ok=True)
    if s.mode == "capture" and s.delay < 10:
        log("warning: --delay %g is below 10 s; archive.org throttles replay loads" % s.delay)

    ws = None
    if s.mode in ("coverage", "capture"):
        ws = _make_ws(args, s, deps, clock, sleep)
        await ws.check_version()

    if s.mode == "coverage" or (s.mode == "capture" and s.dry_run):
        results = await coverage(s, ws, rows)
        plan = _plan(s, rows) if s.mode == "capture" else None
        if args.json:
            stdout.write(json.dumps({"coverage": results, "plan": plan} if plan is not None else results,
                                    indent=2) + "\n")
        else:
            stdout.write(format_coverage(results, s.wide))
            if plan is not None:
                stdout.write(_format_plan(plan))
        return EXIT_OK

    renderer = deps.get("renderer") or Renderer()
    cap = Capture(s, ws, renderer, clock, sleep, log)
    try:
        code, message = await cap.run(rows)
    finally:
        await cap.close()
    summary = cap.summary()
    if args.json:
        stdout.write(json.dumps({"exit_code": code, "message": message, "loads": cap.loads, "packs": summary,
                                 "errors": cap.errors, "index": os.path.join(os.path.realpath(s.out), "index.html")},
                                indent=2) + "\n")
    else:
        for p in summary:
            stdout.write("%s / %s / %s %s: %s -> %s\n" % (
                p["brand"], p["page_type"], p["label"], p["target_date"],
                ", ".join("%s %s" % kv for kv in p["sides"].items()), os.path.join(p["path"], "compare.html")))
        stdout.write("%d page loads. %s\n" % (cap.loads, message or "Done." if code == EXIT_OK else message))
    return code


def _plan(s: Settings, rows) -> list:
    plan = []
    sides = ["then"] + (["now-live"] if s.now_source in ("live", "both") else []) + \
        (["now-archive"] if s.now_source in ("archive", "both") else [])
    for row in rows:
        for years in s.years_back:
            target = target_date(s.date, years)
            path = pack_dir(s.out, row, target)
            existing = read_json(os.path.join(path, "pack.json")) or {}
            entry = {"pack": path, "row": row.name(), "target_date": target.isoformat(), "sides": {}, "loads": 0}
            for side in sides:
                status = ((existing.get("sides") or {}).get(side) or {}).get("status", "planned")
                entry["sides"][side] = status
                if status in ("planned", "failed"):
                    entry["loads"] += len(s.viewports) if side == "now-live" else 1
            plan.append(entry)
    return plan


def _format_plan(plan) -> str:
    lines = ["", "Plan (no page loads made):"]
    total = 0
    for e in plan:
        total += e["loads"]
        lines.append("  %s %s: %s; about %d page loads (an archive side can take up to %d if captures are suspect)"
                     % (e["row"], e["target_date"], ", ".join("%s %s" % kv for kv in e["sides"].items()), e["loads"],
                        MAX_ATTEMPTS))
    lines.append("  total about %d page loads" % total)
    return "\n".join(lines) + "\n"


def main(argv=None, deps=None) -> int:
    deps = deps or {}
    stdout = deps.get("stdout", sys.stdout)
    stderr = deps.get("stderr", sys.stderr)
    parser = build_parser(stdout, stderr)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except _ParserExit as e:
        return e.status
    try:
        return asyncio.run(_dispatch(args, deps, stdout, stderr))
    except UsageError as e:
        stderr.write("error: %s\n" % e)
        return EXIT_ERROR
    except StopRun as e:
        stderr.write("stopped: %s\n" % e)
        return e.code
    except WaybackMissing as e:
        stderr.write("error: %s\n" % e)
        return EXIT_WAYBACK
    except BrowserMissing as e:
        stderr.write("error: %s\n" % e)
        return EXIT_BROWSER
    except KeyboardInterrupt:
        stderr.write("interrupted; pack state so far is saved, rerun the same command to resume\n")
        return 130
    except Exception:  # noqa: BLE001
        stderr.write("unexpected error:\n" + traceback.format_exc())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
