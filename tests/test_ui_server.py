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

    def test_the_shape_and_the_music_are_the_users_to_set_for_a_project(self):
        from videtor.plan import settings

        footage = self.tmp / "footage-h"
        footage.mkdir(exist_ok=True)
        _, r = self.post("/api/prep", {"folder": str(footage), "out": str(self.tmp / "out"), "name": "wide one", "frame": "horizontal"})
        d = Path(r["project"]["dir"])
        self.assertEqual(settings.load(d), {"frame": "horizontal"})  # a new project is made with its shape
        self.assertIn("frame must be", self.post("/api/prep", {"folder": str(footage), "out": str(self.tmp / "out"), "name": "odd", "frame": "round"})[1]["error"])
        req = json.loads(self.get(f"/api/project?dir={d}")[2])["requirements"]
        self.assertEqual((req["mine"], req["says"]["frame"], req["plan"]), ({"frame": "horizontal"}, "Horizontal 16:9", None))
        self.assertEqual([k for k, _ in req["frames"]], ["vertical", "portrait", "horizontal", "horizontal-4k", "square"])
        # the user's own track: it has to be there and be audio; a start "at" needs its second
        self.assertIn("not there", self.post("/api/settings", {"project": str(d), "music": {"path": str(self.tmp / "no.wav")}})[1]["error"])
        (self.tmp / "notes.txt").write_text("x")
        self.assertIn("audio file", self.post("/api/settings", {"project": str(d), "music": {"path": str(self.tmp / "notes.txt")}})[1]["error"])
        track = self.tmp / "song.wav"
        track.write_bytes(b"")
        beats = self.tmp / "cache" / "beats"
        beats.mkdir(parents=True, exist_ok=True)
        (beats / "song.beats.json").write_text(json.dumps({"path": str(track), "duration_s": 60.0, "tempo_bpm": 120.0, "beats_s": [k * 0.5 for k in range(120)],
                                                            "rms_db": [-8.0] * 120, "onset_half_s": [1.0] * 120}))
        self.assertIn("second of the track", self.post("/api/settings", {"project": str(d), "music": {"path": str(track), "start": "at"}})[1]["error"])
        _, r = self.post("/api/settings", {"project": str(d), "music": {"path": str(track), "start": "at", "in_s": 12}})
        self.assertEqual(r["requirements"]["mine"], {"frame": "horizontal", "music": {"path": str(track), "start": "at", "in_s": 12.0}})
        self.assertEqual(r["requirements"]["says"]["music"], "song.wav, from 12 s")
        # with a plan written for vertical on beats, the page is told what the cut will be
        m = {"version": 1, "name": "wide-one", "key": "wide-one::footage", "length_s": 4.0, "width": 1080, "height": 1920,
             "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "beats": 4}, {"id": "s-2", "show": "y", "beats": 4}]}]}
        self.post("/api/project/save", {"project": str(d), "shotmap": json.dumps(m)})
        req = json.loads(self.get(f"/api/project?dir={d}")[2])["requirements"]
        self.assertEqual((req["plan"]["frame"], req["cut"]["width"], req["cut"]["height"], req["cut"]["music"], req["cut"]["in_s"]), ("vertical", 1920, 1080, "song.wav", 12.0))
        self.assertTrue(any("as you chose" in n for n in req["notes"]))
        _, r = self.post("/api/settings", {"project": str(d), "music": {"none": True}, "frame": "square"})
        self.assertEqual((r["requirements"]["mine"], r["requirements"]["cut"]["music"]), ({"frame": "square", "music": {"none": True}}, None))
        _, r = self.post("/api/settings", {"project": str(d), "music": None})
        self.assertEqual(r["requirements"]["mine"], {"frame": "square"})  # back to the plan's music
        self.assertIn("not a folder", self.post("/api/music/ranked", {"folder": str(self.tmp / "nope")})[1]["error"])
        self.assertEqual(self.post("/api/music/ranked", {"folder": str(footage), "length": 10})[1], {"tracks": []})

    def test_media_is_served_in_the_byte_ranges_a_player_asks_for(self):
        f = self.tmp / "out" / "clip.bin"
        f.write_bytes(bytes(range(200)))
        url = self.base + "/file?path=" + str(f)

        def ranged(spec):
            req = urllib.request.Request(url, headers={"Range": spec})
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Range"), r.read()

        with urllib.request.urlopen(url) as r:
            self.assertEqual((r.status, r.headers.get("Accept-Ranges"), len(r.read())), (200, "bytes", 200))
        self.assertEqual(ranged("bytes=10-19"), (206, "bytes 10-19/200", bytes(range(10, 20))))
        self.assertEqual(ranged("bytes=190-"), (206, "bytes 190-199/200", bytes(range(190, 200))))
        self.assertEqual(ranged("bytes=-5"), (206, "bytes 195-199/200", bytes(range(195, 200))))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            ranged("bytes=500-600")
        self.assertEqual(ctx.exception.code, 416)

    def test_the_timeline_is_drawn_edited_and_noted_through_the_server(self):
        d = self.tmp / "out" / "timed"
        d.mkdir(parents=True, exist_ok=True)
        _, r = self.post("/api/project/add", {"dir": str(d)})
        d = Path(r["project"]["dir"])
        self.assertIn("no cut yet", json.loads(self.get(f"/api/timeline?dir={d}")[2])["error"])
        shot = lambda fid, a: {"fid": fid, "path": f"/V/{fid}.mp4", "name": f"{fid}.mp4", "in_s": a, "out_s": a + 1.0, "speed": 1.0, "duration_s": 10.0, "file_fps": 25.0}  # noqa: E731
        slots = [{"id": "s1", "section": "one", "show": "x", "start_s": 0.0, "length_s": 1.0, "pick": shot("a", 2.0), "alternates": [shot("c", 1.0)]},
                 {"id": "s2", "section": "one", "show": "y", "start_s": 1.0, "length_s": 1.0, "pick": shot("b", 3.0), "alternates": []}]
        (d / "picks.json").write_text(json.dumps({"version": 1, "name": "timed", "key": "timed::footage", "fps": 25.0, "width": 1080, "height": 1920, "slots": slots}))
        tl = json.loads(self.get(f"/api/timeline?dir={d}")[2])
        self.assertEqual((len(tl["clips"]), tl["length_s"], tl["missing"], tl["can_undo"], tl["music"]), (2, 2.0, 2, False, []))
        _, r = self.post("/api/edit", {"project": str(d), "op": "roll", "slot": "s1", "frames": 5})
        self.assertEqual(r["line"], "the cut after s1 moved +5 frame(s)")
        self.assertEqual((r["timeline"]["slots"][0]["length_s"], r["timeline"]["can_undo"]), (1.2, True))
        self.assertIn("no such shot", self.post("/api/edit", {"project": str(d), "op": "slip", "slot": "zz", "frames": 1})[1]["error"])
        _, r = self.post("/api/edit", {"project": str(d), "op": "swap", "slot": "s1", "k": 1})
        self.assertEqual(r["timeline"]["slots"][0]["name"], "c.mp4")
        self.post("/api/edit", {"project": str(d), "op": "undo"})
        _, r = self.post("/api/edit", {"project": str(d), "op": "undo"})
        self.assertEqual((r["timeline"]["slots"][0]["length_s"], r["timeline"]["slots"][0]["name"], r["timeline"]["can_undo"]), (1.0, "a.mp4", False))
        self.assertEqual(self.post("/api/note", {"project": str(d), "slot": "s2", "text": "closer"})[1], {"ok": True})
        self.assertEqual(json.loads(self.get(f"/api/timeline?dir={d}")[2])["slots"][1]["note"], "closer")
        self.assertEqual(json.loads(self.get(f"/api/options?dir={d}&slot=s1")[2])["rows"], [])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + f"/media?dir={d}&i=0")  # the cut has no music: nothing outside the project is served
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(self.post("/api/write-xml", {"project": str(d)})[1]["job"]["args"], ["write-xml", str(d / "picks.json")])
        self.assertEqual(self.post("/api/proxies", {"project": str(d), "slots": ["s1"], "alternates": True})[1]["job"]["args"],
                         ["proxies", str(d / "picks.json"), "--slot", "s1", "--alternates"])
        self.assertEqual(self.post("/api/options", {"project": str(d), "slot": "s1", "show": " a dog "})[1]["job"]["args"], ["options", str(d / "picks.json"), "s1", "--show", "a dog"])

    def test_one_file_is_chosen_with_the_windows_own_dialog_when_there_is_a_window(self):
        from unittest import mock

        window = mock.Mock()
        window.create_file_dialog.return_value = ("/Users/me/Music/song.wav",)
        self.app.window = window
        try:
            self.assertEqual(self.post("/api/pick-file", {"kind": "audio", "start": str(self.tmp)})[1], {"path": "/Users/me/Music/song.wav", "cancelled": False})
            self.assertIn("*.wav", window.create_file_dialog.call_args.kwargs["file_types"][0])
            window.create_file_dialog.return_value = None
            self.assertEqual(self.post("/api/pick-file", {"kind": "video"})[1], {"path": None, "cancelled": True})
        finally:
            self.app.window = None

    def test_promos_are_asked_for_listed_and_swapped_through_the_server(self):
        from unittest import mock

        from videtor.analyze import link

        got = json.loads(self.get("/api/promos")[2])
        self.assertEqual((got["sets"], [k for k, _ in got["frames"]]), ([], ["vertical", "portrait", "horizontal", "square"]))
        self.assertIn("Paste the video's link", self.post("/api/promos/make", {"source": " "})[1]["error"])
        self.assertIn("There is no video at", self.post("/api/promos/make", {"source": "/nowhere/talk.mp4"})[1]["error"])
        with mock.patch.object(link, "available", return_value=None):
            self.assertIn("needs yt-dlp", self.post("/api/promos/make", {"source": "https://youtu.be/abc"})[1]["error"])
        root = self.tmp / "out" / "promos"
        with mock.patch.object(link, "available", return_value="2026.10.26"):
            job = self.post("/api/promos/make", {"source": "https://www.youtube.com/watch?v=abc", "frame": "square", "kind": "edit", "count": 2, "length": 15,
                                                 "browser": "chrome"})[1]["job"]
        self.assertEqual((job["kind"], job["title"]), ("promos", "Promos · youtube.com"))
        self.assertEqual(job["args"], ["promos", "https://www.youtube.com/watch?v=abc", "--frame", "square", "--kind", "edit", "--count", "2", "--length", "15",
                                       "--lead", "auto", "--fit", "fill", "--out", str(root), "--browser", "chrome"])
        f = self.tmp / "out" / "talk.mp4"
        f.write_bytes(b"x")
        job = self.post("/api/promos/make", {"source": str(f), "count": 99, "browser": "netscape"})[1]["job"]
        self.assertEqual((job["title"], job["args"][2:8], "--browser" in job["args"]), ("Promos · talk.mp4", ["--frame", "vertical", "--kind", "clip", "--count", "6"], False))
        self.assertIn("not one Videtor knows", self.post("/api/promos/make", {"source": str(f), "frame": "round"})[1]["error"])
        d = root / "a-talk" / "clips-vertical-30s"  # a video's folder, and one making in it
        d.mkdir(parents=True)
        row = {"n": 1, "file": str(d / "promo-1.mp4"), "xml": str(d / "promo-1.xml"), "length_s": 28.4, "kind": "clip", "at": "2:10", "reasons": ["ends on a full stop"],
               "text": "Hello.", "shots": 1, "made_at": "2026-11-16T19:00:00", "score": 2.0}
        (d / "promos.json").write_text(json.dumps({"name": "a-talk", "made_at": "2026-11-16T19:00:00", "source": {"title": "A talk", "path": "/V/a.mp4", "duration_s": 600.0},
                                                   "options": {"count": 1, "length": 30.0, "frame": "vertical", "kind": "clip", "lead": "auto", "fit": "fill"},
                                                   "lead": "speech", "said": 0.8, "notes": [], "promos": [row], "candidates": [{}, {}, {}], "used": [0]}))
        old = root / "an-old-one"  # a set from before sets had folders of their own: in the video's folder itself
        old.mkdir()
        (old / "promos.json").write_text(json.dumps({**json.loads((d / "promos.json").read_text()), "name": "an-old-one", "made_at": "2026-11-16T18:00:00",
                                                     "options": {"count": 1, "length": 30.0, "frame": "square", "kind": "edit", "lead": "auto", "fit": "fill"}}))
        sets = json.loads(self.get("/api/promos")[2])["sets"]
        one = sets[0]
        self.assertEqual((one["title"], one["more"], one["promos"][0]["at"], "score" in one["promos"][0], "candidates" in one), ("A talk", 2, "2:10", False, False))
        self.assertEqual([(x["dir"], x["options"]["kind"]) for x in sets], [(str(d.resolve()), "clip"), (str(old.resolve()), "edit")])  # every making, the newest first
        self.assertEqual(len({x["video"] for x in sets}), 1)  # and known as sets of one video, whichever folder they lie in
        for folder in (d, old):
            self.assertEqual(self.post("/api/promos/another", {"dir": str(folder), "n": 1})[1]["job"]["args"], ["promos", str(folder), "--another", "1"])
        self.assertIn("no promos there", self.post("/api/promos/another", {"dir": str(self.tmp / "out"), "n": 1})[1]["error"])
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "promos.json").write_text("{}")
        self.assertIn("no promos there", self.post("/api/promos/another", {"dir": str(elsewhere), "n": 1})[1]["error"])  # only folders of promos
        self.assertEqual(self.post("/api/promos/update", {})[1]["job"]["args"], ["promos", "--update"])

    def test_a_shoot_sorted_in_the_terminal_is_listed_on_the_library_page(self):
        lib = self.tmp / "out" / "library" / "a-shoot"
        lib.mkdir(parents=True)
        (lib / "library.json").write_text(json.dumps({"folders": ["/Volumes/T/a shoot"], "clips": [], "counts": {}}))
        before = len(self.app.state.get("libraries", []))
        self.app.discover()
        self.assertEqual(self.app.discover(), 0)  # once
        libs = json.loads(self.get("/api/state")[2])["libraries"]
        self.assertEqual((len(libs), libs[-1]["name"], libs[-1]["folders"], libs[-1]["dir"]), (before + 1, "a-shoot", ["/Volumes/T/a shoot"], str(lib.resolve())))

    def test_a_jobs_result_names_the_set_of_promos_it_made(self):
        from videtor.ui import jobs

        job = jobs.Job(id="j9", kind="promos", title="t", args=[])
        jobs.note_event(job, {"event": "artifact", "kind": "promo", "path": "/p/x/clips/promo-1.mp4", "text": ""})
        jobs.note_event(job, {"event": "artifact", "kind": "promos", "path": "/p/x/clips/promos.json", "text": ""})
        self.assertEqual(job.result, {"promos": "/p/x/clips/promos.json"})  # what the page opens when the job ends
        jobs.note_event(job, {"event": "error", "text": "YouTube wants a login for this video"})
        self.assertEqual(job.result["error"], "YouTube wants a login for this video")  # what it shows when the job fails

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


