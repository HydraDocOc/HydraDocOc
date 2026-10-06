#!/usr/bin/env python3
"""Generate assets/activity.svg, the profile's contribution matrix.

Data comes from GitHub's GraphQL API (`user.contributionsCollection`) for one
account and one calendar year. Only values GitHub returns are rendered:

    COMMITS         totalCommitContributions
    PULL REQUESTS   totalPullRequestContributions
    ISSUES          totalIssueContributions
    ACTIVE DAYS     number of calendar days with contributionCount > 0
    grid levels     contributionLevel for every day

The output is deterministic (no timestamps), so an unchanged data set yields a
byte-identical file and the workflow has nothing to commit.

Standard library only. Examples:

    GITHUB_TOKEN=... python3 scripts/generate_activity.py --login HydraDocOc
    python3 scripts/generate_activity.py --input data.json --out /tmp/a.svg
    python3 scripts/generate_activity.py --pending          # "awaiting first sync" placeholder
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from xml.sax.saxutils import escape

GRAPHQL_URL = "https://api.github.com/graphql"

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
            contributionLevel
          }
        }
      }
    }
  }
}
"""

LEVELS = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}

# --- design tokens (match the rest of the profile) --------------------------
CREAM, INK, COBALT, SKY = "#F0EAE1", "#0A0D12", "#1E40AF", "#60A5FA"
LGRAY, DGRAY = "#BDC3CB", "#646C79"

W, H = 900, 396
GRID_X0, GRID_Y0, GRID_RIGHT = 64.0, 106.0, 868.0
CELL_RATIO = 0.79  # cell size as a fraction of the cell pitch
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class DataError(RuntimeError):
    """Raised when GitHub data cannot be fetched or is malformed."""


# =============================================================================
# fetch
# =============================================================================
def _post(url: str, token: str, body: bytes, attempts: int = 3) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "profile-activity-generator",
    }
    last = "unknown error"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code in (401, 403):
                raise DataError(f"GitHub rejected the token ({last})") from exc
            if exc.code < 500 and exc.code != 429:
                raise DataError(last) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise DataError(f"GitHub request failed after {attempts} attempts ({last})")


def fetch(login: str, year: int, token: str, url: str, now: dt.datetime) -> dict:
    start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc)
    to = min(end, now)
    if to < start:
        raise DataError(f"year {year} has not started yet")
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    body = json.dumps(
        {
            "query": QUERY,
            "variables": {"login": login, "from": start.strftime(stamp), "to": to.strftime(stamp)},
        }
    ).encode("utf-8")
    return normalise(_post(url, token, body), login, year)


def normalise(payload: dict, login: str, year: int) -> dict:
    """Reduce a GraphQL response to the small dict the renderer needs."""
    if payload.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in payload["errors"])[:300]
        raise DataError(f"GraphQL errors: {msgs}")
    user = (payload.get("data") or {}).get("user")
    if not user:
        raise DataError(f"GitHub user {login!r} not found")
    try:
        cc = user["contributionsCollection"]
        cal = cc["contributionCalendar"]
        days: dict[str, tuple[int, int]] = {}
        for week in cal["weeks"]:
            for d in week["contributionDays"]:
                if str(d["date"]).startswith(f"{year}-"):
                    days[d["date"]] = (int(d["contributionCount"]), LEVELS[d["contributionLevel"]])
        data = {
            "login": login,
            "year": year,
            "commits": int(cc["totalCommitContributions"]),
            "pull_requests": int(cc["totalPullRequestContributions"]),
            "issues": int(cc["totalIssueContributions"]),
            "total": int(cal["totalContributions"]),
            "days": [[k, v[0], v[1]] for k, v in sorted(days.items())],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"unexpected GitHub response shape ({type(exc).__name__}: {exc})") from exc
    return data


