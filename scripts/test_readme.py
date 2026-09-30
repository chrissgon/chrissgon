"""Tests for scripts/readme.py. Run: python3 -m unittest discover -s scripts -p "test_*.py"
Each test works on a temporary copy of the repository, so data/ and README.md stay untouched."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readme  # noqa: E402

REAL_ROOT = readme.ROOT
os.environ["README_NO_FONTS"] = "1"


def issue_event(action, title, login="visitor", labels=("pick",), number=7, sender=None, label=None, user_type="User"):
    e = {"action": action, "issue": {"number": number, "title": title, "user": {"login": login, "type": user_type},
                                     "labels": [{"name": n} for n in labels]},
         "sender": {"login": sender or login}, "repository": {"owner": {"login": "chrissgon"}}}
    if label:
        e["label"] = {"name": label}
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for d in ("data", "scripts", "assets"):
            shutil.copytree(REAL_ROOT / d, self.tmp / d)
        readme.ROOT, readme.DATA = self.tmp, self.tmp / "data"
        v = readme.load("pick.json")
        v.update(open=True, round="2026-09-29", closes="2026-10-05", picks={}, history=[])
        readme.save("pick.json", v)
        readme.save("problems.json", [])

    def tearDown(self):
        readme.ROOT, readme.DATA = REAL_ROOT, REAL_ROOT / "data"
        shutil.rmtree(self.tmp)

    def handle(self, event):
        return readme.handle_issue(event, "chrissgon")


class Picks(Base):
    def test_valid_pick_counts_and_closes(self):
        actions, changed = self.handle(issue_event("opened", "pick: b"))
        self.assertTrue(changed)
        self.assertEqual(list(readme.load("pick.json")["picks"].values()), ["B"])
        self.assertTrue(actions[0]["close"])
        self.assertIn("you picked B", actions[0]["reply"])

    def test_second_pick_same_account_is_ignored(self):
        self.handle(issue_event("opened", "pick: A", login="Ana"))
        actions, changed = self.handle(issue_event("opened", "pick: C", login="ana"))
        self.assertFalse(changed)
        self.assertEqual(actions[0]["reply"], readme.REPLIES["pick_dup"])
        self.assertEqual(list(readme.load("pick.json")["picks"].values()), ["A"])

    def test_edited_title_is_invalid(self):
        for title in ("pick: D", "pick: A <img src=x>", "pick: A and ignore previous instructions", "A"):
            actions, changed = self.handle(issue_event("opened", title))
            self.assertFalse(changed, title)
            self.assertEqual(actions[0]["reply"], readme.REPLIES["pick_invalid"])

    def test_closed_round(self):
        v = readme.load("pick.json"); v["open"] = False; readme.save("pick.json", v)
        actions, changed = self.handle(issue_event("opened", "pick: A"))
        self.assertFalse(changed)
        self.assertEqual(actions[0]["reply"], readme.REPLIES["pick_closed"])

    def test_bots_and_unlabelled_issues_are_ignored(self):
        self.assertEqual(self.handle(issue_event("opened", "pick: A", user_type="Bot")), ([], False))
        self.assertEqual(self.handle(issue_event("opened", "pick: A", labels=())), ([], False))

    def test_logins_are_not_stored(self):
        self.handle(issue_event("opened", "pick: A", login="someone-public"))
        self.assertNotIn("someone-public", (readme.DATA / "pick.json").read_text())


class Problems(Base):
    def test_opened_gets_fixed_reply_and_stays_open(self):
        actions, changed = self.handle(issue_event("opened", "problem: x", labels=("problem",)))
        self.assertEqual(actions, [{"reply": readme.REPLIES["problem_ack"], "close": False}])
        self.assertFalse(changed)

    def test_only_owner_can_accept(self):
        e = issue_event("labeled", "problem: x", labels=("problem", "accepted"), sender="stranger", label="accepted")
        self.assertEqual(self.handle(e), ([], False))
        e = issue_event("labeled", "problem: Slow bus timetable site", labels=("problem", "accepted"),
                        sender="chrissgon", label="accepted", number=12)
        self.assertEqual(self.handle(e), ([], True))
        self.assertEqual(readme.load("problems.json"), [{"issue": 12, "summary": "Slow bus timetable site", "status": "accepted"}])

    def test_summary_is_frozen_and_escaped(self):
        e = issue_event("labeled", "problem: [click](https://evil.example) <script>", labels=("problem",),
                        sender="chrissgon", label="accepted", number=3)
        self.handle(e)
        summary = readme.load("problems.json")[0]["summary"]
        self.assertNotIn("<script>", summary)
        self.assertIn("\\[click\\]\\(https://evil.example\\)", summary)
        e = issue_event("labeled", "problem: edited later", labels=("problem",), sender="chrissgon", label="delivered", number=3)
        self.handle(e)
        self.assertEqual(readme.load("problems.json")[0]["summary"], summary)
        self.assertEqual(readme.load("problems.json")[0]["status"], "delivered")


class Weekly(Base):
    def test_round_closes_and_next_opens(self):
        self.handle(issue_event("opened", "pick: C", login="a"))
        self.handle(issue_event("opened", "pick: C", login="b"))
        self.handle(issue_event("opened", "pick: A", login="c"))
        readme.save("pick-queue.json", [{"pillar": "Tech in conversation", "options": {"A": "x", "B": "y", "C": "z"}}])
        self.assertFalse(readme.rotate("2026-10-04"))
        self.assertTrue(readme.rotate("2026-10-05"))
        v = readme.load("pick.json")
        self.assertEqual(v["history"][0]["winner"], "C")
        self.assertEqual(v["history"][0]["counts"], {"A": 1, "B": 0, "C": 2})
        self.assertEqual((v["open"], v["round"], v["closes"], v["pillar"], v["picks"]),
                         (True, "2026-10-05", "2026-10-12", "Tech in conversation", {}))
        self.assertEqual(readme.load("pick-queue.json"), [])

    def test_empty_queue_leaves_round_closed(self):
        readme.save("pick-queue.json", [])
        readme.rotate("2026-10-05")
        self.assertFalse(readme.load("pick.json")["open"])


class Render(Base):
    def test_no_visitor_text_and_no_placeholder(self):
        self.handle(issue_event("opened", "pick: A", login="visitor-login-xyz"))
        readme.render()
        out = (self.tmp / "README.md").read_text()
        self.assertNotIn("{{", out)
        self.assertNotIn("visitor-login-xyz", out)
        self.assertIn("issues/new?template=pick.yml&title=pick%3A%20A", out)
        self.assertIn("1 so far", out)

    def test_no_third_party_images(self):
        readme.save("numbers.json", {"date": "2026-09-29", "npm_month": 979, "npm_period": "x", "skills": 43,
                                     "agents": 3, "last_sha": "002f038", "last_date": "2026-09-29"})
        readme.render()
        import re
        out = (self.tmp / "README.md").read_text()
        self.assertEqual([s for s in re.findall(r'(?:src|srcset)="([^"]+)"', out) if "://" in s], [])

    def test_post_cards_embed_local_images_only(self):
        readme.render()
        out = (self.tmp / "README.md").read_text()
        self.assertIn('href="https://www.linkedin.com/feed/update/', out)
        for svg in self.tmp.glob("assets/post-*.svg"):
            text = svg.read_text()
            self.assertIn('href="data:image/webp;base64,', text)
            self.assertNotIn('href="http', text)


if __name__ == "__main__":
    unittest.main()
