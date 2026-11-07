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

    def post(self, path: str, body: dict):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

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

    def test_unknown_routes_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/api/nope")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
