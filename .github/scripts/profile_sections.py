#!/usr/bin/env python3
"""Regenerate the data-driven parts of the profile.

Run by .github/workflows/profile.yml. Needs `gh` (authenticated) for the
GitHub API and plain HTTPS for the public addons.mozilla.org API. To try it
locally:  python3 .github/scripts/profile_sections.py

README.md blocks are delimited by marker comments and rewritten in place:

  <!-- LANGS:START --> ... <!-- LANGS:END -->
      languages across ORG's public repositories, by code size;
  <!-- ACTIVITY:START --> ... <!-- ACTIVITY:END -->
      USER's most recent public pull requests, anywhere on GitHub;
  <!-- RELEASES:START --> ... <!-- RELEASES:END -->
      the newest release of the most recently released public repositories,
      in ORG and USER's own (non-fork) repos;
  <!-- FEATURED:START --> ... <!-- FEATURED:END -->
      pin cards for the ORG repositories with the most recent pushes
      (the card images are rendered by the workflow from the pin1..pinN
      step outputs this script writes to $GITHUB_OUTPUT);
  <!-- UPSTREAM:START --> ... <!-- UPSTREAM:END -->
      every merged or open pull request by USER outside ORG and USER's own
      repos, grouped by the organization that owns the project (UPSTREAM_ORGS
      names them), organizations with the most merged pull requests first;
  <!-- EXTENSIONS:START --> ... <!-- EXTENSIONS:END -->
      every public extension published on Firefox Add-ons by AMO_AUTHOR,
      with its Chrome Web Store listing when CHROME_IDS knows one;
  <!-- BRIEFING:START --> ... <!-- BRIEFING:END -->
      the daily mission briefing inside the dossier: a hint of the day that
      rotates with the date, the newest pull request and release, and the
      power level derived from the contribution count;
  <!-- HEROES:START --> ... <!-- HEROES:END -->
      the Hall of Heroes: everyone who opened an issue on this repository
      titled "I have the stones" (the reward for finding the Easter eggs).

profile/numbers.json holds the counters behind the shields.io dynamic badges.
profile/hud.svg is the animated J.A.R.V.I.S. heads-up display: an arc reactor,
a looping boot sequence that reads the day's numbers, and suit-integrity bars
for the top languages. Pure SVG with CSS animations, so GitHub renders it in
an <img> tag with no scripts.

The whole run is a dozen small API calls: one GraphQL query covers every ORG
repository (stars, releases, languages), so nothing is fetched per repo and
memory use stays at a few megabytes.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

USER = "dhivadhiva"
ORG = "Dhiva-Labs"
AMO_AUTHOR = "20302614"  # https://addons.mozilla.org/en-US/firefox/user/20302614/
# AMO slug -> Chrome Web Store id, for extensions published on both stores.
CHROME_IDS = {
    "dl-image-tools": "ighlhdddbockbcooddmeajncpgihlcob",
}
ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
NUMBERS = ROOT / "profile" / "numbers.json"
HUD = ROOT / "profile" / "hud.svg"
SHIELD = "https://img.shields.io"

# ORG repositories that are not products and are never featured.
NOT_FEATURED = {".github", "Dhiva-Labs", "intern-training"}
PINS = 6            # must match the number of "Pin" steps in the workflow
ACTIVITY_ROWS = 8
RELEASE_ROWS = 6
LANGUAGES = 8
HEROES = 40

# Upstream owner login -> the organization shown on the profile. Projects in
# the same ecosystem share a name (monero-lws is maintained by vtnerd; the
# FCMP++ work lives in seraphis-migration). Unknown owners show their login
# and sort last. Organizations with more merged pull requests come first;
# ties keep the order of this table.
UPSTREAM_ORGS = {
    "monero-project": "Monero",
    "vtnerd": "Monero",
    "seraphis-migration": "Monero",
    "google": "Google",
    "microsoft": "Microsoft",
    "discord": "Discord",
    "coinbase": "Coinbase",
    "base": "Base",
    "unstablebuild": "Rune",
}
# The avatar shown for an organization, when its name covers several owners.
UPSTREAM_AVATARS = {"Monero": "monero-project"}
HEROES_TITLE = "I have the stones"

# Power level, by lifetime contributions. The last threshold that fits wins.
RANKS = [
    (0, "Recruit"),
    (250, "Agent of S.H.I.E.L.D."),
    (1000, "Level 7"),
    (2000, "Avenger"),
    (5000, "Founding Avenger"),
    (10000, "Sorcerer Supreme"),
    (25000, "Cosmic Entity"),
]

# One per day of the year, so the briefing changes every morning.
HINTS = [
    "Hint: six stones are hiding above this dossier. Hover each one.",
    "Hint: the footer has a post-credits scene. Hover over it.",
    "Hint: the source of this README has a Level 7 message at the very top.",
    "Debugging is just time travel with a worse interface.",
    "Rust is the arc reactor. Flutter is the suit. Coffee is the vibranium.",
    "The daily build is the only Avenger that never misses a call.",
    "Snap. Half the bugs are gone. The other half are in the issue tracker.",
    "Every unsafe block is a Skrull. Trust nothing until it is verified.",
    "The suit upgrades itself: one mark per open-source repository.",
    "Whatever it takes: ship it, then fix it upstream.",
    "With great compute comes great responsibility.",
    "Multiverse rule: one codebase, every operating system.",
    "A merged upstream PR is the only infinity gauntlet worth wearing.",
    "Found all six stones? Open an issue titled \"I have the stones\".",
    "House Party Protocol: every app, on every platform, at once.",
    "J.A.R.V.I.S. runs on GitHub Actions now. He is fine with it.",
    "The suit is nanotech. It is also 44% Dart.",
    "Mark I was built in a cave. Mark 49 was built in Chennai.",
    "Repulsors charged. Lints at zero. Deploying.",
]


def gh_graphql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    if data.get("errors"):
        sys.exit(f"GraphQL error: {data['errors']}")
    return data


def search_count(q):
    query = "query($q:String!){search(query:$q,type:ISSUE,first:1){issueCount}}"
    return gh_graphql(query, q=q)["data"]["search"]["issueCount"]


def day(iso):
    """2026-11-08T22:22:47Z -> 'Nov 8'."""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return f"{d:%b} {d.day}"


def cell(text):
    """Make free text safe inside a markdown table cell or link label."""
    return (text or "").strip().replace("\n", " ").replace("|", "\\|").replace("<", "&lt;")


# ---------------------------------------------------------------------------
# Organization repositories: one query for stars, releases and languages
# ---------------------------------------------------------------------------
def org_repos():
    """Public, non-fork, non-archived ORG repositories, most recent push first."""
    query = """
    query($org:String!, $after:String) {
      organization(login:$org) {
        repositories(first:100, after:$after, isFork:false, privacy:PUBLIC,
                     orderBy:{field:PUSHED_AT, direction:DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name url isArchived pushedAt stargazerCount
            releases(first:1, orderBy:{field:CREATED_AT, direction:DESC}) {
              totalCount
              nodes { tagName name url publishedAt isPrerelease }
            }
            languages(first:10, orderBy:{field:SIZE, direction:DESC}) {
              edges { size node { name color } }
            }
          }
        }
      }
    }"""
    repos, after = [], None
    while True:
        data = gh_graphql(query, org=ORG, **({"after": after} if after else {}))
        page = data["data"]["organization"]["repositories"]
        repos += [r for r in page["nodes"] if not r["isArchived"]]
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        after = page["pageInfo"]["endCursor"]


def user_released_repos():
    """USER's own public, non-fork, non-archived repositories with a release.

    Some products (Circuit Lab, for one) live on the personal account rather
    than in ORG; without these their releases never reach the profile.
    """
    query = """
    query($login:String!, $after:String) {
      user(login:$login) {
        repositories(first:100, after:$after, isFork:false, privacy:PUBLIC,
                     ownerAffiliations:OWNER, orderBy:{field:PUSHED_AT, direction:DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            name url isArchived pushedAt
            releases(first:1, orderBy:{field:CREATED_AT, direction:DESC}) {
              totalCount
              nodes { tagName name url publishedAt isPrerelease }
            }
          }
        }
      }
    }"""
    repos, after = [], None
    while True:
        data = gh_graphql(query, login=USER, **({"after": after} if after else {}))
        page = data["data"]["user"]["repositories"]
        repos += [r for r in page["nodes"]
                  if not r["isArchived"] and r["releases"]["totalCount"]]
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        after = page["pageInfo"]["endCursor"]


def language_shares(repos, limit=LANGUAGES):
    """[(name, percent, hex color without '#')] for the biggest languages."""
    size, color = {}, {}
    for r in repos:
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            size[n] = size.get(n, 0) + e["size"]
            color[n] = (e["node"]["color"] or "#334155").lstrip("#")
    total = sum(size.values()) or 1
    top = sorted(size, key=size.get, reverse=True)[:limit]
    return [(n, 100 * size[n] / total, color[n]) for n in top]


def langs_block(repos):
    badges = []
    for n, share, color in language_shares(repos):
        label = urllib.parse.quote(n.replace("-", "--").replace("_", "__"), safe="")
        pct = f"{share:.1f}%25"
        badges.append(f'  <img src="{SHIELD}/badge/{label}-{pct}-{color}?style=flat-square" alt="{n}" />')
    return "\n".join(
        ['<div align="center">',
         f"  <sub>Code across {len(repos)} Dhiva Labs repositories, by size</sub><br/>"]
        + badges + ["</div>"])


def releases_block(repos):
    latest = [(n["publishedAt"], r, n) for r in repos
              for n in r["releases"]["nodes"] if n["publishedAt"]]
    latest.sort(key=lambda x: x[0], reverse=True)
    lines = ["| Project | Release | Published |", "| --- | --- | --- |"]
    for published, r, n in latest[:RELEASE_ROWS]:
        label = f"[`{cell(n['tagName'])}`]({n['url']})"
        if n["isPrerelease"]:
            label += " <sub>pre-release</sub>"
        lines.append(f"| [**{r['name']}**]({r['url']}) | {label} | {day(published)} |")
    return "\n".join(lines)


def featured(repos):
    return [r for r in repos if r["name"] not in NOT_FEATURED][:PINS]


def featured_block(pins):
    lines = ['<div align="center">']
    for i, r in enumerate(pins, 1):
        lines.append(f'  <a href="{r["url"]}"><img src="./profile/pin-{i}.svg" alt="{r["name"]}" /></a>')
    lines += ["</div>", "",
              '<p align="center"><sub>Picked daily: the six Dhiva Labs projects with the newest commits</sub></p>']
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recent activity: the newest public pull requests, wherever they are
# ---------------------------------------------------------------------------
def recent_prs():
    query = """
    query($q:String!) {
      search(query:$q, type:ISSUE, first:40) {
        nodes { ... on PullRequest {
          number title url state createdAt mergedAt closedAt
          repository { nameWithOwner url isPrivate }
        } }
      }
    }"""
    q = f"author:{USER} is:pr is:public sort:updated-desc"
    nodes = gh_graphql(query, q=q)["data"]["search"]["nodes"]
    prs = [n for n in nodes if n and not n["repository"]["isPrivate"]]
    for pr in prs:
        pr["when"] = pr["mergedAt"] or pr["closedAt"] or pr["createdAt"]
    prs.sort(key=lambda p: p["when"], reverse=True)
    return prs[:ACTIVITY_ROWS]


def activity_block(prs):
    verb = {"MERGED": "🟣 **Merged**", "OPEN": "🟢 **Opened**", "CLOSED": "🔴 **Closed**"}
    lines = []
    for pr in prs:
        repo = pr["repository"]
        lines.append(f"- {verb[pr['state']]} [{clean_title(pr['title'])}]({pr['url']}) "
                     f"in [{repo['nameWithOwner']}]({repo['url']}) · <sub>{day(pr['when'])}</sub>")
    return "\n".join(lines) or "_No public pull requests yet._"


# ---------------------------------------------------------------------------
# Counters for the shields.io dynamic badges
# ---------------------------------------------------------------------------
def contribution_totals():
    """(commits, contributions) summed over every year of the account."""
    query = """
    query($login:String!, $from:DateTime!, $to:DateTime!) {
      user(login:$login) {
        createdAt
        contributionsCollection(from:$from, to:$to) {
          totalCommitContributions
          contributionCalendar { totalContributions }
        }
      }
    }"""
    def year(y):
        v = {"login": USER, "from": f"{y}-01-01T00:00:00Z", "to": f"{y}-12-31T23:59:59Z"}
        return gh_graphql(query, **v)["data"]["user"]
    this_year = datetime.now(timezone.utc).year
    first = year(this_year)
    commits = contributions = 0
    for y in range(int(first["createdAt"][:4]), this_year + 1):
        c = (first if y == this_year else year(y))["contributionsCollection"]
        commits += c["totalCommitContributions"]
        contributions += c["contributionCalendar"]["totalContributions"]
    return commits, contributions


def numbers(repos, upstream, shipped):
    commits, contributions = contribution_totals()
    stats = OrderedDict([
        ("commits", commits),
        ("contributions", contributions),
        ("prs", search_count(f"author:{USER} is:pr")),
        ("merged", search_count(f"author:{USER} is:pr is:merged")),
        ("issues", search_count(f"author:{USER} is:issue")),
        ("upstream", sum(1 for p in upstream if p["merged"])),
        ("repos", len(repos)),
        ("stars", sum(r["stargazerCount"] for r in repos)),
        ("releases", sum(r["releases"]["totalCount"] for r in shipped)),
        ("updated", f"{datetime.now(timezone.utc):%Y-%m-%d}"),
    ])
    stats["rank"] = rank(stats["contributions"])
    stats["suit"] = f"Mark {stats['repos']}"
    return stats


def upstream_prs():
    q = f"author:{USER} is:pr -org:{ORG} -user:{USER} sort:created-desc"
    query = """
    query($q:String!, $after:String) {
      search(query:$q, type:ISSUE, first:100, after:$after) {
        pageInfo { hasNextPage endCursor }
        nodes { ... on PullRequest {
          number title url state merged createdAt
          repository { nameWithOwner url isPrivate }
        } }
      }
    }"""
    prs, after = [], None
    while True:
        data = gh_graphql(query, q=q, **({"after": after} if after else {}))["data"]["search"]
        prs += [n for n in data["nodes"] if n and not n["repository"]["isPrivate"]]
        if not data["pageInfo"]["hasNextPage"]:
            return prs
        after = data["pageInfo"]["endCursor"]


def clean_title(title):
    title = re.sub(r"^[a-z][\w./-]*(\([^)]*\))?!?:\s+", "", title.strip())  # feat(x): ...
    title = title[:1].upper() + title[1:]
    return title.replace("|", "\\|").replace("<", "&lt;")


def upstream_org(repo):
    """'google/safe-bindings' -> 'Google'."""
    owner = repo.split("/")[0]
    return UPSTREAM_ORGS.get(owner.lower(), owner)


def upstream_block(prs):
    """Merged and open upstream pull requests, grouped by organization.

    Pull requests closed without a merge are left out: they are not
    contributions. Organizations with the most merged pull requests come
    first; inside one, projects with the most merged pull requests, then
    merged before open.
    """
    prs = [p for p in prs if p["merged"] or p["state"] == "OPEN"]
    if not prs:
        return "_No upstream pull requests yet._"
    orgs = OrderedDict()
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        orgs.setdefault(upstream_org(repo), OrderedDict()).setdefault(repo, []).append(pr)

    def merged(items):
        return sum(1 for p in items if p["merged"])

    priority = list(OrderedDict.fromkeys(UPSTREAM_ORGS.values()))

    def org_key(item):
        name, repos = item
        allprs = [p for items in repos.values() for p in items]
        return (-merged(allprs), priority.index(name) if name in priority else len(priority))

    ordered = sorted(orgs.items(), key=org_key)
    total_merged = merged(prs)
    total_open = len(prs) - total_merged
    names = [n for n, _ in ordered]
    listed = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]

    def avatar(name, repos, size):
        owner = UPSTREAM_AVATARS.get(name) or next(iter(repos)).split("/")[0]
        return (f'<img src="https://github.com/{owner}.png?size={size * 2}" '
                f'width="{size}" height="{size}" alt="{name}" />')

    lines = [
        f"**{total_merged} merged** and **{total_open} in review**, across "
        f"**{sum(len(r) for _, r in ordered)} projects** at **{len(ordered)} organizations**: "
        f"{listed}.",
        "",
        '<p align="center">',
    ]
    for name, repos in ordered:
        allprs = [p for items in repos.values() for p in items]
        m = merged(allprs)
        tip = f"{name} · {m} merged, {len(allprs) - m} in review"
        q = urllib.parse.quote(f"is:pr author:{USER} " + " ".join(f"repo:{r}" for r in repos))
        url = f"https://github.com/search?q={q}&type=pullrequests"
        lines.append(f'  <a href="{url}" title="{tip}">{avatar(name, repos, 44)}</a>&nbsp;&nbsp;')
    lines += ["</p>", "",
              "| Organization | Project | Pull request | Status |",
              "| --- | --- | --- | --- |"]
    for name, repos in ordered:
        org_cell = f"{avatar(name, repos, 20)} **{name}**"
        for repo, items in sorted(repos.items(), key=lambda kv: -merged(kv[1])):
            url = items[0]["repository"]["url"]
            proj_cell = f"[**{repo}**]({url}/pulls?q=is%3Apr+author%3A{USER})"
            for pr in sorted(items, key=lambda p: (not p["merged"], p["number"])):
                owner, rname = repo.split("/")
                badge = (f"[![]({SHIELD}/github/issues/detail/state/{owner}/{rname}/{pr['number']}"
                         f"?style=flat-square&label=%23{pr['number']})]({pr['url']})")
                lines.append(f"| {org_cell} | {proj_cell} | {clean_title(pr['title'])} | {badge} |")
                org_cell = proj_cell = ""
    return "\n".join(lines)


def amo_extensions():
    url = (f"https://addons.mozilla.org/api/v5/addons/search/?author={AMO_AUTHOR}"
           f"&app=firefox&type=extension&sort=created&page_size=50")
    req = urllib.request.Request(url, headers={"User-Agent": "dhivadhiva-profile"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["results"]


def extensions_block(addons):
    lines = [
        "| Extension | What it does | Firefox | Chrome |",
        "| --- | --- | --- | --- |",
    ]
    for a in addons:
        slug = a["slug"]
        name = a["name"].get("en-US") or next(iter(a["name"].values()))
        summary = ((a.get("summary") or {}).get("en-US") or "").split(". ")[0].rstrip(".")
        summary = summary.replace("|", "\\|")
        amo = f"https://addons.mozilla.org/en-US/firefox/addon/{slug}/"
        fx = (f"[![]({SHIELD}/amo/v/{slug}?style=flat-square&label=&color=FF7139)]({amo}) "
              f"[![]({SHIELD}/amo/users/{slug}?style=flat-square&label=users&color=334155)]({amo})")
        if slug in CHROME_IDS:
            cws = f"https://chromewebstore.google.com/detail/{CHROME_IDS[slug]}"
            ch = (f"[![]({SHIELD}/chrome-web-store/v/{CHROME_IDS[slug]}"
                  f"?style=flat-square&label=&color=4285F4)]({cws})")
        else:
            ch = "—"
        lines.append(f"| [**{name}**]({amo}) | {summary} | {fx} | {ch} |")
    return "\n".join(lines)


def rank(contributions):
    return [name for floor, name in RANKS if contributions >= floor][-1]


def briefing_block(stats, recent, repos, shipped):
    """The dossier's daily mission briefing."""
    now = datetime.now(timezone.utc)
    hint = HINTS[now.timetuple().tm_yday % len(HINTS)]
    lines = [
        "**📡 Daily mission briefing** · <sub>regenerated " + f"{now:%b} {now.day}" + "</sub>",
        "",
        f"> {hint}",
        "",
        "| | |",
        "| --- | --- |",
        f"| **Power level** | **{stats['rank']}** · {stats['contributions']:,} contributions, "
        f"{stats['merged']} pull requests merged |",
        f"| **Suit** | **{stats['suit']}** · {stats['repos']} open-source repositories, "
        f"{stats['releases']} releases shipped |",
    ]
    if recent:
        pr = recent[0]
        state = {"MERGED": "merged", "OPEN": "opened", "CLOSED": "closed"}[pr["state"]]
        lines.append(f"| **Latest mission** | [{clean_title(pr['title'])}]({pr['url']}) "
                     f"in [{pr['repository']['nameWithOwner']}]({pr['repository']['url']}), "
                     f"{state} {day(pr['when'])} |")
    latest = [(n["publishedAt"], r, n) for r in shipped
              for n in r["releases"]["nodes"] if n["publishedAt"]]
    if latest:
        published, r, n = max(latest, key=lambda x: x[0])
        lines.append(f"| **Latest drop** | [**{r['name']}**]({r['url']}) "
                     f"[`{cell(n['tagName'])}`]({n['url']}), {day(published)} |")
    if repos:
        r = repos[0]
        lines.append(f"| **Currently assembling** | [**{r['name']}**]({r['url']}), "
                     f"last push {day(r['pushedAt'])} |")
    return "\n".join(lines)


