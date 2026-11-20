#!/usr/bin/env python3
"""sfn - catch drift between deployed Supabase Edge Functions and your repository.

Edge Functions can be deployed from several places: the CLI, the dashboard
editor, an AI assistant with an MCP connector, a teammate's laptop. Whoever
deploys from one place does not update the others, and the next deploy from the
repository silently overwrites their work with an older version.

sfn treats what is running online as the truth and tells you when the
repository has fallen behind it, before you deploy.

    sfn status                what changed online or locally since the last record
    sfn mark [SLUG ...]       record "repository and online match" for these functions
    sfn deploy SLUG           deploy from disk; refuses if the online copy moved
    sfn migrations            save the migrations applied to the database as files

Configuration lives in sfn.json (see README). The access token is read from
SUPABASE_ACCESS_TOKEN or, on macOS, from the keychain; it is never written to disk.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

log = logging.getLogger("sfn")

API_BASE = "https://api.supabase.com"
CONFIG_NAME = "sfn.json"
LOCK_NAME = "sfn-lock.json"
DEFAULT_KEYCHAIN_SERVICE = "supabase-access-token"

IN_SYNC = "in sync"
CHANGED_ONLINE = "CHANGED ONLINE"
CHANGED_LOCALLY = "changed locally"
NEVER_RECORDED = "never recorded"
NOT_DEPLOYED = "not deployed"
NO_LOCAL_COPY = "NO LOCAL COPY"

# states that mean "someone deployed from somewhere else"
DRIFT = (CHANGED_ONLINE, NO_LOCAL_COPY)


class SfnError(Exception):
    """An error to show to the user without a traceback."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, path: Path, data: dict) -> None:
        self.path = path
        self.root = path.parent
        ref = os.environ.get("SUPABASE_PROJECT_REF") or data.get("project_ref") or ""
        if not re.fullmatch(r"[a-z0-9]{10,40}", ref):
            raise SfnError("project_ref is missing or malformed in %s "
                           "(it is the id in your project URL)" % path)
        self.project_ref: str = ref
        self.functions_dir: Path = (self.root / data.get("functions_dir", "supabase/functions"))
        self.migrations_dir: Path = (self.root / data.get("migrations_dir", "supabase/migrations"))
        self.keychain_service: str = data.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE)
        self.verify_jwt: Dict[str, bool] = dict(data.get("verify_jwt", {}))
        listed = data.get("functions")
        if listed is not None and (not isinstance(listed, list)
                                   or not all(isinstance(s, str) for s in listed)):
            raise SfnError("'functions' in %s must be a list of slugs" % path)
        self._listed: Optional[List[str]] = listed

    @property
    def lock_path(self) -> Path:
        return self.functions_dir / LOCK_NAME

    def tracked(self) -> List[str]:
        """Slugs this repository owns: the configured list, or every folder with an index.ts."""
        if self._listed is not None:
            return list(self._listed)
        if not self.functions_dir.is_dir():
            return []
        return sorted(p.name for p in self.functions_dir.iterdir()
                      if p.is_dir() and not p.name.startswith(("_", "."))
                      and (p / "index.ts").is_file())


def load_config(explicit: Optional[str]) -> Config:
    if explicit:
        path = Path(explicit).resolve()
    else:
        here = Path.cwd().resolve()
        path = next((d / CONFIG_NAME for d in (here, *here.parents)
                     if (d / CONFIG_NAME).is_file()), here / CONFIG_NAME)
    if not path.is_file():
        raise SfnError("no %s found here or in a parent folder. Create one:\n"
                       '  {"project_ref": "<your project id>"}' % CONFIG_NAME)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SfnError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(data, dict):
        raise SfnError("%s must contain a JSON object" % path)
    return Config(path, data)


# ---------------------------------------------------------------------------
# token and API
# ---------------------------------------------------------------------------

