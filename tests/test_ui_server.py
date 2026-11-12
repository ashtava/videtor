import json
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from videtor.ui import server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "cache").mkdir()
        (cls.tmp / "out").mkdir()
        cls.app = server.App(cls.tmp, [sys.executable, "-c", "print('fake videtor')"], state_file=cls.tmp / "cache" / "ui-state.json")
        cls.httpd = server.serve(cls.app, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def post(self, path: str, body: dict, headers: dict | None = None):
        headers = {"Content-Type": "application/json", "X-Videtor-Token": self.app.token} if headers is None else headers
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_only_the_apps_own_page_may_change_anything(self):
        token = {"Content-Type": "application/json", "X-Videtor-Token": self.app.token}
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, {"Content-Type": "application/json"})[0], 403)  # no token
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, {**token, "X-Videtor-Token": "guess"})[0], 403)
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, {**token, "Content-Type": "text/plain"})[0], 403)  # a form post from elsewhere
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, {**token, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, {**token, "Host": "evil.example"})[0], 403)  # a name pointed at this Mac
        self.assertEqual(self.post("/api/validate", {"shotmap": "{}"}, token)[0], 200)
        req = urllib.request.Request(self.base + "/api/state", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn(f'content="{self.app.token}"', self.get("/")[2].decode())  # the page is handed the token

    def test_the_page_and_its_assets_are_served(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"VIDETOR", body)
        self.assertEqual(self.get("/static/app.js")[0], 200)
        self.assertEqual(self.get("/static/app.css")[0], 200)

    def test_state_lists_projects_jobs_and_drives(self):
        _, _, body = self.get("/api/state")
        s = json.loads(body)
        self.assertEqual(s["repo"], str(self.tmp.resolve()))
        self.assertIn("projects", s)
        self.assertIn("jobs", s)
        self.assertIsInstance(s["volumes"], list)

    def test_files_come_only_from_allowed_roots(self):
        secret = self.tmp / "secret.txt"
        secret.write_text("no")
        req = urllib.request.Request(self.base + "/file?path=" + str(secret))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)
        ok = self.tmp / "cache" / "still.txt"
        ok.write_text("yes")
        status, _, body = self.get("/file?path=" + str(ok))
        self.assertEqual((status, body), (200, b"yes"))

    def test_the_folder_browser_lists_folders_and_counts_videos(self):
        d = self.tmp / "shoot"
        (d / "Card 1").mkdir(parents=True)
        (d / ".hidden").mkdir()
        (d / "C0001.MP4").write_bytes(b"")
        _, _, body = self.get("/api/fs?path=" + str(d))
        r = json.loads(body)
        self.assertEqual(r["dirs"], ["Card 1"])
        self.assertEqual(r["videos"], 1)
        self.assertEqual(r["parent"], str(self.tmp))

    def test_a_project_is_saved_read_back_and_validated(self):
        p = self.app.add_project("demo", str(self.tmp / "shoot"), str(self.tmp / "out" / "demo"))
        status, r = self.post("/api/project/save", {"project": p["dir"], "brief": "A 20 s reel.", "shotmap": json.dumps({"version": 1, "length_s": 20, "classes": ["nope"], "sections": []})})
        self.assertEqual(status, 200)
        self.assertTrue(any("no slots" in e for e in r["errors"]))
        self.assertTrue(any("classes must be" in e for e in r["errors"]))
        _, _, body = self.get("/api/project?dir=" + str(p["dir"]))
        proj = json.loads(body)
        self.assertEqual(proj["brief"], "A 20 s reel.")
        self.assertEqual(proj["key"], "demo::footage")
        self.assertTrue(proj["errors"])
        status, r = self.post("/api/cut", {"project": p["dir"]})
        self.assertIn("problems", r["error"])
        status, r = self.post("/api/validate", {"shotmap": "{not json"})
        self.assertTrue(r["errors"][0].startswith("not valid JSON"))

    def test_prep_starts_a_job_and_registers_the_project(self):
        folder = self.tmp / "footage"
        folder.mkdir(exist_ok=True)
        status, r = self.post("/api/prep", {"folder": str(folder), "out": str(self.tmp / "out"), "name": "My Shoot"})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["project"]["name"], "my-shoot")
        self.assertEqual(r["project"]["dir"], str(self.tmp / "out" / "my-shoot"))
        self.assertEqual(r["job"]["kind"], "prep")
        self.assertEqual(r["job"]["args"][:2], ["prep", str(folder)])
        status, r2 = self.post("/api/prep", {"folder": str(self.tmp / "missing")})
        self.assertIn("not a folder", r2["error"])
        _, _, body = self.get(f"/api/jobs/{r['job']['id']}?since=0")
        self.assertEqual(json.loads(body)["job"]["id"], r["job"]["id"])

    def test_the_folder_dialog_is_the_macs_own_in_the_browser_too(self):
        from unittest import mock

        with mock.patch.object(server, "choose_folder_native", return_value="/Volumes/Drive/Shoot") as dialog:
            status, r = self.post("/api/pick-folder", {"start": "/Volumes", "prompt": "Choose the footage folder"})
        self.assertEqual((status, r), (200, {"path": "/Volumes/Drive/Shoot", "cancelled": False}))
        dialog.assert_called_once_with("Choose the footage folder", "/Volumes")
        with mock.patch.object(server, "choose_folder_native", return_value=None):
            status, r = self.post("/api/pick-folder", {})
        self.assertEqual(r, {"path": None, "cancelled": True})

    def test_a_project_says_what_comes_next(self):
        d = self.tmp / "out" / "steps"
        d.mkdir(parents=True, exist_ok=True)
        self.app.add_project("steps", str(self.tmp / "nowhere"), str(d))
        proj = lambda: json.loads(self.get("/api/project?dir=" + str(d))[2])  # noqa: E731
        p = proj()
        self.assertEqual([s["id"] for s in p["steps"]], ["overview", "brief", "tools", "cut", "look"])
        self.assertEqual([s["state"] for s in p["steps"]], ["todo", "blocked", "optional", "blocked", "blocked"])
        self.assertIn("Plug in the footage drive", p["next"]["text"])
        self.assertFalse(p["drive"]["connected"])
        (d / "overview.md").write_text("# steps")
        p = proj()
        self.assertEqual((p["steps"][0]["state"], p["next"]["tab"]), ("done", "brief"))
        self.assertIn("brief", p["next"]["text"])
        (d / "brief.md").write_text("A 20 s reel.")
        p = proj()
        self.assertIn("plan", p["next"]["text"])
        status = next(x["status"] for x in json.loads(self.get("/api/state")[2])["projects"] if x["dir"] == str(d))
        self.assertEqual(status, {"text": "Needs the plan", "tone": "todo"})

    def test_a_verdict_a_finished_video_and_a_swap_by_the_user_are_feedback(self):
        d = self.tmp / "out" / "judged"
        d.mkdir(parents=True, exist_ok=True)
        _, r = self.post("/api/project/add", {"dir": str(d)})
        d = Path(r["project"]["dir"])
        self.assertIn("no cut yet", self.post("/api/verdict", {"project": str(d), "good": True})[1]["error"])
        pick = {"path": "/V/a.mp4", "name": "a.mp4", "in_s": 0.0, "out_s": 1.0, "fid": "a"}
        (d / "picks.json").write_text(json.dumps({"version": 1, "name": "judged", "key": "judged::footage", "fps": 25, "width": 1080, "height": 1920,
                                                  "slots": [{"id": "s1", "show": "x", "start_s": 0.0, "length_s": 1.0, "pick": pick, "alternates": []}]}))
        self.assertEqual(self.post("/api/verdict", {"project": str(d), "good": False, "note": "too slow"})[1], {"ok": True})
        self.assertEqual(json.loads((self.tmp / "kb" / "feedback" / "judged.json").read_text())["verdict"]["note"], "too slow")
        self.assertIn("No finished video", self.post("/api/readback", {"project": str(d)})[1]["error"])
        (d / "preview.mp4").write_bytes(b"x")  # Videtor's own preview is not the finished job
        self.assertIn("No finished video", self.post("/api/readback", {"project": str(d)})[1]["error"])
        (d / "final export.mp4").write_bytes(b"x")
        _, r = self.post("/api/readback", {"project": str(d)})
        self.assertEqual(r["job"]["args"], ["readback", str(d / "picks.json"), "--render", str(d / "final export.mp4"), "--learn"])
        _, r = self.post("/api/look", {"project": str(d), "choices": {"s1": 1}})
        self.assertEqual(r["job"]["args"][:4], ["look", str(d / "picks.json"), "--by", "you"])  # a person chose: it teaches
        fb = json.loads(self.get(f"/api/project?dir={d}")[2])["feedback"]
        self.assertEqual((fb["verdict"]["good"], fb["final"]), (False, str(d / "final export.mp4")))

    def test_the_teach_page_takes_every_input_and_says_what_is_known(self):
        (self.tmp / "kb").mkdir(exist_ok=True)
        (self.tmp / "kb" / "manifest.json").write_text(json.dumps({"types": {"Old.xml::Seq": "event"}, "footage": {"Old.xml::Seq": ["/Volumes/Gone/shoot"]}}))
        t = json.loads(self.get("/api/teach")[2])
        self.assertEqual(t["kinds"][0], "event")
        ev = next(b for b in t["buckets"] if b["bucket"] == "event")
        self.assertEqual((ev["edits"][0]["name"], ev["edits"][0]["drive"], ev["edits"][0]["connected"]), ("Old", "Gone", False))
        self.assertEqual(self.post("/api/teach/rule", {"bucket": "event", "text": "People first."})[1], {"rules": ["People first."]})
        self.assertEqual(self.post("/api/teach/rule", {"bucket": "event", "text": "People first.", "remove": True})[1], {"rules": []})
        self.assertIn("words", self.post("/api/teach/rule", {"bucket": "event", "text": " "})[1]["error"])
        # a job cut by hand: a name, a kind of work, and the finished video or the XML
        self.assertIn("name", self.post("/api/teach/project", {"bucket": "event"})[1]["error"])
        self.assertIn("finished video or the Premiere XML", self.post("/api/teach/project", {"name": "Nike Launch", "bucket": "event"})[1]["error"])
        self.assertIn("not there", self.post("/api/teach/project", {"name": "Nike Launch", "bucket": "event", "video": str(self.tmp / "no.mp4")})[1]["error"])
        video, footage = self.tmp / "final.mp4", self.tmp / "footage"
        video.write_bytes(b"x")
        footage.mkdir(exist_ok=True)
        _, r = self.post("/api/teach/project", {"name": "Nike Launch", "bucket": "Brand Social", "video": str(video), "footage": str(footage)})
        self.assertEqual(r["job"]["args"], ["teach", "project", "nike-launch", "--bucket", "brand-social", "--footage", str(footage), "--video", str(video)])
        self.assertEqual(r["job"]["kind"], "teach")
        self.assertIn("folder of finished videos", self.post("/api/teach/style", {"bucket": "event"})[1]["error"])
        self.assertEqual(self.post("/api/teach/style", {"source": str(footage), "bucket": "event"})[1]["job"]["args"], ["teach", "style", str(footage), "--bucket", "event"])
        self.assertEqual(self.post("/api/teach/edit", {"key": "Old.xml::Seq"})[1]["job"]["args"], ["teach", "edit", "Old.xml::Seq"])
        self.assertEqual(self.post("/api/teach/pending", {})[1]["job"]["args"][:3], ["teach", "pending", "--out"])

    def test_projects_made_elsewhere_are_listed_and_each_kind_of_cut_runs_its_own_command(self):
        out = self.tmp / "out"
        for name, files in (("an-mv", ["mv.json"]), ("a-talk", ["aroll.json", "shotmap.json"]), ("library", ["picks.json"]), ("empty", [])):
            (out / name).mkdir(parents=True, exist_ok=True)
            for f in files:
                (out / name / f).write_text(json.dumps({"version": 1, "name": name, "key": f"{name}::footage", "length_s": 10,
                                                        "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "beats": 1}]}]}))
        self.app.out_root = out
        self.assertGreaterEqual(self.app.discover(), 2)
        self.assertEqual(self.app.discover(), 0)
        dirs = {p["name"]: p["dir"] for p in json.loads(self.get("/api/state")[2])["projects"]}
        self.assertTrue({"an-mv", "a-talk"} <= set(dirs))
        self.assertFalse({"library", "empty"} & set(dirs))  # not the library's folder, not a folder with nothing in it
        self.assertEqual(self.post("/api/cut", {"project": dirs["an-mv"]})[1]["job"]["args"], ["cut-mv", str(Path(dirs["an-mv"]) / "mv.json")])
        self.assertEqual(self.post("/api/cut", {"project": dirs["a-talk"]})[1]["job"]["args"],
                         ["cut-interview", str(Path(dirs["a-talk"]) / "aroll.json"), str(Path(dirs["a-talk"]) / "shotmap.json")])
        self.assertIn("no cut yet", self.post("/api/preview", {"project": dirs["a-talk"]})[1]["error"])
        (Path(dirs["a-talk"]) / "picks.json").write_text(json.dumps({"name": "a-talk", "slots": []}))
        self.assertEqual(self.post("/api/preview", {"project": dirs["a-talk"]})[1]["job"]["args"], ["preview", str(Path(dirs["a-talk"]) / "picks.json")])
        self.assertIn("which way", self.post("/api/orient", {"project": dirs["a-talk"], "which": "M2U", "rotate": 45})[1]["error"])
        _, r = self.post("/api/orient", {"project": dirs["a-talk"], "which": "M2U", "rotate": -90})
        self.assertEqual(r["job"]["args"][:6], ["orient", "a-talk", "--rotate", "-90", "--camera", "M2U"])
        self.assertEqual(self.post("/api/orient", {"project": dirs["a-talk"], "which": "C0123.MP4", "rotate": 180})[1]["job"]["args"][4:6], ["--take", "C0123.MP4"])
        proj = json.loads(self.get(f"/api/project?dir={dirs['a-talk']}")[2])
        self.assertEqual((proj["kind"], proj["preview"], proj["finishing"]), ("interview", None, []))

    def test_scan_counts_the_video_files_under_a_folder(self):
        d = self.tmp / "scan" / "Card 1"
        d.mkdir(parents=True)
        (d / "C0001.MP4").write_bytes(b"x" * 10)
        (d / "C0001M01.XML").write_text("x")
        (self.tmp / "scan" / ".hidden.mp4").write_bytes(b"")
        r = json.loads(self.get("/api/scan?path=" + str(self.tmp / "scan"))[2])
        self.assertEqual((r["videos"], r["partial"]), (1, False))
        self.assertIsNone(r["drive"]["name"])
        self.assertTrue(r["drive"]["connected"])

    def test_unknown_routes_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/api/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
