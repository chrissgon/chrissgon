"""Tests for scripts/readme.py. Run: python3 -m unittest discover -s scripts -p "test_*.py"
Each test works on a temporary copy of the repository, so data/ and README.md stay untouched."""
import json
import re
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

    def test_new_letter_from_same_account_replaces_the_pick(self):
        self.handle(issue_event("opened", "pick: A", login="Ana"))
        actions, changed = self.handle(issue_event("opened", "pick: C", login="ana"))
        self.assertTrue(changed)
        self.assertIn("changed from A to C", actions[0]["reply"])
        self.assertEqual(list(readme.load("pick.json")["picks"].values()), ["C"])

    def test_same_letter_again_changes_nothing(self):
        self.handle(issue_event("opened", "pick: A", login="ana"))
        actions, changed = self.handle(issue_event("opened", "pick: a", login="Ana"))
        self.assertFalse(changed)
        self.assertEqual(actions[0]["reply"], readme.REPLIES["pick_same"].format(letter="A"))
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


UTC = readme.dt.timezone.utc


def at(stamp):
    return readme.dt.datetime.fromisoformat(stamp).replace(tzinfo=UTC)


class FakeAPI:
    def __init__(self, issues=(), comments=None):
        self.issues, self.comments = list(issues), comments or {}

    def open_issues(self, label):
        return [i for i in self.issues if label in {l["name"] for l in i["labels"]}]

    def acknowledged(self, number, text):
        return text in self.comments.get(number, [])


def open_issue(number, title, login="visitor", label="pick", user_type="User"):
    return {"number": number, "title": title, "user": {"login": login, "type": user_type}, "labels": [{"name": label}]}


class Sweep(Base):
    def setUp(self):
        super().setUp()
        readme.save("numbers.json", {"date": "2026-09-29", "npm_month": 1, "npm_period": "x", "skills": 1,
                                     "agents": 1, "last_sha": "0000000", "last_date": "2026-09-29"})

    def sweep(self, api, now="2026-09-30T05:00:00"):
        return readme.sweep(api, "chrissgon", at(now), measure_fn=lambda token: self.fail("measured too early"))

    def test_picks_apply_in_creation_order_whatever_the_listing_order(self):
        api = FakeAPI([open_issue(11, "pick: B", "ana"), open_issue(10, "pick: A", "ana")])
        replies, changed = self.sweep(api)
        self.assertEqual([n for n, _ in replies], [10, 11])
        self.assertIn("changed from A to B", replies[1][1][0]["reply"])
        self.assertEqual(list(readme.load("pick.json")["picks"].values()), ["B"])
        self.assertEqual(changed, {"data/pick.json"})

    def test_a_missed_run_is_caught_by_the_next(self):
        api = FakeAPI([open_issue(1, "pick: A", "a"), open_issue(2, "pick: C", "b"), open_issue(3, "pick: C", "c")])
        self.sweep(api)
        counts = list(readme.load("pick.json")["picks"].values())
        self.assertEqual(sorted(counts), ["A", "C", "C"])

    def test_problem_is_acknowledged_once(self):
        api = FakeAPI([open_issue(5, "problem: x", label="problem")])
        replies, _ = self.sweep(api)
        self.assertEqual(replies, [(5, [{"reply": readme.REPLIES["problem_ack"], "close": False}])])
        api.comments = {5: [readme.REPLIES["problem_ack"]]}
        self.assertEqual(self.sweep(api)[0], [])

    def test_bot_issues_are_left_alone(self):
        replies, changed = self.sweep(FakeAPI([open_issue(9, "pick: A", user_type="Bot")]))
        self.assertEqual((replies, changed), ([], set()))

    def test_numbers_are_measured_after_a_week(self):
        seen = []
        readme.sweep(FakeAPI(), "chrissgon", at("2026-10-06T13:00:00"),
                     measure_fn=lambda token: seen.append(1) or {"date": "2026-10-06", "npm_month": 2, "npm_period": "x",
                                                                 "skills": 1, "agents": 1, "last_sha": "1", "last_date": "x"})
        self.assertEqual(seen, [1])


class Rounds(Base):
    def test_round_closes_on_monday_at_noon_utc_and_next_opens(self):
        self.handle(issue_event("opened", "pick: C", login="a"))
        self.handle(issue_event("opened", "pick: C", login="b"))
        self.handle(issue_event("opened", "pick: A", login="c"))
        readme.save("pick-queue.json", [{"pillar": "Tech in conversation", "options": {"A": "x", "B": "y", "C": "z"}}])
        self.assertFalse(readme.rotate(at("2026-10-05T11:59:00")))
        self.assertTrue(readme.rotate(at("2026-10-05T12:00:00")))
        v = readme.load("pick.json")
        self.assertEqual(v["history"][0]["winner"], "C")
        self.assertEqual(v["history"][0]["counts"], {"A": 1, "B": 0, "C": 2})
        self.assertEqual((v["open"], v["round"], v["closes"], v["pillar"], v["picks"]),
                         (True, "2026-10-05", "2026-10-12", "Tech in conversation", {}))
        self.assertEqual(readme.load("pick-queue.json"), [])

    def test_empty_queue_leaves_round_closed(self):
        readme.save("pick-queue.json", [])
        readme.rotate(at("2026-10-05T12:00:00"))
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

    def test_linked_pictures_stay_on_one_line(self):
        readme.render()
        for line in (self.tmp / "README.md").read_text().splitlines():
            if line.startswith("<a href="):
                self.assertTrue(line.endswith("</a>"), line[:60])

    def test_image_address_changes_when_the_card_changes(self):
        readme.render()
        before = re.search(r'srcset="(assets/pick-b-dark\.svg\?v=[0-9a-f]{8})"', (self.tmp / "README.md").read_text()).group(1)
        self.handle(issue_event("opened", "pick: B", login="someone"))
        readme.render()
        after = re.search(r'srcset="(assets/pick-b-dark\.svg\?v=[0-9a-f]{8})"', (self.tmp / "README.md").read_text()).group(1)
        self.assertNotEqual(before, after)
        readme.render()
        self.assertIn(after, (self.tmp / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
