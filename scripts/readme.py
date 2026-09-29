#!/usr/bin/env python3
"""Keep the chrissgon/chrissgon profile README alive: picks, problems, weekly numbers.

Usage:
  python3 scripts/readme.py issue --event <event.json> [--apply]   handle an issue event (pick or problem)
  python3 scripts/readme.py weekly [--apply]                         measure numbers, close the round, open the next
  python3 scripts/readme.py render                                   rebuild README.md and the generated SVGs

Without --apply nothing leaves the machine: files are written locally and the replies, closes and
commit are printed as JSON on stdout. With --apply the script calls the GitHub API with GITHUB_TOKEN
and GITHUB_REPOSITORY (set by GitHub Actions) and commits the changed files.

Rules (NFR-2): text written by a visitor never reaches the README. A pick becomes a count; a problem
is shown only after the owner adds the label `accepted`, with the title frozen at that moment.
Replies are the fixed texts in REPLIES. Standard library only. README_NO_FONTS=1 skips font download.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OWNER_REPO = "chrissgon/chrissgon"
LETTERS = ("A", "B", "C")
REPLIES = {
    "pick_ok": "Thanks, you picked {letter}, \"{topic}\", and it's counted. The chart in the README updates in a minute or two.",
    "pick_dup": "You already picked in this round, so your first pick stands. A new round opens every Monday.",
    "pick_closed": "There's no open round right now. A new one opens on a Monday, with the links in the README.",
    "pick_invalid": "I couldn't read a pick in this issue, so I closed it. Use the A, B or C links in the README to pick a topic.",
    "problem_ack": "Thanks for writing this up. I read every problem that comes in and pick some to build in public; the accepted ones show up in the README.",
}
MODES = {
    "dark": {"bg": "#000000", "text": "#FFFFFF", "accent": "#07B6F0", "muted": "#9CA3AF", "surface": "#1F2937"},
    "light": {"bg": "#FFFFFF", "text": "#000000", "accent": "#0092CD", "muted": "#6B7280", "surface": "#E5E7EB"},
}
SANS = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def md_safe(text, limit=100):
    """Plain text for Markdown: drop HTML and escape everything that could become a link or markup."""
    text = re.sub(r"<[^>]*>", "", text).replace("\n", " ").strip()[:limit]
    return re.sub(r"([\\`*_{}\[\]()<>#+!|~])", r"\\\1", text)


def picker_id(login, round_id):
    return hashlib.sha256(f"{round_id}:{login.lower()}".encode()).hexdigest()[:16]


# ---------- issue events ----------

def handle_issue(event, owner):
    """Return (actions, changed) for one issues event. actions: list of {"reply", "close"}."""
    issue, action = event.get("issue", {}), event.get("action")
    labels = {l["name"] for l in issue.get("labels", [])}
    user = issue.get("user", {})
    if user.get("type") == "Bot":
        return [], False
    if action == "opened" and "pick" in labels:
        return pick(issue)
    if action == "opened" and "problem" in labels:
        return [{"reply": REPLIES["problem_ack"], "close": False}], False
    if action == "labeled" and event.get("label", {}).get("name") in ("accepted", "delivered"):
        if event.get("sender", {}).get("login", "").lower() != owner.lower() or "problem" not in labels:
            return [], False
        return problem_status(issue, event["label"]["name"])
    return [], False


def pick(issue):
    v = load("pick.json")
    m = re.fullmatch(r"\s*pick:\s*([ABCabc])\s*", issue.get("title", ""))
    if not v.get("open"):
        return [{"reply": REPLIES["pick_closed"], "close": True}], False
    if not m:
        return [{"reply": REPLIES["pick_invalid"], "close": True}], False
    letter = m.group(1).upper()
    vid = picker_id(issue["user"]["login"], v["round"])
    if vid in v["picks"]:
        return [{"reply": REPLIES["pick_dup"], "close": True}], False
    v["picks"][vid] = letter
    save("pick.json", v)
    return [{"reply": REPLIES["pick_ok"].format(letter=letter, topic=v["options"][letter]), "close": True}], True


def problem_status(issue, status):
    items = load("problems.json")
    n = issue["number"]
    found = next((p for p in items if p["issue"] == n), None)
    if found is None:
        summary = re.sub(r"^\s*problem:\s*", "", issue.get("title", ""), flags=re.I)
        items.append({"issue": n, "summary": md_safe(summary), "status": status})
    else:
        found["status"] = status  # the summary stays frozen from the moment it was accepted
    save("problems.json", items)
    return [], True


# ---------- weekly ----------

def get_json(url, token=None):
    req = urllib.request.Request(url, headers={"User-Agent": "chrissgon-readme", "Accept": "application/vnd.github+json"})
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def measure(token=None):
    gh = "https://api.github.com/repos/chrissgon/ai-workbench"
    month = get_json("https://api.npmjs.org/downloads/point/last-month/@chrissgon/perfectui")
    skills = [x for x in get_json(f"{gh}/contents/skills", token) if x["type"] == "dir"]
    agents = [x for x in get_json(f"{gh}/contents/agents", token) if x["name"].endswith(".md")]
    last = get_json(f"{gh}/commits/main", token)
    return {"date": dt.date.today().isoformat(), "npm_month": month["downloads"],
            "npm_period": f'{month["start"]} to {month["end"]}', "skills": len(skills), "agents": len(agents),
            "last_sha": last["sha"][:7], "last_date": last["commit"]["committer"]["date"][:10]}


def rotate(today):
    """Close the current round if it has ended and open the next queued one."""
    v, queue = load("pick.json"), load("pick-queue.json")
    changed = False
    if v.get("open") and today >= v["closes"]:
        counts = {k: list(v["picks"].values()).count(k) for k in LETTERS}
        best = max(counts.values())
        winner = next(k for k in LETTERS if counts[k] == best) if best else None
        v["history"].insert(0, {"round": v["round"], "pillar": v["pillar"], "options": v["options"],
                                "counts": counts, "winner": winner, "post_url": None})
        v.update(open=False, picks={})
        changed = True
    if not v.get("open") and queue:
        nxt = queue.pop(0)
        start = dt.date.fromisoformat(today)
        v.update(open=True, round=today, closes=(start + dt.timedelta(days=7)).isoformat(),
                 pillar=nxt["pillar"], options=nxt["options"], picks={})
        save("pick-queue.json", queue)
        changed = True
    if changed:
        save("pick.json", v)
    return changed


# ---------- rendering ----------

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def fonts(svg):
    """Embed Inter and JetBrains Mono subsets (SIL OFL 1.1) holding only the characters used."""
    if os.environ.get("README_NO_FONTS"):
        return svg
    faces = []
    for fam, weight, marker in (("Inter", 800, 'font-weight="800"'), ("Inter", 600, 'font-weight="600"'),
                                ("JetBrains Mono", 400, "JetBrains Mono'")):
        chars = ""
        for attrs, body in re.findall(r"<text([^>]*)>(.*?)</text>", svg, re.S):
            if marker in attrs:
                chars += re.sub(r"<[^>]+>", "", body).replace("&lt;", "<").replace("&gt;", ">") \
                    .replace("&quot;", '"').replace("&amp;", "&")
        if not chars:
            continue
        q = urllib.parse.urlencode({"family": f"{fam}:wght@{weight}", "text": "".join(sorted(set(chars)))})
        ua = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://fonts.googleapis.com/css2?{q}", headers=ua), timeout=30) as r:
                url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", r.read().decode()).group(1)
            with urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30) as r:
                b64 = base64.b64encode(r.read()).decode()
        except Exception as e:  # keep the system-font fallback rather than failing the run
            print(f"readme.py: font {fam} {weight} not embedded: {e}", file=sys.stderr)
            continue
        faces.append(f"@font-face{{font-family:'{fam}';font-weight:{weight};src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    notice = ("<!-- Fonts: Inter (Copyright The Inter Project Authors) and JetBrains Mono (Copyright The JetBrains Mono "
              "Project Authors), subsets embedded under the SIL Open Font License 1.1, https://openfontlicense.org -->")
    return svg.replace("</title>", "</title>" + notice + f"<style>{''.join(faces)}</style>", 1)


def svg_doc(w, h, title, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{esc(title)}"><title>{esc(title)}</title>{body}</svg>\n')


def pick_card(c, letter, topic, count, total, is_open):
    w, h = 880, 76
    share = count / total if total else 0
    label = f"{count} pick" + ("" if count == 1 else "s")
    body = (f'<rect x="1" y="1" width="{w-2}" height="{h-2}" rx="12" fill="{c["bg"]}" stroke="{c["surface"]}" stroke-width="1.5"/>'
            f'<rect x="20" y="18" width="30" height="30" rx="6" fill="none" stroke="{c["text"]}" stroke-width="1.5"/>'
            f'<text x="35" y="39" text-anchor="middle" font-family="{MONO}" font-size="15" fill="{c["text"]}">{letter}</text>'
            f'<text x="68" y="39" font-family="{SANS}" font-size="18" font-weight="600" fill="{c["text"]}">{esc(topic)}</text>'
            f'<text x="{w-24}" y="39" text-anchor="end" font-family="{MONO}" font-size="13" fill="{c["muted"]}">{label}</text>'
            f'<rect x="68" y="56" width="{w-92}" height="4" rx="2" fill="{c["surface"]}"/>'
            f'<rect x="68" y="56" width="{(w-92)*share:.1f}" height="4" rx="2" fill="{c["accent"]}"/>')
    action = "Pick" if is_open else "Topic"
    return svg_doc(w, h, f"{action} {letter}: {topic}. {label}.", body)


def numbers_card(c, n):
    w, h = 880, 200
    body = (f'<rect x="1" y="1" width="{w-2}" height="{h-2}" rx="12" fill="{c["bg"]}" stroke="{c["surface"]}" stroke-width="1.5"/>'
            f'<text x="28" y="40" font-family="{MONO}" font-size="13" fill="{c["muted"]}">'
            f'<tspan fill="{c["accent"]}">agent&gt;</tspan> measured {esc(n["date"])} by scripts/readme.py</text>')
    cols = [(f'{n["npm_month"]:,}', "npm downloads, last 30 days", "@chrissgon/perfectui"),
            (str(n["skills"]), "skills in ai-workbench", f'{n["agents"]} agents'),
            (n["last_sha"], "last commit in ai-workbench", n["last_date"])]
    for i, (big, lab, sub) in enumerate(cols):
        x = 28 + i * 280
        body += (f'<text x="{x}" y="112" font-family="{SANS}" font-size="44" font-weight="800" letter-spacing="-1.3" fill="{c["text"]}">{esc(big)}</text>'
                 f'<text x="{x}" y="142" font-family="{MONO}" font-size="13" fill="{c["text"]}">{esc(lab)}</text>'
                 f'<text x="{x}" y="164" font-family="{MONO}" font-size="12" fill="{c["muted"]}">{esc(sub)}</text>')
    title = (f'Measured {n["date"]}: {n["npm_month"]} npm downloads of @chrissgon/perfectui in the last 30 days; '
             f'{n["skills"]} skills and {n["agents"]} agents in ai-workbench; last commit {n["last_sha"]} on {n["last_date"]}.')
    return svg_doc(w, h, title, body)


def picture(name, alt, width="100%", href=None):
    img = (f'<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.svg">\n'
           f'  <source media="(prefers-color-scheme: light)" srcset="assets/{name}-light.svg">\n'
           f'  <img alt="{esc(alt)}" src="assets/{name}-dark.svg" width="{width}">\n</picture>')
    return f'<a href="{href}">{img}</a>' if href else img


def issue_url(**q):
    return f"https://github.com/{OWNER_REPO}/issues/new?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def render():
    v, n = load("pick.json"), load("numbers.json")
    written = []
    def write(rel, text):
        p = ROOT / rel
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
            written.append(rel)
    # pick section
    counts = {k: list(v["picks"].values()).count(k) for k in LETTERS}
    total = sum(counts.values())
    if v.get("open"):
        cards = []
        for k in LETTERS:
            for mode, c in MODES.items():
                write(f"assets/pick-{k.lower()}-{mode}.svg", fonts(pick_card(c, k, v["options"][k], counts[k], total, True)))
            cards.append(picture(f"pick-{k.lower()}", f"Pick {k}: {v['options'][k]} ({counts[k]} so far)",
                                 href=issue_url(template="pick.yml", title=f"pick: {k}")))
        pick_md = (f"This week's slot is **{v['pillar']}**. Pick the topic I write next: one pick per GitHub account, "
                   f"and the round closes on {v['closes']}.\n\n" + "\n".join(cards))
    else:
        pick_md = "The next round opens on a Monday. Until then, the last result is below."
    last = next((h for h in v["history"] if h.get("winner")), None)
    if last:
        topic = last["options"][last["winner"]]
        pick_md += (f"\n\nLast round you picked **{topic}**. " +
                    (f"[I wrote it]({last['post_url']})." if last.get("post_url") else "I'm writing it now."))
    # problems
    probs = load("problems.json")
    if probs:
        prob_md = "\n".join(f"- [#{p['issue']}](https://github.com/{OWNER_REPO}/issues/{p['issue']}) {p['summary']} · {p['status']}"
                            for p in reversed(probs[-8:]))
    else:
        prob_md = "No accepted problems yet. Yours could be the first."
    # numbers
    if n:
        for mode, c in MODES.items():
            write(f"assets/numbers-{mode}.svg", fonts(numbers_card(c, n)))
        numbers_md = picture("numbers", f"Measured {n['date']}: {n['npm_month']} npm downloads of @chrissgon/perfectui in "
                             f"the last 30 days; {n['skills']} skills and {n['agents']} agents in ai-workbench.")
    else:
        numbers_md = ""
    posts = sorted(load("posts.json"), key=lambda p: p["date"], reverse=True)[:3]
    posts_md = "\n".join(f"- {p['date']} · [{md_safe(p['title'])}]({p['url']})" for p in posts)
    now_md = (DATA / "now.md").read_text(encoding="utf-8").strip()
    tpl = (ROOT / "scripts" / "README.template.md").read_text(encoding="utf-8")
    out = (tpl.replace("{{now}}", now_md).replace("{{pick}}", pick_md).replace("{{problems}}", prob_md)
              .replace("{{problem_url}}", issue_url(template="problem.yml"))
              .replace("{{numbers}}", numbers_md).replace("{{posts}}", posts_md))
    assert "{{" not in out, "unfilled placeholder in README.template.md"
    write("README.md", out)
    return written


# ---------- GitHub API ----------

class GitHub:
    def __init__(self):
        self.token, self.repo = os.environ["GITHUB_TOKEN"], os.environ.get("GITHUB_REPOSITORY", OWNER_REPO)

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"https://api.github.com/repos/{self.repo}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                                              "User-Agent": "chrissgon-readme", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r) if r.status != 204 else None

    def act(self, number, actions):
        for a in actions:
            self.call("POST", f"/issues/{number}/comments", {"body": a["reply"]})
            if a["close"]:
                self.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})

    def commit(self, paths, message, branch):
        """One commit with every path, made through the API so GitHub signs it."""
        head = self.call("GET", f"/git/ref/heads/{branch}")["object"]["sha"]
        base = self.call("GET", f"/git/commits/{head}")["tree"]["sha"]
        tree = [{"path": p, "mode": "100644", "type": "blob",
                 "sha": self.call("POST", "/git/blobs", {"content": base64.b64encode((ROOT / p).read_bytes()).decode(),
                                                         "encoding": "base64"})["sha"]} for p in paths]
        new_tree = self.call("POST", "/git/trees", {"base_tree": base, "tree": tree})["sha"]
        sha = self.call("POST", "/git/commits", {"message": message, "tree": new_tree, "parents": [head]})["sha"]
        self.call("PATCH", f"/git/refs/heads/{branch}", {"sha": sha})
        return sha


def finish(args, actions, data_changed, data_files, message, number=None):
    written = render() if data_changed else []
    paths = sorted(set(data_files if data_changed else []) | set(written))
    plan = {"actions": actions, "commit": paths, "message": message}
    if not args.apply:
        print(json.dumps(plan, indent=1, ensure_ascii=False))
        return
    gh = GitHub()
    if paths:
        gh.commit(paths, message, os.environ.get("README_BRANCH", "master"))
    if number is not None and actions:
        gh.act(number, actions)
    print(json.dumps(plan, indent=1, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("issue"); s.add_argument("--event", required=True); s.add_argument("--apply", action="store_true")
    s = sub.add_parser("weekly"); s.add_argument("--apply", action="store_true"); s.add_argument("--today")
    sub.add_parser("render")
    args = ap.parse_args()
    if args.cmd == "render":
        print(json.dumps(render(), indent=1))
    elif args.cmd == "issue":
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        owner = event.get("repository", {}).get("owner", {}).get("login", OWNER_REPO.split("/")[0])
        actions, changed = handle_issue(event, owner)
        number = event.get("issue", {}).get("number")
        finish(args, actions, changed, ["data/pick.json", "data/problems.json"], f"chore(readme): issue #{number}", number)
    elif args.cmd == "weekly":
        today = args.today or dt.date.today().isoformat()
        numbers = measure(os.environ.get("GITHUB_TOKEN"))
        save("numbers.json", numbers)
        rotate(today)
        finish(args, [], True, ["data/numbers.json", "data/pick.json", "data/pick-queue.json"], f"chore(readme): weekly update {today}")


if __name__ == "__main__":
    main()