class OneWriterTests(unittest.TestCase):
    """Jobs that write a project's cut, one at a time: the videtor here sleeps, so the first job stays running."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "cache").mkdir()
        self.app = server.App(self.tmp, [sys.executable, "-c", "import time; time.sleep(30)"], out_root=self.tmp / "out", state_file=self.tmp / "cache" / "ui-state.json")
        self.httpd = server.serve(self.app, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.d = self.tmp / "out" / "demo"
        self.d.mkdir(parents=True)
        (self.d / "overview.md").write_text("x")
        (self.d / "shotmap.json").write_text(json.dumps({"version": 1, "name": "demo", "key": "demo::footage", "length_s": 10,
                                                         "sections": [{"name": "s", "slots": [{"id": "s1", "show": "x", "beats": 4}]}]}))
        self.app.add_project("demo", str(self.tmp), str(self.d))

    def tearDown(self):
        self.app.jobs.cancel_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    post = ServerTests.post

    def project(self) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/project?dir={self.d}") as r:
            return json.loads(r.read())

    def test_the_project_says_its_stage_and_what_may_be_done(self):
        p = self.project()
        self.assertEqual(p["stage"], "cut")
        self.assertEqual(set(p["can"]), {"prep", "cut", "edit", "look", "write_xml", "readback", "reference"})
        self.assertEqual(p["can"]["cut"], {"ok": True, "why": ""})
        self.assertEqual(p["can"]["edit"], {"ok": False, "why": "There is no cut yet: cut it first."})
        self.assertEqual(self.post("/api/write-xml", {"project": str(self.d)})[1], {"error": "There is no cut yet: cut it first."})  # the endpoint says the same
        self.assertTrue(self.app.state_file.with_name("ui-jobs.json").exists())  # the running jobs are noted beside the app's state

    def test_a_plan_from_a_reference_video_is_a_job_that_writes_the_project(self):
        self.assertEqual(self.post("/api/reference", {"project": str(self.d)})[1], {"error": "Choose the video to cut like first: a finished video, or a link to one."})
        self.assertIn("is not there", self.post("/api/reference", {"project": str(self.d), "video": str(self.tmp / "no.mp4")})[1]["error"])
        (self.tmp / "notes.txt").write_text("x")
        self.assertIn("Choose a video file", self.post("/api/reference", {"project": str(self.d), "video": str(self.tmp / "notes.txt")})[1]["error"])
        video = self.tmp / "their reel.mp4"
        video.write_bytes(b"x")
        _, r = self.post("/api/reference", {"project": str(self.d), "video": str(video)})
        job = r["job"]
        self.assertEqual((job["kind"], job["writes"], job["args"][:3]), ("plan", True, ["reference", str(self.d), str(video)]))
        self.assertEqual(self.project()["status"], {"text": "Writing the plan", "tone": "busy"})
        busy = f"“{job['title']}” is still working on this project: wait for it to finish, or stop it, first."
        self.assertEqual(self.post("/api/cut", {"project": str(self.d)})[1], {"error": busy})  # no cut from a plan being rewritten
        # the reference given with a new project is kept with it; forgetting it is a setting
        _, r = self.post("/api/prep", {"folder": str(self.tmp), "name": "two", "frame": "square", "reference": str(video)})
        two = Path(r["project"]["dir"])
        self.assertEqual(json.loads((two / "settings.json").read_text()), {"frame": "square", "reference": str(video)})
        with urllib.request.urlopen(f"{self.base}/api/references") as resp:  # a project's reference can be chosen again for another
            self.assertEqual([(x["name"], x["of"]) for x in json.loads(resp.read())["references"]], [("their reel.mp4", "the reference of two")])
        self.assertEqual(self.post("/api/settings", {"project": str(two), "reference": ""})[1]["requirements"]["mine"], {"frame": "square"})
        self.assertIn("is not there", self.post("/api/prep", {"folder": str(self.tmp), "name": "three", "reference": "/nowhere/x.mp4"})[1]["error"])

    def test_a_second_cut_and_an_edit_are_refused_in_words_while_a_cut_runs(self):
        _, r = self.post("/api/cut", {"project": str(self.d)})
        first = r["job"]
        self.assertTrue(first["live"] and first["writes"])
        busy = f"“{first['title']}” is still working on this project: wait for it to finish, or stop it, first."
        self.assertEqual(self.post("/api/cut", {"project": str(self.d)})[1], {"error": busy})
        (self.d / "picks.json").write_text(json.dumps({"version": 1, "name": "demo", "key": "demo::footage", "fps": 25, "width": 1080, "height": 1920, "slots": []}))
        before = (self.d / "picks.json").read_text()
        for path, body in (("/api/edit", {"op": "undo"}), ("/api/write-xml", {}), ("/api/look", {"choices": {"s1": 1}})):
            self.assertEqual(self.post(path, {"project": str(self.d), **body})[1], {"error": busy}, path)
        self.assertEqual((self.d / "picks.json").read_text(), before)
        p = self.project()
        self.assertEqual((p["stage"], p["can"]["edit"], p["can"]["cut"]["ok"], p["can"]["readback"]["ok"]), ("review", {"ok": False, "why": busy}, False, False))
        self.assertEqual(p["status"], {"text": "Cutting", "tone": "busy"})
        from unittest import mock

        with mock.patch.object(server.project_mod, "can", return_value=(True, "")):  # past the page's rule, the jobs' own still holds (jobs.Busy)
            self.assertEqual(self.post("/api/write-xml", {"project": str(self.d)})[1], {"error": busy})
        self.app.jobs.cancel(self.app.jobs.get(first["id"]))
        self.assertIn("job", self.post("/api/write-xml", {"project": str(self.d)})[1])  # the cut stopped: the XML may be written


if __name__ == "__main__":
    unittest.main()