def load_input(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("login", "year", "commits", "pull_requests", "issues", "total", "days"):
        if key not in data:
            raise DataError(f"{path}: missing key {key!r}")
    return data


# =============================================================================
# render
# =============================================================================
def num(v: float) -> str:
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return s or "0"


def cell_path(x: float, y: float, c: float) -> str:
    return f"M{num(x)} {num(y)}h{num(c)}v{num(c)}h-{num(c)}z"


def fade(delay: float, length: float = 0.5) -> str:
    """Opacity entrance: hidden until `delay`, then eases in over `length` seconds.

    No `fill`/`freeze` is used: when the animation ends the element returns to its
    authored opacity (1), which is also what a viewer without SMIL sees.
    """
    total = delay + length
    if delay <= 0:
        return f'<animate attributeName="opacity" values="0;1" dur="{length:g}s" begin="0s"/>'
    return (
        f'<animate attributeName="opacity" values="0;0;1" keyTimes="0;{delay / total:.4f};1" '
        f'dur="{total:.2f}s" begin="0s"/>'
    )


LEVEL_ATTR = {
    0: f'fill="{CREAM}" stroke="{INK}" stroke-opacity="0.22" stroke-width="0.5"',
    1: f'fill="{LGRAY}"',
    2: f'fill="{DGRAY}"',
    3: f'fill="{COBALT}"',
    4: f'fill="{COBALT}"',
    "future": f'fill="none" stroke="{INK}" stroke-opacity="0.18" stroke-width="0.5" stroke-dasharray="1.5 1.5"',
}


def render(data: dict | None, year: int, login: str, as_of: dt.date) -> str:
    pending = data is None
    first, last = dt.date(year, 1, 1), dt.date(year, 12, 31)
    lead = (first.weekday() + 1) % 7  # Sunday-first weeks, like GitHub

    def pos(d: dt.date) -> tuple[int, int]:
        n = (d - first).days + lead
        return n // 7, n % 7

    ncols = pos(last)[0] + 1
    pitch = (GRID_RIGHT - GRID_X0) / (ncols - 1 + CELL_RATIO)
    cell = round(pitch * CELL_RATIO, 2)

    lookup: dict[dt.date, tuple[int, int]] = {}
    if data:
        for date_s, count, level in data["days"]:
            lookup[dt.date.fromisoformat(date_s)] = (int(count), int(level))

    # ---- metrics (only values GitHub returned) ----
    if pending:
        commits = prs = issues = active = "—"
        total_s, last_active = "AWAITING FIRST SYNC", "—"
        summary = f"Contribution matrix {year} for @{login}: awaiting first sync from GitHub."
    else:
        active_days = sorted(d for d, (c, _) in lookup.items() if c > 0)
        commits, prs, issues = (f"{data[k]:,}" for k in ("commits", "pull_requests", "issues"))
        active = f"{len(active_days):,}"
        total_s = f"{data['total']:,} CONTRIBUTIONS IN {year}"
        last_active = active_days[-1].isoformat() if active_days else "—"
        summary = (
            f"Contribution matrix {year} for @{login}: {commits} commits, {prs} pull requests, "
            f"{issues} issues opened, {active} active days. Last active {last_active}."
        )

    out: list[str] = []
    a = out.append
    a(
        f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-labelledby="t d">'
    )
    a(f'  <title id="t">{escape(f"Contribution matrix {year} — @{login}")}</title>')
    a(f'  <desc id="d">{escape(summary)}</desc>')
    a("  <defs>")
    a(
        "    <style>.m{font-family:'IBM Plex Mono','SF Mono',Consolas,monospace}"
        ".d{font-family:'Arial Black','Impact','Helvetica Neue',sans-serif;font-weight:900}</style>"
    )
    a('    <pattern id="act-grid" width="25" height="25" patternUnits="userSpaceOnUse">')
    a(f'      <path d="M 25 0 L 0 0 0 25" fill="none" stroke="{INK}" stroke-width="0.2" opacity="0.07"/>')
    a("    </pattern>")
    a("  </defs>")
    a(f'  <rect width="{W}" height="{H}" fill="{CREAM}"/>')
    a(f'  <rect width="{W}" height="{H}" fill="url(#act-grid)"/>')

    # ---- header ----
    a(f'  <line x1="32" y1="20" x2="868" y2="20" stroke="{INK}" stroke-width="1.2"/>')
    a(f'  <text class="m" x="32" y="38" font-size="8" font-weight="600" fill="{INK}" letter-spacing="3" opacity="0.6">INDEX // 06</text>')
    a(f'  <text class="m" x="140" y="38" font-size="8" fill="{INK}" letter-spacing="1.5" opacity="0.4">GITHUB CONTRIBUTION DATA</text>')
    a(f'  <text class="m" x="868" y="38" font-size="8" fill="{COBALT}" letter-spacing="1" text-anchor="end">@{escape(login)}</text>')
    a(f'  <text class="d" x="32" y="66" font-size="24" fill="{INK}" letter-spacing="1.5">/// CONTRIBUTION MATRIX</text>')
    a(f'  <text class="d" x="868" y="66" font-size="24" fill="{COBALT}" letter-spacing="1.5" text-anchor="end">{year}</text>')
    a(f'  <line x1="32" y1="76" x2="868" y2="76" stroke="{INK}" stroke-width="0.5" opacity="0.3"/>')

    # ---- month labels + weekday labels ----
    for m in range(1, 13):
        col, _ = pos(dt.date(year, m, 1))
        a(
            f'  <text class="m" x="{num(GRID_X0 + col * pitch)}" y="98" font-size="7.5" fill="{INK}" '
            f'letter-spacing="1.5" opacity="0.6">{MONTHS[m - 1]}</text>'
        )
    for row, name in ((1, "MON"), (3, "WED"), (5, "FRI")):
        a(
            f'  <text class="m" x="32" y="{num(GRID_Y0 + row * pitch + cell / 2 + 2.3)}" font-size="6.5" '
            f'fill="{INK}" letter-spacing="1" opacity="0.5">{name}</text>'
        )

    # ---- the matrix: one <g> per month so the entrance can run month by month ----
    for m in range(1, 13):
        buckets: dict[object, list[str]] = {0: [], 1: [], 2: [], 3: [], 4: [], "future": []}
        marks: list[str] = []
        d = dt.date(year, m, 1)
        while d.month == m:
            col, row = pos(d)
            x, y = GRID_X0 + col * pitch, GRID_Y0 + row * pitch
            level = lookup.get(d, (0, 0))[1]
            key: object = "future" if d > as_of else level  # days that haven't happened yet are never "empty"
            buckets[key].append(cell_path(x, y, cell))
            if key == 4:
                mk = 3.0
                marks.append(cell_path(x + (cell - mk) / 2, y + (cell - mk) / 2, mk))
            d += dt.timedelta(days=1)
        a(f'  <g id="m{m:02d}">')
        a(f"    {fade((m - 1) * 0.07)}")
        for key in (0, 1, 2, 3, 4, "future"):
            if buckets[key]:
                a(f'    <path {LEVEL_ATTR[key]} d="{"".join(buckets[key])}"/>')
        if marks:
            a(f'    <path fill="{CREAM}" d="{"".join(marks)}"/>')
        a("  </g>")

    # ---- caption + legend + metric tiles (fade in after the matrix) ----
    a('  <g id="summary">')
    a(f"    {fade(0.9)}")
    a(f'    <text class="m" x="32" y="236" font-size="8" fill="{INK}" letter-spacing="1" opacity="0.6">{escape(total_s)}</text>')
    sw, sg = 10.0, 4.0
    more_x = 868.0
    sw_x0 = more_x - 26 - (5 * sw + 4 * sg)
    a(f'    <text class="m" x="{num(sw_x0 - 6)}" y="235" font-size="7" fill="{INK}" letter-spacing="1" opacity="0.5" text-anchor="end">LESS</text>')
    for i in range(5):
        x = sw_x0 + i * (sw + sg)
        a(f'    <path {LEVEL_ATTR[i]} d="{cell_path(x, 226, sw)}"/>')
    a(f'    <path fill="{CREAM}" d="{cell_path(sw_x0 + 4 * (sw + sg) + (sw - 2.5) / 2, 226 + (sw - 2.5) / 2, 2.5)}"/>')
    a(f'    <text class="m" x="{num(more_x)}" y="235" font-size="7" fill="{INK}" letter-spacing="1" opacity="0.5" text-anchor="end">MORE</text>')
    a(f'    <line x1="32" y1="250" x2="868" y2="250" stroke="{INK}" stroke-width="0.5" opacity="0.3"/>')

    tiles = [
        ("01 · COMMITS", commits, "COMMIT CONTRIBUTIONS"),
        ("02 · PULL REQUESTS", prs, "PULL REQUESTS OPENED"),
        ("03 · ISSUES", issues, "ISSUES OPENED"),
        ("04 · ACTIVE DAYS", active, "DAYS WITH A CONTRIBUTION"),
    ]
    tw, tg = 197, 16
    for i, (label, value, sub) in enumerate(tiles):
        a(f'    <g transform="translate({32 + i * (tw + tg)}, 264)">')
        a(f'      <rect width="{tw}" height="84" fill="{INK}"/>')
        a(f'      <rect width="3" height="84" fill="{COBALT}"/>')
        a(f'      <text class="m" x="20" y="26" font-size="8" font-weight="700" fill="{SKY}" letter-spacing="2">{label}</text>')
        a(f'      <text class="m" x="{tw - 16}" y="26" font-size="7.5" fill="#CBD0D6" letter-spacing="1" opacity="0.4" text-anchor="end">{year}</text>')
        a(f'      <text class="d" x="20" y="61" font-size="30" fill="{CREAM}" letter-spacing="0.5">{escape(value)}</text>')
        a(f'      <text class="m" x="20" y="76" font-size="7" fill="#CBD0D6" letter-spacing="1" opacity="0.55">{sub}</text>')
        a("    </g>")
    a("  </g>")

    # ---- footer note ----
    if pending:
        note = "SOURCE // GITHUB CONTRIBUTIONS API · AWAITING FIRST SYNC"
    else:
        note = f"SOURCE // GITHUB CONTRIBUTIONS API · LAST ACTIVE {last_active}"
    a(f'  <text class="m" x="32" y="370" font-size="8" fill="{INK}" letter-spacing="1" opacity="0.5">{escape(note)}</text>')
    a(f'  <line x1="32" y1="380" x2="868" y2="380" stroke="{INK}" stroke-width="0.5" opacity="0.25"/>')
    a("</svg>")
    return "\n".join(out) + "\n"


# =============================================================================
# cli
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the contribution-matrix SVG.")
    p.add_argument("--login", default=os.environ.get("PROFILE_LOGIN") or os.environ.get("GITHUB_REPOSITORY_OWNER"))
    p.add_argument("--year", type=int, help="calendar year (default: current UTC year)")
    p.add_argument("--out", default="assets/activity.svg")
    p.add_argument("--input", help="render from a saved data JSON instead of calling GitHub")
    p.add_argument("--save-data", help="also write the fetched data as JSON to this path")
    p.add_argument("--pending", action="store_true", help='render the "awaiting first sync" placeholder (no data)')
    p.add_argument("--as-of", help="YYYY-MM-DD; days after this date are drawn as not-yet-happened (default: today, UTC)")
    p.add_argument("--api-url", default=os.environ.get("GITHUB_GRAPHQL_URL", GRAPHQL_URL))
    args = p.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else now.date()
    try:
        if args.pending:
            if not args.login:
                p.error("--login is required with --pending")
            data, login, year = None, args.login, args.year or as_of.year
        elif args.input:
            data = load_input(args.input)
            login, year = data["login"], int(data["year"])
        else:
            if not args.login:
                p.error("--login is required (or set GITHUB_REPOSITORY_OWNER)")
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not token:
                raise DataError("no GITHUB_TOKEN / GH_TOKEN in the environment")
            login, year = args.login, args.year or now.year
            data = fetch(login, year, token, args.api_url, now)
            if args.save_data:
                with open(args.save_data, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=1)
        svg = render(data, year, login, as_of)
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Write only on success, via a temp file, so a failed run never leaves a half-written asset.
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg)
    os.replace(tmp, args.out)
    state = "pending placeholder" if data is None else f"{len(data['days'])} days"
    print(f"wrote {args.out} ({len(svg.encode('utf-8')):,} bytes, {year}, {state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
