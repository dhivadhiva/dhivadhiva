#!/usr/bin/env python3
"""Regenerate the data-driven blocks of README.md.

Run by .github/workflows/profile.yml. Needs `gh` (authenticated) for the
GitHub search and plain HTTPS for the public addons.mozilla.org API.

Blocks are delimited by marker comments and rewritten in place:

  <!-- UPSTREAM:START --> ... <!-- UPSTREAM:END -->
      every pull request by USER outside ORG and USER's own repos,
      grouped by repository, newest repository first;
  <!-- EXTENSIONS:START --> ... <!-- EXTENSIONS:END -->
      every public extension published on Firefox Add-ons by AMO_AUTHOR,
      with its Chrome Web Store listing when CHROME_IDS knows one.
"""
import json
import re
import subprocess
import sys
import urllib.request
from collections import OrderedDict
from pathlib import Path

USER = "dhivadhiva"
ORG = "Dhiva-Labs"
AMO_AUTHOR = "20302614"  # https://addons.mozilla.org/en-US/firefox/user/20302614/
# AMO slug -> Chrome Web Store id, for extensions published on both stores.
CHROME_IDS = {
    "dl-image-tools": "ighlhdddbockbcooddmeajncpgihlcob",
}
README = Path(__file__).resolve().parents[2] / "README.md"
SHIELD = "https://img.shields.io"


def gh_graphql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return json.loads(out)


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


def replace_block(text, tag, body):
    start, end = f"<!-- {tag}:START -->", f"<!-- {tag}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        sys.exit(f"README.md has no {start} ... {end} block")
    return pattern.sub(lambda _: f"{start}\n{body}\n{end}", text)


def main():
    text = README.read_text(encoding="utf-8")
    prs = upstream_prs()
    text = replace_block(text, "UPSTREAM", upstream_block(prs))
    addons = amo_extensions()
    text = replace_block(text, "EXTENSIONS", extensions_block(addons))
    README.write_text(text, encoding="utf-8")
    print(f"upstream: {len(prs)} PRs in {len({p['repository']['nameWithOwner'] for p in prs})} repos; "
          f"extensions: {len(addons)}")


if __name__ == "__main__":
    main()
