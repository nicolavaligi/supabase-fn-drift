# sfn — Supabase Edge Function drift check

Know when the Edge Function running online is **no longer the one in your
repository**, before your next deploy overwrites it.

```console
$ sfn status
 ok   send-invoice    in sync          v12
!!!!  stripe-webhook  CHANGED ONLINE   v7 -> v9: deployed from somewhere else
 ~    daily-report    changed locally  edits not deployed yet
!!!!  quick-fix       NO LOCAL COPY    online v1, not in the repository

2 function(s) were deployed from somewhere else. Bring the online version into the
repository before deploying:
  supabase functions download stripe-webhook --project-ref abcdefghijklmnopqrst
  supabase functions download quick-fix --project-ref abcdefghijklmnopqrst
then record the new state:  sfn mark stripe-webhook quick-fix
```

## The problem

An Edge Function can be deployed from the CLI, from the dashboard editor, from
an AI assistant through an MCP connector, or from a teammate's machine. None of
these tells the others. The repository quietly goes stale, and the next
`deploy` from it publishes an old version on top of somebody's fix. Nothing
fails; you find out from a customer.

`sfn` keeps a small lock file next to your functions recording, for each one,
*which online version the repository matched* and *a fingerprint of the local
files*. From those two facts it can always tell the four cases apart:

| State | Meaning | Exit status of `sfn status` |
|---|---|---|
| `in sync` | online version and local files are the ones recorded | 0 |
| `changed locally` | you edited files and have not deployed yet | 0 |
| `CHANGED ONLINE` | the online version moved without the repository | **1** |
| `NO LOCAL COPY` | a function exists online but not in the repository | **1** |
| `never recorded` | online and local both exist, never compared | 0 |
| `not deployed` | exists only in the repository | 0 |

The rule it enforces: **what is online is the truth**, because it is the only
thing your users see. The repository catches up with it, never the opposite by
accident.

## Install

Python 3.9+, no dependencies. Copy `sfn.py` into your project, or:

```bash
pip install git+https://github.com/nicolavaligi/supabase-fn-drift
```

## Set up

1. Create `sfn.json` in the repository root (only `project_ref` is required):

   ```json
   {
     "project_ref": "abcdefghijklmnopqrst",
     "functions_dir": "supabase/functions",
     "verify_jwt": { "stripe-webhook": false }
   }
   ```

   Every folder in `functions_dir` containing an `index.ts` is treated as a
   function (folders starting with `_`, such as `_shared`, are not). Add
   `"functions": ["a", "b"]` to track an explicit list instead.

2. Provide a personal access token
   ([supabase.com/dashboard/account/tokens](https://supabase.com/dashboard/account/tokens)):

   ```bash
   export SUPABASE_ACCESS_TOKEN=sbp_...
   # or, on macOS, once:
   security add-generic-password -s supabase-access-token -a "$USER" -w
   ```

3. Record the starting point and commit the lock file:

   ```bash
   sfn status          # look first
   sfn mark            # "repository and online match as of now"
   git add supabase/functions/sfn-lock.json
   ```

## Commands

| Command | What it does |
|---|---|
| `sfn status [--json]` | Compares online and repository. Exit 1 on drift, so it works as a CI or pre-deploy gate. |
| `sfn mark [SLUG ...]` | Records the current online version and local fingerprint. Run it after you brought an online change home, or after deploying with another tool. |
| `sfn deploy SLUG [--force]` | Deploys from disk, but first checks that function: if the online copy changed, or was never recorded, it refuses. On success it records the new state. `--verify-jwt` / `--no-verify-jwt` override `sfn.json` (default: verify). |
| `sfn migrations [--since V] [--out DIR]` | Writes the migrations *actually applied* to the database (`supabase_migrations.schema_migrations`) as `<version>_<name>.sql` files, so a schema changed from the dashboard or an assistant is not lost. |

As a gate in CI:

```yaml
- run: python sfn.py status
  env:
    SUPABASE_ACCESS_TOKEN: ${{ secrets.SUPABASE_ACCESS_TOKEN }}
```

## Limits

- **`sfn` does not download function source.** The Management API returns the
  deployed code as a binary bundle; decoding it here would mean maintaining a
  parser that breaks with every platform change. Use
  `supabase functions download <slug>` (the message printed on drift gives the
  exact command), then `sfn mark`.
- **`sfn deploy` uploads the `*.ts` and `*.json` files at the top level of the
  function folder.** Functions that import from `../_shared` or use sub-folders
  should be deployed with the official CLI, followed by `sfn mark <slug>`.
- Drift is detected by version number: it tells you *that* the online copy
  changed and from which version, not *what* changed.
- The test suite runs against a local fake of the Management API, not against
  a real project. Start with `sfn status`, which is read-only, and check that
  it describes your project correctly before relying on `sfn deploy`.

## Security and privacy

- The token is read from the environment or the macOS keychain, used only as a
  bearer header towards `api.supabase.com`, and never written to disk or logs.
  A personal access token can do everything you can: prefer a token for a
  dedicated low-privilege account in CI.
- The lock file holds the project id, version numbers and file hashes — no
  secrets, no source, no data. It is meant to be committed.
- `sfn migrations` writes SQL **statements**, not table contents. Review the
  files before committing all the same: seed data or hard-coded values inside a
  migration would come along.
- No telemetry; the only network calls are to the Supabase Management API.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Plain Python standard library by choice: this runs right before a deploy, in
CI images and on laptops, and must not add an install step or a dependency to
audit to a pipeline that already has the Supabase CLI.

Unofficial tool, not affiliated with Supabase.

## License

MIT