def heroes():
    """People who opened an issue titled HEROES_TITLE on this repository."""
    query = """
    query($q:String!) {
      search(query:$q, type:ISSUE, first:100) {
        nodes { ... on Issue {
          title url createdAt
          author { login url }
        } }
      }
    }"""
    q = f'repo:{USER}/{USER} is:issue "{HEROES_TITLE}" in:title sort:created-asc'
    nodes = gh_graphql(query, q=q)["data"]["search"]["nodes"]
    seen, found = set(), []
    for n in nodes:
        if not n or not n.get("author") or HEROES_TITLE.lower() not in n["title"].lower():
            continue
        login = n["author"]["login"]
        if login in seen or login == USER:
            continue
        seen.add(login)
        found.append({"login": login, "url": n["author"]["url"],
                      "issue": n["url"], "when": n["createdAt"]})
    found.sort(key=lambda h: h["when"])
    return found[:HEROES]


def heroes_block(found):
    if not found:
        return ("<p align=\"center\"><sub>Nobody has collected all six stones yet. "
                "Be the first: find them, then open an issue titled "
                f"<b>\"{HEROES_TITLE}\"</b>.</sub></p>")
    lines = ['<p align="center">']
    for h in found:
        login = h["login"].replace('"', "")
        lines.append(f'  <a href="{h["issue"]}" title="{login} · {day(h["when"])}">'
                     f'<img src="https://github.com/{login}.png?size=96" width="56" height="56" '
                     f'style="border-radius: 50%;" alt="{login}" /></a>')
    lines += ["</p>",
              f'<p align="center"><sub>{len(found)} hero{"es" if len(found) != 1 else ""} '
              "so far. Hover an avatar for the name and the day they found the stones.</sub></p>"]
    return "\n".join(lines)