def read_token(keychain_service: str) -> str:
    token = (os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
    if token:
        return token
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", keychain_service, "-w"],
                capture_output=True, text=True, check=True).stdout.strip()
            if out:
                return out
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    raise SfnError(
        "no access token. Create one at supabase.com/dashboard/account/tokens, then either\n"
        "  export SUPABASE_ACCESS_TOKEN=sbp_...\n"
        "or, on macOS, store it once in the keychain:\n"
        "  security add-generic-password -s %s -a \"$USER\" -w" % keychain_service)


class Api:
    """Thin client for the Supabase Management API, scoped to one project."""

    def __init__(self, project_ref: str, token: str, base: Optional[str] = None) -> None:
        base = base or os.environ.get("SFN_API_BASE") or API_BASE
        self._root = "%s/v1/projects/%s" % (base.rstrip("/"), project_ref)
        self._token = token

    def _call(self, method: str, path: str, body: Optional[bytes] = None,
              content_type: Optional[str] = None, timeout: int = 60):
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                   "User-Agent": "sfn/" + __version__}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self._root + path, data=body, method=method, headers=headers)
        log.debug("%s %s", method, path)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SfnError("Supabase refused the access token (HTTP %d): check that it is "
                               "valid and can access this project" % exc.code)
            detail = exc.read()[:300].decode("utf-8", "replace").strip()
            raise SfnError("Supabase API error on %s %s: HTTP %d %s"
                           % (method, path, exc.code, detail))
        except urllib.error.URLError as exc:
            raise SfnError("cannot reach the Supabase API: %s" % exc.reason)
        return json.loads(raw) if raw.strip() else None

    def list_functions(self) -> Dict[str, dict]:
        return {f["slug"]: f for f in self._call("GET", "/functions") or []}

    def deploy(self, slug: str, files: Sequence[Tuple[str, bytes]], verify_jwt: bool) -> dict:
        metadata = {"name": slug, "entrypoint_path": "index.ts", "verify_jwt": verify_jwt}
        body, ctype = multipart([("metadata", json.dumps(metadata))], files)
        return self._call("POST", "/functions/deploy?slug=" + slug, body, ctype, timeout=120)

    def query(self, sql: str):
        body = json.dumps({"query": sql}).encode("utf-8")
        return self._call("POST", "/database/query", body, "application/json")


def multipart(fields: Sequence[Tuple[str, str]],
              files: Sequence[Tuple[str, bytes]]) -> Tuple[bytes, str]:
    boundary = "----sfn" + uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in fields:
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, name, value)).encode("utf-8"))
    for filename, content in files:
        ctype = mimetypes.guess_type(filename)[0] or "application/typescript"
        parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                      'Content-Type: %s\r\n\r\n' % (boundary, filename, ctype)).encode("utf-8"))
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


# ---------------------------------------------------------------------------
# local state
# ---------------------------------------------------------------------------

