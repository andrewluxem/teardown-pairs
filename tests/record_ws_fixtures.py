"""Record wayback-salvage --json output as test fixtures, offline.

Runs wayback-salvage's own main() in-process with the FakeTransport from
its tests/support.py, so no request leaves the machine (that module also
installs its network guard). The responses fed to it are synthetic, but
the JSON printed on stdout, the exit codes and the stderr messages are
exactly what wayback-salvage produces, which pins the shapes the
teardown-pairs wrapper parses.

    python tests/record_ws_fixtures.py ../wayback-salvage

Rerun only when moving to a new wayback-salvage version. The repo under
the given path is read, never modified; scratch output goes to a
temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "ws")

PAGE_URL = "https://www.shop.example/"
TS = "20251024013522"

CDX_ROWS = [
    ["original", "timestamp", "mimetype", "statuscode", "digest", "length"],
    ["https://www.shop.example/", "20251017080000", "text/html", "200", "DIGESTA", "51000"],
    ["https://www.shop.example/", "20251023224510", "text/html", "200", "DIGESTB", "51200"],
    ["https://www.shop.example/", TS, "text/html", "200", "DIGESTC", "51300"],
    ["https://www.shop.example/", "20251025120000", "application/json", "200", "DIGESTD", "900"],
    ["https://www.shop.example/", "20251029101500", "text/html", "200", "DIGESTE", "51400"],
]

PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Shop Example | Fall Sale</title>
<link rel="canonical" href="https://www.shop.example/">
<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>
<script src="https://cdn.cookielaw.org/scripttemplates/otSDKStub.js"></script>
<script src="https://www.googletagmanager.com/gtag/js?id=G-XYZ"></script>
<script src="/static/app.js"></script>
<script src="https://static.shop.example/bundle.js"></script>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization",
 "sameAs": ["https://www.instagram.com/shopexample"]}</script>
<script>window.social = "https://www.facebook.com/shopexample";
window.schema = "https://schema.org/Thing";</script>
</head><body><header><nav><a href="/women">Women</a><a href="/men">Men</a></nav></header>
<main><h1>Fall Sale</h1><img src="/img/hero.jpg" alt="hero">
<p>Plenty of ordinary paragraph text so the page reads as a static document and the
JS-rendered heuristic stays quiet. It talks about jackets, boots and the season.</p>
</main></body></html>
"""


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    ws_repo = os.path.abspath(argv[1])
    # Leave no __pycache__ behind in the wayback-salvage checkout.
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.join(ws_repo, "tests"))
    import support as wsup  # noqa: E402  (installs wayback-salvage's network guard)

    ws = wsup.ws
    os.makedirs(OUT, exist_ok=True)

    def html_resp(body, status=200, memento=True):
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if memento:
            headers["Memento-Datetime"] = "Fri, 24 Oct 2025 01:35:22 GMT"
        return wsup.resp(status, headers, body.encode("utf-8"))

    def run(args, transport, cwd):
        old = os.getcwd()
        os.chdir(cwd)
        try:
            code, stdout, stderr = wsup.run_cli(args, transport)
        finally:
            os.chdir(old)
        return code, stdout, stderr

    def save(name, args, code, stdout, stderr):
        record = {
            "wayback_salvage_version": ws.VERSION,
            "args": ["<wsout>" if a == wsout else a for a in args],
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
        }
        with open(os.path.join(OUT, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("%-22s exit %d, %d bytes of stdout" % (name, code, len(stdout)))

    with tempfile.TemporaryDirectory() as tmp:
        wsout = os.path.join(tmp, "wsout")

        # --version (argparse prints it on stdout and exits 0)
        out, err = io.StringIO(), io.StringIO()
        code = ws.main(["--version"], stdout=out, stderr=err)
        save("version", ["--version"], code, out.getvalue(), err.getvalue())

        # snapshots with rows
        body = json.dumps(CDX_ROWS).encode("utf-8")
        t = wsup.FakeTransport().add(wsup.is_cdx, wsup.resp(200, {"Content-Type": "application/json"}, body))
        args = ["snapshots", PAGE_URL, "--from", "20251017", "--to", "20251031", "--status", "200",
                "-o", wsout, "--json", "--quiet"]
        save("snapshots", args, *run(args, t, tmp))

        # snapshots with no captures
        t = wsup.FakeTransport().add(wsup.is_cdx, wsup.resp(200, {"Content-Type": "application/json"}, b""))
        args = ["snapshots", PAGE_URL, "--from", "20200101", "--to", "20200131", "--status", "200",
                "-o", wsout, "--json", "--quiet"]
        save("snapshots_empty", args, *run(args, t, tmp))

        # fetch with --save (relative path, as the wrapper passes absolute ones)
        t = wsup.FakeTransport().add(wsup.wb(TS, PAGE_URL), html_resp(PAGE_HTML))
        args = ["fetch", PAGE_URL, "-t", TS, "--save", "then/page.html", "-o", wsout, "--json", "--quiet"]
        save("fetch", args, *run(args, t, tmp))

        # page analysis with loaded and mentioned script hosts
        t = wsup.FakeTransport().add(wsup.wb(TS, PAGE_URL), html_resp(PAGE_HTML))
        args = ["page", PAGE_URL, "-t", TS, "-o", wsout, "--json", "--quiet"]
        save("page", args, *run(args, t, tmp))

        # sustained archive outage page: wayback-salvage stops with exit 3
        offline = wsup.fixture("archive_offline")
        t = wsup.FakeTransport().add(lambda u: True, offline)
        args = ["page", PAGE_URL, "-t", TS, "-o", wsout, "--json", "--quiet"]
        save("page_exit3", args, *run(args, t, tmp))

        # request cap reached on a redirect hop: exit 2
        t = wsup.FakeTransport()
        t.add(wsup.wb(TS, PAGE_URL), wsup.redirect(wsup.wb("20251024013600", PAGE_URL)))
        args = ["page", PAGE_URL, "-t", TS, "-n", "1", "-o", wsout, "--json", "--quiet"]
        save("page_exit2", args, *run(args, t, tmp))

        # CDX connection refused on every attempt: 0.2.2 retries on the 503
        # schedule, then stops with exit 3 (the live failure exited 1 in 0.2.1)
        t = wsup.FakeTransport().add(wsup.is_cdx, ConnectionRefusedError(61, "Connection refused"))
        args = ["snapshots", PAGE_URL, "--from", "20251017", "--to", "20251031", "--status", "200",
                "-o", wsout, "--json", "--quiet"]
        save("snapshots_exit3_refused", args, *run(args, t, tmp))

        # a DNS failure that is not temporary is still exit 1, as urllib raises it
        cause = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        t = wsup.FakeTransport().add(wsup.is_cdx, urllib.error.URLError(reason=cause))
        save("snapshots_exit1", args, *run(args, t, tmp))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