def xml(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def hud_svg(stats, recent, repos):
    """The animated J.A.R.V.I.S. heads-up display, written to profile/hud.svg."""
    W, H = 880, 330
    now = datetime.now(timezone.utc)
    merged_pct = round(100 * stats["merged"] / max(stats["prs"], 1))
    mission = clean_title(recent[0]["title"]) if recent else "standing by"
    if len(mission) > 44:
        mission = mission[:43].rstrip() + "…"
    lines = [
        f"Good morning, sir. Loading suit {stats['suit']}…",
        f"Arc reactor: {stats['contributions']:,} contributions · {stats['commits']:,} commits",
        f"Armory: {stats['repos']} open-source repositories · {stats['releases']} releases shipped",
        f"Pull requests merged: {stats['merged']} of {stats['prs']} ({merged_pct}%)",
        f"Latest mission: {mission}",
        f"Threat scan: 0 unsafe blocks. Clearance: {stats['rank'].upper()}",
        "All systems nominal.",
    ]
    langs = language_shares(repos, 5)

    # One 18 s loop: the boot lines appear one by one and hold, the bars grow,
    # then everything resets for the next pass.
    loop = 18.0
    css = [
        "text{font-family:'Fira Code','JetBrains Mono',ui-monospace,Consolas,monospace}",
        ".k{fill:#7DD3FC;letter-spacing:.18em;font-size:10px}",
        ".l{fill:#E2E8F0;font-size:13px;opacity:0;animation:boot %.0fs linear infinite}" % loop,
        ".dim{fill:#64748B;font-size:11px}",
        ".gold{fill:#F59E0B}.green{fill:#4ADE80}",
        ".ring{transform-origin:150px 150px;transform-box:view-box}",
        ".spin{animation:spin 14s linear infinite}",
        ".spinr{animation:spin 9s linear infinite reverse}",
        ".core{transform-origin:150px 150px;transform-box:view-box;animation:pulse 2.4s ease-in-out infinite}",
        ".glow{transform-origin:150px 150px;transform-box:view-box;animation:glow 2.4s ease-in-out infinite}",
        ".bar{transform-box:fill-box;transform-origin:left;transform:scaleX(0);animation:grow %.0fs ease-out infinite}" % loop,
        ".cur{animation:blink 1s steps(2,start) infinite}",
        ".scan{animation:scan 6s linear infinite}",
        "@keyframes spin{to{transform:rotate(360deg)}}",
        "@keyframes pulse{0%,100%{transform:scale(.94)}50%{transform:scale(1.04)}}",
        "@keyframes glow{0%,100%{opacity:.35}50%{opacity:.8}}",
        "@keyframes blink{to{visibility:hidden}}",
        "@keyframes scan{from{transform:translateY(-10px)}to{transform:translateY(%dpx)}}" % H,
        "@keyframes boot{0%%{opacity:0}var(--in)%%{opacity:0}var(--on)%%{opacity:1}93%%{opacity:1}97%%{opacity:0}100%%{opacity:0}}",
        "@keyframes grow{0%,10%{transform:scaleX(0)}30%{transform:scaleX(1)}93%{transform:scaleX(1)}97%,100%{transform:scaleX(0)}}",
    ]
    # CSS variables do not work inside @keyframes percentages, so emit one rule per line.
    css = [c for c in css if "var(--in)" not in c]
    for i in range(len(lines)):
        start = 3 + i * 8          # percent of the loop at which line i starts fading in
        css.append("@keyframes boot%d{0%%,%d%%{opacity:0}%d%%{opacity:1}93%%{opacity:1}97%%,100%%{opacity:0}}"
                   % (i, start, start + 3))
        css.append(".l%d{animation-name:boot%d}" % (i, i))

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="J.A.R.V.I.S. heads-up display">',
        "<style>" + "".join(css) + "</style>",
        "<defs>",
        '<radialGradient id="core"><stop offset="0" stop-color="#F0F9FF"/><stop offset=".45" stop-color="#7DD3FC"/>'
        '<stop offset="1" stop-color="#0EA5E9" stop-opacity=".2"/></radialGradient>',
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0B1220"/>'
        '<stop offset="1" stop-color="#111827"/></linearGradient>',
        '<linearGradient id="frame" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#B91C1C"/>'
        '<stop offset=".5" stop-color="#F59E0B"/><stop offset="1" stop-color="#B91C1C"/></linearGradient>',
        '<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="14"/></filter>',
        '<pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">'
        '<path d="M24 0H0V24" fill="none" stroke="#1E293B" stroke-width=".6"/></pattern>',
        '<clipPath id="clip"><rect width="%d" height="%d" rx="14"/></clipPath>' % (W, H),
        "</defs>",
        f'<rect width="{W}" height="{H}" rx="14" fill="url(#bg)"/>',
        f'<rect width="{W}" height="{H}" rx="14" fill="url(#grid)" opacity=".7"/>',
        f'<rect x=".75" y=".75" width="{W-1.5}" height="{H-1.5}" rx="13.5" fill="none" stroke="url(#frame)" stroke-width="1.5"/>',
        # scan line
        f'<g clip-path="url(#clip)"><rect class="scan" width="{W}" height="3" fill="#38BDF8" opacity=".12"/></g>',
        # arc reactor
        f'<g transform="translate(0 15)">',
        '<circle class="glow" cx="150" cy="150" r="70" fill="#38BDF8" filter="url(#blur)"/>',
        '<circle cx="150" cy="150" r="104" fill="none" stroke="#38BDF8" stroke-width="1.5" opacity=".3"/>',
        '<circle class="ring spin" cx="150" cy="150" r="90" fill="none" stroke="#7DD3FC" stroke-width="11" '
        'stroke-dasharray="24 11" opacity=".85"/>',
        '<circle class="ring spinr" cx="150" cy="150" r="70" fill="none" stroke="#BAE6FD" stroke-width="3" '
        'stroke-dasharray="5 7" opacity=".9"/>',
        '<circle class="core" cx="150" cy="150" r="46" fill="url(#core)"/>',
        '<polygon class="core" points="150,116 182,170 118,170" fill="none" stroke="#F0F9FF" stroke-width="3" '
        'stroke-linejoin="round" opacity=".95"/>',
        '<text x="150" y="282" text-anchor="middle" class="k">ARC REACTOR</text>',
        '<text x="150" y="300" text-anchor="middle" class="k green" style="font-size:11px">● ONLINE</text>',
        f'<text x="150" y="40" text-anchor="middle" class="k gold">{xml(stats["suit"].upper())}</text>',
        "</g>",
        # divider
        '<line x1="312" y1="28" x2="312" y2="302" stroke="#1E3A8A" stroke-width="1"/>',
        # boot sequence
        '<text x="336" y="42" class="k">J.A.R.V.I.S.</text>',
        f'<text x="{W-28}" y="42" text-anchor="end" class="dim">boot sequence · {now:%b} {now.day}</text>',
    ]
    y = 70
    for i, line in enumerate(lines):
        cur = ' <tspan class="cur">▌</tspan>' if i == len(lines) - 1 else ""
        out.append(f'<text x="336" y="{y}" class="l l{i}"><tspan class="gold">›</tspan> {xml(line)}{cur}</text>')
        y += 21
    # suit integrity bars
    y += 4
    out.append(f'<text x="336" y="{y}" class="k">SUIT INTEGRITY · CODE BY LANGUAGE</text>')
    y += 14
    bar_x, bar_w = 430, W - 430 - 28
    for i, (name, share, color) in enumerate(langs):
        out.append(f'<text x="336" y="{y+10}" class="dim" style="fill:#CBD5E1">{xml(name)}</text>')
        out.append(f'<rect x="{bar_x}" y="{y}" width="{bar_w}" height="12" rx="6" fill="#1E293B"/>')
        out.append(f'<rect class="bar" x="{bar_x}" y="{y}" width="{bar_w * share / 100:.1f}" height="12" rx="6" '
                   f'fill="#{color}" style="animation-delay:{0.15*i:.2f}s"/>')
        out.append(f'<text x="{W-28}" y="{y+10}" text-anchor="end" class="dim">{share:.1f}%</text>')
        y += 17
    out.append("</svg>")
    return "\n".join(out)


