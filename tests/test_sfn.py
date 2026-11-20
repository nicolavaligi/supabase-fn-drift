"""Tests for sfn, run against a fake Management API on 127.0.0.1 (no real Supabase calls)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sfn  # noqa: E402

REF = "abcdefghijklmnopqrst"
TOKEN = "sbp_test_token"


class FakeSupabase(BaseHTTPRequestHandler):
    functions = {}      # slug -> {"slug", "version", "updated_at", "status"}
    migrations = []
    requests = []       # (method, path, body bytes, headers)
    status_override = None

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        cls.requests.append((method, self.path, body, dict(self.headers)))
        if cls.status_override:
            return self._send(cls.status_override, {"message": "nope"})
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, {"message": "unauthorized"})
        prefix = "/v1/projects/%s" % REF
        if method == "GET" and self.path == prefix + "/functions":
            return self._send(200, list(cls.functions.values()))
        if method == "POST" and self.path.startswith(prefix + "/functions/deploy?slug="):
            slug = self.path.split("slug=")[1]
            current = cls.functions.get(slug, {"version": 0})
            cls.functions[slug] = {"slug": slug, "version": current["version"] + 1,
                                   "updated_at": 1790000000000, "status": "ACTIVE"}
            return self._send(201, cls.functions[slug])
        if method == "POST" and self.path == prefix + "/database/query":
            return self._send(201, cls.migrations)
        return self._send(404, {"message": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSupabase)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeSupabase.functions = {}
        FakeSupabase.migrations = []
        FakeSupabase.requests = []
        FakeSupabase.status_override = None
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self._env = mock.patch.dict(os.environ, {
            "SUPABASE_ACCESS_TOKEN": TOKEN,
            "SFN_API_BASE": "http://127.0.0.1:%d" % self.server.server_address[1]})
        self._env.start()
        os.environ.pop("SUPABASE_PROJECT_REF", None)
        self.config({"project_ref": REF})

    def tearDown(self):
        self._env.stop()
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def config(self, data):
        (self.root / "sfn.json").write_text(json.dumps(data))

    def function(self, slug, body="export default 1;\n", extra=None):
        folder = self.root / "supabase" / "functions" / slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.ts").write_text(body)
        for name, text in (extra or {}).items():
            (folder / name).write_text(text)
        return folder

    def online(self, slug, version):
        FakeSupabase.functions[slug] = {"slug": slug, "version": version,
                                        "updated_at": 1780000000000, "status": "ACTIVE"}

    def run_sfn(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sfn.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def states(self):
        code, out, _ = self.run_sfn("status", "--json")
        return code, {r["slug"]: r["state"] for r in json.loads(out)}


class Status(Base):
    def test_never_recorded_then_in_sync(self):
        self.function("hello")
        self.online("hello", 3)
        self.assertEqual(self.states(), (0, {"hello": sfn.NEVER_RECORDED}))
        self.assertEqual(self.run_sfn("mark")[0], 0)
        self.assertEqual(self.states(), (0, {"hello": sfn.IN_SYNC}))

    def test_changed_online_is_drift(self):
        self.function("hello")
        self.online("hello", 3)
        self.run_sfn("mark")
        self.online("hello", 4)
        code, out, _ = self.run_sfn("status")
        self.assertEqual(code, 1)
        self.assertIn("CHANGED ONLINE", out)
        self.assertIn("v3 -> v4", out)
        self.assertIn("supabase functions download hello --project-ref " + REF, out)

    def test_changed_locally(self):
        folder = self.function("hello")
        self.online("hello", 3)
        self.run_sfn("mark")
        (folder / "index.ts").write_text("export default 2;\n")
        self.assertEqual(self.states(), (0, {"hello": sfn.CHANGED_LOCALLY}))

    def test_new_helper_file_counts_as_local_change(self):
        folder = self.function("hello")
        self.online("hello", 1)
        self.run_sfn("mark")
        (folder / "lib").mkdir()
        (folder / "lib" / "util.ts").write_text("export const x = 1;\n")
        self.assertEqual(self.states()[1], {"hello": sfn.CHANGED_LOCALLY})

    def test_function_online_without_local_copy_is_drift(self):
        self.function("hello")
        self.online("hello", 1)
        self.online("ghost", 7)
        self.run_sfn("mark")
        self.assertEqual(self.states(),
                         (1, {"hello": sfn.IN_SYNC, "ghost": sfn.NO_LOCAL_COPY}))

    def test_local_only_function(self):
        self.function("draft")
        self.assertEqual(self.states(), (0, {"draft": sfn.NOT_DEPLOYED}))

    def test_shared_and_folders_without_index_are_not_functions(self):
        self.function("hello")
        shared = self.root / "supabase" / "functions" / "_shared"
        shared.mkdir()
        (shared / "index.ts").write_text("x")
        (self.root / "supabase" / "functions" / "notes").mkdir()
        self.online("hello", 1)
        self.assertEqual(set(self.states()[1]), {"hello"})

    def test_explicit_function_list_and_custom_dir(self):
        self.config({"project_ref": REF, "functions_dir": "fn", "functions": ["a"]})
        (self.root / "fn" / "a").mkdir(parents=True)
        (self.root / "fn" / "a" / "index.ts").write_text("x")
        self.online("a", 1)
        self.online("b", 1)
        self.assertEqual(self.states()[1], {"a": sfn.NEVER_RECORDED, "b": sfn.NO_LOCAL_COPY})

    def test_config_found_from_subfolder(self):
        folder = self.function("hello")
        self.online("hello", 1)
        os.chdir(folder)
        self.assertEqual(self.states()[1], {"hello": sfn.NEVER_RECORDED})


class Mark(Base):
    def test_lock_content(self):
        folder = self.function("hello")
        self.online("hello", 5)
        self.run_sfn("mark", "hello")
        lock = json.loads((self.root / "supabase" / "functions" / "sfn-lock.json").read_text())
        self.assertEqual(lock["project_ref"], REF)
        self.assertEqual(lock["functions"]["hello"]["online_version"], 5)
        self.assertEqual(lock["functions"]["hello"]["local_fingerprint"], sfn.fingerprint(folder))
        self.assertNotIn(TOKEN, json.dumps(lock))
        self.assertEqual(sorted(p.name for p in (self.root / "supabase" / "functions").iterdir()),
                         ["hello", "sfn-lock.json"])

    def test_mark_unknown_or_missing_folder_fails(self):
        self.online("ghost", 1)
        code, _, err = self.run_sfn("mark", "ghost", "nowhere")
        self.assertEqual(code, 1)
        self.assertIn("ghost: no folder", err)
        self.assertIn("nowhere: not deployed online", err)
        self.assertFalse((self.root / "supabase" / "functions" / "sfn-lock.json").exists())

    def test_lock_for_another_project_is_refused(self):
        self.function("hello")
        self.online("hello", 1)
        self.run_sfn("mark")
        self.config({"project_ref": "zzzzzzzzzzzzzzzzzzzz"})
        code, _, err = self.run_sfn("status")
        self.assertEqual(code, 2)
        self.assertIn("was recorded for project", err)


class Deploy(Base):
    def deploy_requests(self):
        return [r for r in FakeSupabase.requests if "/functions/deploy" in r[1]]

    def test_deploy_sends_files_and_records(self):
        self.function("hello", extra={"deno.json": "{}", "notes.md": "not uploaded"})
        code, out, _ = self.run_sfn("deploy", "hello")
        self.assertEqual(code, 0)
        self.assertIn("hello: deployed v1 (ACTIVE), verify_jwt=true", out)
        (_, path, body, headers), = self.deploy_requests()
        self.assertTrue(path.endswith("/functions/deploy?slug=hello"))
        self.assertIn("multipart/form-data; boundary=", headers["Content-Type"])
        self.assertIn(b'filename="index.ts"', body)
        self.assertIn(b"export default 1;", body)
        self.assertIn(b'filename="deno.json"', body)
        self.assertNotIn(b"notes.md", body)
        self.assertIn(b'"verify_jwt": true', body)
        self.assertEqual(self.states(), (0, {"hello": sfn.IN_SYNC}))

    def test_refuses_when_online_changed(self):
        self.function("hello")
        self.online("hello", 2)
        self.run_sfn("mark")
        self.online("hello", 3)
        code, _, err = self.run_sfn("deploy", "hello")
        self.assertEqual(code, 2)
        self.assertIn("could overwrite someone else's work", err)
        self.assertEqual(self.deploy_requests(), [])

    def test_refuses_when_never_recorded_then_force(self):
        self.function("hello")
        self.online("hello", 2)
        self.assertEqual(self.run_sfn("deploy", "hello")[0], 2)
        self.assertEqual(self.deploy_requests(), [])
        self.assertEqual(self.run_sfn("deploy", "hello", "--force")[0], 0)
        self.assertEqual(FakeSupabase.functions["hello"]["version"], 3)

    def test_local_edits_deploy_normally(self):
        folder = self.function("hello")
        self.online("hello", 2)
        self.run_sfn("mark")
        (folder / "index.ts").write_text("export default 9;\n")
        self.assertEqual(self.run_sfn("deploy", "hello")[0], 0)

    def test_verify_jwt_from_config_and_flag(self):
        self.config({"project_ref": REF, "verify_jwt": {"hook": False}})
        self.function("hook")
        self.run_sfn("deploy", "hook")
        self.assertIn(b'"verify_jwt": false', self.deploy_requests()[-1][2])
        self.run_sfn("deploy", "hook", "--verify-jwt")
        self.assertIn(b'"verify_jwt": true', self.deploy_requests()[-1][2])

    def test_missing_index_and_bad_slug(self):
        self.assertEqual(self.run_sfn("deploy", "nothing")[0], 2)
        self.assertEqual(self.run_sfn("deploy", "../etc")[0], 2)
        self.assertEqual(self.deploy_requests(), [])


class Migrations(Base):
    def test_writes_files_once(self):
        FakeSupabase.migrations = [
            {"version": "20260101120000", "name": "create users", "body": "create table u();"},
            {"version": "20260102090000", "name": "add_index", "body": "create index i on u(id);"},
        ]
        code, out, _ = self.run_sfn("migrations", "--since", "20260101")
        self.assertEqual(code, 0)
        dest = self.root / "supabase" / "migrations"
        self.assertEqual(sorted(p.name for p in dest.iterdir()),
                         ["20260101120000_create_users.sql", "20260102090000_add_index.sql"])
        self.assertEqual((dest / "20260101120000_create_users.sql").read_text(),
                         "create table u();\n")
        self.assertIn("2 file(s) written", out)
        self.assertIn("0 file(s) written", self.run_sfn("migrations")[1])
        sent = json.loads(FakeSupabase.requests[-2][2])["query"]
        self.assertIn("version >= '20260101'", sent)

    def test_since_must_be_digits(self):
        before = len(FakeSupabase.requests)
        code, _, err = self.run_sfn("migrations", "--since", "1'; drop table x; --")
        self.assertEqual(code, 2)
        self.assertIn("digits only", err)
        self.assertEqual(len(FakeSupabase.requests), before)

    def test_odd_names_cannot_escape_the_folder(self):
        FakeSupabase.migrations = [{"version": "20260101120000", "name": "../../evil", "body": "x"}]
        self.run_sfn("migrations")
        dest = self.root / "supabase" / "migrations"
        self.assertEqual([p.name for p in dest.iterdir()], ["20260101120000__evil.sql"])


class Errors(Base):
    def test_missing_config(self):
        (self.root / "sfn.json").unlink()
        code, _, err = self.run_sfn("status")
        self.assertEqual(code, 2)
        self.assertIn("no sfn.json found", err)

    def test_bad_project_ref(self):
        self.config({"project_ref": "Not Valid!"})
        self.assertEqual(self.run_sfn("status")[0], 2)

    def test_project_ref_from_environment(self):
        self.config({})
        with mock.patch.dict(os.environ, {"SUPABASE_PROJECT_REF": REF}):
            self.assertEqual(self.run_sfn("status")[0], 0)

    def test_rejected_token_is_explained_and_not_echoed(self):
        with mock.patch.dict(os.environ, {"SUPABASE_ACCESS_TOKEN": "sbp_wrong_secret"}):
            code, out, err = self.run_sfn("-v", "status")
        self.assertEqual(code, 2)
        self.assertIn("refused the access token", err)
        self.assertNotIn("sbp_wrong_secret", out + err)

    def test_api_failure(self):
        FakeSupabase.status_override = 500
        code, _, err = self.run_sfn("status")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 500", err)

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"SUPABASE_ACCESS_TOKEN": ""}), \
                mock.patch.object(sfn.sys, "platform", "linux"):
            code, _, err = self.run_sfn("status")
        self.assertEqual(code, 2)
        self.assertIn("no access token", err)

    def test_no_command(self):
        self.assertEqual(self.run_sfn()[0], 2)


if __name__ == "__main__":
    unittest.main()
