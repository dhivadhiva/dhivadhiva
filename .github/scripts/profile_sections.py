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
      the newest release of the most recently released ORG repositories;
  <!-- FEATURED:START --> ... <!-- FEATURED:END -->
      pin cards for the ORG repositories with the most recent pushes
      (the card images are rendered by the workflow from the pin1..pinN
      step outputs this script writes to $GITHUB_OUTPUT);
  <!-- UPSTREAM:START --> ... <!-- UPSTREAM:END -->
      every pull request by USER outside ORG and USER's own repos,
      grouped by repository, newest repository first;
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
SHIELD = "https://img.shields.io"

# ORG repositories that are not products and are never featured.
NOT_FEATURED = {".github", "Dhiva-Labs", "intern-training"}
PINS = 6            # must match the number of "Pin" steps in the workflow
ACTIVITY_ROWS = 8
RELEASE_ROWS = 6
LANGUAGES = 8
HEROES = 40
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


def langs_block(repos):
    size, color = {}, {}
    for r in repos:
        for e in r["languages"]["edges"]:
            n = e["node"]["name"]
            size[n] = size.get(n, 0) + e["size"]
            color[n] = (e["node"]["color"] or "#334155").lstrip("#")
    total = sum(size.values()) or 1
    top = sorted(size, key=size.get, reverse=True)[:LANGUAGES]
    badges = []
    for n in top:
        label = urllib.parse.quote(n.replace("-", "--").replace("_", "__"), safe="")
        pct = f"{100 * size[n] / total:.1f}%25"
        badges.append(f'  <img src="{SHIELD}/badge/{label}-{pct}-{color[n]}?style=flat-square" alt="{n}" />')
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


def numbers(repos, upstream):
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
        ("releases", sum(r["releases"]["totalCount"] for r in repos)),
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


def upstream_block(prs):
    by_repo = OrderedDict()
    for pr in prs:  # newest first, so the first repo seen is the most recent one
        by_repo.setdefault(pr["repository"]["nameWithOwner"], []).append(pr)
    merged = sum(1 for p in prs if p["merged"])
    open_ = sum(1 for p in prs if p["state"] == "OPEN")
    lines = [
        f"**{len(prs)} pull requests** to **{len(by_repo)} projects**: "
        f"**{merged} merged**, **{open_} open**.",
        "",
        "| Project | Pull request | Status |",
        "| --- | --- | --- |",
    ]
    for repo, items in by_repo.items():
        url = items[0]["repository"]["url"]
        first = f"[**{repo}**]({url}/pulls?q=is%3Apr+author%3A{USER})"
        for pr in sorted(items, key=lambda p: p["number"]):
            owner, name = repo.split("/")
            badge = (f"[![]({SHIELD}/github/issues/detail/state/{owner}/{name}/{pr['number']}"
                     f"?style=flat-square&label=%23{pr['number']})]({pr['url']})")
            lines.append(f"| {first} | {clean_title(pr['title'])} | {badge} |")
            first = ""
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


def briefing_block(stats, recent, repos):
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
    latest = [(n["publishedAt"], r, n) for r in repos
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


def replace_block(text, tag, body):
    start, end = f"<!-- {tag}:START -->", f"<!-- {tag}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        sys.exit(f"README.md has no {start} ... {end} block")
    return pattern.sub(lambda _: f"{start}\n{body}\n{end}", text)


def main():
    text = README.read_text(encoding="utf-8")

    repos = org_repos()
    pins = featured(repos)
    text = replace_block(text, "LANGS", langs_block(repos))
    text = replace_block(text, "RELEASES", releases_block(repos))
    text = replace_block(text, "FEATURED", featured_block(pins))

    recent = recent_prs()
    text = replace_block(text, "ACTIVITY", activity_block(recent))

    prs = upstream_prs()
    text = replace_block(text, "UPSTREAM", upstream_block(prs))
    addons = amo_extensions()
    text = replace_block(text, "EXTENSIONS", extensions_block(addons))

    stats = numbers(repos, prs)
    text = replace_block(text, "BRIEFING", briefing_block(stats, recent, repos))
    found = heroes()
    text = replace_block(text, "HEROES", heroes_block(found))
    NUMBERS.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    README.write_text(text, encoding="utf-8")

    # Tell the workflow which repositories to render pin cards for.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for i, r in enumerate(pins, 1):
                out.write(f"pin{i}={r['name']}\n")

    print(json.dumps(stats, indent=2))
    print("featured:", ", ".join(r["name"] for r in pins))
    print(f"upstream: {len(prs)} PRs in {len({p['repository']['nameWithOwner'] for p in prs})} repos; "
          f"extensions: {len(addons)}; heroes: {len(found)}")


if __name__ == "__main__":
    main()