def fingerprint(folder: Path) -> Optional[str]:
    """Hash of every file in a function folder (names and contents); None if absent."""
    if not folder.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder).as_posix()
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def read_lock(cfg: Config) -> dict:
    if not cfg.lock_path.is_file():
        return {"functions": {}}
    try:
        lock = json.loads(cfg.lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SfnError("%s is corrupted: %s" % (cfg.lock_path, exc))
    lock.setdefault("functions", {})
    if lock.get("project_ref") not in (None, cfg.project_ref):
        raise SfnError("%s was recorded for project %s, but sfn.json points to %s"
                       % (cfg.lock_path, lock["project_ref"], cfg.project_ref))
    return lock


def write_lock(cfg: Config, lock: dict) -> None:
    lock["project_ref"] = cfg.project_ref
    lock["recorded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg.lock_path.with_name(cfg.lock_path.name + ".tmp")
    tmp.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, cfg.lock_path)


def state_of(slug: str, cfg: Config, lock: dict, online: Dict[str, dict]) -> Tuple[str, str]:
    """(state, note) for one tracked function."""
    live = online.get(slug)
    recorded = lock["functions"].get(slug)
    if live is None:
        return NOT_DEPLOYED, "exists only in the repository"
    if not recorded:
        return NEVER_RECORDED, "online v%s" % live.get("version")
    if live.get("version") != recorded.get("online_version"):
        return CHANGED_ONLINE, ("v%s -> v%s: deployed from somewhere else"
                                % (recorded.get("online_version"), live.get("version")))
    if fingerprint(cfg.functions_dir / slug) != recorded.get("local_fingerprint"):
        return CHANGED_LOCALLY, "edits not deployed yet"
    return IN_SYNC, "v%s" % live.get("version")


def compare(cfg: Config, lock: dict, online: Dict[str, dict]) -> List[Tuple[str, str, str]]:
    tracked = cfg.tracked()
    rows = [(slug, *state_of(slug, cfg, lock, online)) for slug in tracked]
    for slug in sorted(set(online) - set(tracked)):
        rows.append((slug, NO_LOCAL_COPY,
                     "online v%s, not in the repository" % online[slug].get("version")))
    return rows


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

_MARK = {IN_SYNC: " ok ", CHANGED_ONLINE: "!!!!", CHANGED_LOCALLY: " ~  ",
         NEVER_RECORDED: " ?  ", NOT_DEPLOYED: " -  ", NO_LOCAL_COPY: "!!!!"}


def cmd_status(cfg: Config, api: Api, args: argparse.Namespace) -> int:
    rows = compare(cfg, read_lock(cfg), api.list_functions())
    drifted = [r for r in rows if r[1] in DRIFT]
    if args.json:
        json.dump([{"slug": s, "state": st, "note": n} for s, st, n in rows],
                  sys.stdout, indent=1)
        sys.stdout.write("\n")
    else:
        if not rows:
            print("No functions online and none in %s." % cfg.functions_dir)
        width = max([len(r[0]) for r in rows] + [8])
        for slug, state, note in rows:
            print("%s  %-*s  %-16s %s" % (_MARK[state], width, slug, state, note))
        if drifted:
            print("\n%d function(s) were deployed from somewhere else. Bring the online "
                  "version into the\nrepository before deploying:" % len(drifted))
            for slug, _, _ in drifted:
                print("  supabase functions download %s --project-ref %s"
                      % (slug, cfg.project_ref))
            print("then record the new state:  sfn mark " + " ".join(r[0] for r in drifted))
    return 1 if drifted else 0


def cmd_mark(cfg: Config, api: Api, args: argparse.Namespace) -> int:
    online = api.list_functions()
    slugs = args.slugs or cfg.tracked()
    lock = read_lock(cfg)
    failed = 0
    for slug in slugs:
        live = online.get(slug)
        local = fingerprint(cfg.functions_dir / slug)
        if live is None:
            log.error("%s: not deployed online, nothing to record", slug)
            failed += 1
        elif local is None:
            log.error("%s: no folder %s, download it first", slug, cfg.functions_dir / slug)
            failed += 1
        else:
            lock["functions"][slug] = {"online_version": live.get("version"),
                                       "online_updated_at": live.get("updated_at"),
                                       "local_fingerprint": local}
            print("%s: recorded as matching online v%s" % (slug, live.get("version")))
    if len(slugs) > failed:
        write_lock(cfg, lock)
    return 1 if failed else 0


def cmd_deploy(cfg: Config, api: Api, args: argparse.Namespace) -> int:
    slug = args.slug
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise SfnError("invalid function name: %r" % slug)
    folder = cfg.functions_dir / slug
    sources = sorted(folder.glob("*.ts")) + sorted(folder.glob("*.json"))
    if not (folder / "index.ts").is_file():
        raise SfnError("nothing to deploy: %s has no index.ts" % folder)

    lock = read_lock(cfg)
    state, note = state_of(slug, cfg, lock, api.list_functions())
    if state in (CHANGED_ONLINE, NEVER_RECORDED) and not args.force:
        raise SfnError(
            "%s is %s (%s).\nDeploying now could overwrite someone else's work. Download the "
            "online version, merge,\nrun `sfn mark %s`, then deploy again - or pass --force "
            "to overwrite on purpose." % (slug, state.lower(), note, slug))

    verify = cfg.verify_jwt.get(slug, True) if args.verify_jwt is None else args.verify_jwt
    result = api.deploy(slug, [(p.name, p.read_bytes()) for p in sources], verify) or {}
    print("%s: deployed v%s (%s), verify_jwt=%s, files: %s"
          % (slug, result.get("version"), result.get("status", "?"),
             str(verify).lower(), ", ".join(p.name for p in sources)))

    live = api.list_functions().get(slug) or result
    lock["functions"][slug] = {"online_version": live.get("version"),
                               "online_updated_at": live.get("updated_at"),
                               "local_fingerprint": fingerprint(folder)}
    write_lock(cfg, lock)
    return 0


def cmd_migrations(cfg: Config, api: Api, args: argparse.Namespace) -> int:
    since = args.since or "0"
    if not since.isdigit():
        raise SfnError("--since must be digits only, e.g. 20260101")
    rows = api.query(
        "select version, name, array_to_string(statements, E';\\n\\n') as body "
        "from supabase_migrations.schema_migrations "
        "where version >= '%s' order by version" % since) or []
    dest = Path(args.out).resolve() if args.out else cfg.migrations_dir
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in rows:
        version = str(row.get("version", ""))
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", str(row.get("name") or "migration"))
        if not version.isdigit():
            log.warning("skipped a migration with an unexpected version: %r", version)
            continue
        target = dest / ("%s_%s.sql" % (version, name))
        text = (row.get("body") or "").rstrip() + "\n"
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            written += 1
            print("  " + target.name)
    print("%d applied migration(s) online, %d file(s) written in %s" % (len(rows), written, dest))
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sfn", description="Catch drift between deployed Supabase Edge Functions "
                                "and your repository.")
    ap.add_argument("--version", action="version", version="sfn " + __version__)
    ap.add_argument("--config", metavar="FILE", help="path to sfn.json (default: search upwards)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show API calls on stderr")
    sub = ap.add_subparsers(dest="command")

    s = sub.add_parser("status", help="compare online and repository (exit 1 on drift)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_status)

    m = sub.add_parser("mark", help="record that repository and online match")
    m.add_argument("slugs", nargs="*", metavar="SLUG", help="default: every tracked function")
    m.set_defaults(func=cmd_mark)

    d = sub.add_parser("deploy", help="deploy one function from disk, guarded against drift")
    d.add_argument("slug", metavar="SLUG")
    d.add_argument("--force", action="store_true",
                   help="deploy even if the online copy changed or was never recorded")
    jwt = d.add_mutually_exclusive_group()
    jwt.add_argument("--verify-jwt", dest="verify_jwt", action="store_true", default=None)
    jwt.add_argument("--no-verify-jwt", dest="verify_jwt", action="store_false")
    d.set_defaults(func=cmd_deploy)

    g = sub.add_parser("migrations", help="save applied database migrations as .sql files")
    g.add_argument("--since", metavar="VERSION", help="only versions >= this, e.g. 20260101")
    g.add_argument("--out", metavar="DIR", help="default: migrations_dir from sfn.json")
    g.set_defaults(func=cmd_migrations)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, format="sfn: %(levelname)s: %(message)s",
                        level=logging.DEBUG if args.verbose else logging.WARNING, force=True)
    if not args.command:
        ap.print_help()
        return 2
    try:
        cfg = load_config(args.config)
        api = Api(cfg.project_ref, read_token(cfg.keychain_service))
        return args.func(cfg, api, args)
    except SfnError as exc:
        print("sfn: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