def replace_block(text, tag, body):
    start, end = f"<!-- {tag}:START -->", f"<!-- {tag}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        sys.exit(f"README.md has no {start} ... {end} block")
    return pattern.sub(lambda _: f"{start}\n{body}\n{end}", text)


def main():
    text = README.read_text(encoding="utf-8")

    repos = org_repos()
    shipped = repos + user_released_repos()
    pins = featured(repos)
    text = replace_block(text, "LANGS", langs_block(repos))
    text = replace_block(text, "RELEASES", releases_block(shipped))
    text = replace_block(text, "FEATURED", featured_block(pins))

    recent = recent_prs()
    text = replace_block(text, "ACTIVITY", activity_block(recent))

    prs = upstream_prs()
    text = replace_block(text, "UPSTREAM", upstream_block(prs))
    addons = amo_extensions()
    text = replace_block(text, "EXTENSIONS", extensions_block(addons))

    stats = numbers(repos, prs, shipped)
    text = replace_block(text, "BRIEFING", briefing_block(stats, recent, repos, shipped))
    found = heroes()
    text = replace_block(text, "HEROES", heroes_block(found))
    NUMBERS.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    HUD.write_text(hud_svg(stats, recent, repos), encoding="utf-8")
    README.write_text(text, encoding="utf-8")

    # Tell the workflow which repositories to render pin cards for.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for i, r in enumerate(pins, 1):
                out.write(f"pin{i}={r['name']}\n")

    print(json.dumps(stats, indent=2))
    print("featured:", ", ".join(r["name"] for r in pins))
    print(f"releases from {len(shipped) - len(repos)} personal repos; "
          f"upstream: {len(prs)} PRs in {len({p['repository']['nameWithOwner'] for p in prs})} repos; "
          f"extensions: {len(addons)}; heroes: {len(found)}")


if __name__ == "__main__":
    main()
