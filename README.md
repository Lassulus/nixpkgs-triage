# nixpkgs-triage

Review workflow for open NixOS/nixpkgs PRs: mirrors them into SQLite (`triage.db`), sorts them
into queues with `categories.toml`, tracks your review state, and runs per-PR jobs (an omp agent
checking the contribution guidelines, and nixpkgs-review). Browse it in a curses UI or a web
dashboard.

Needs Python ≥ 3.11 (stdlib only) and a GitHub token (`GITHUB_TOKEN`/`GH_TOKEN` or `gh auth token`).
Guideline checks need `omp`; nixpkgs-review needs `nix` and a nixpkgs checkout (`~/src/nixpkgs`).
`TRIAGE_DB`, `TRIAGE_CATEGORIES` and `TRIAGE_JOBS_DIR` override the file locations.

## Sync

```sh
./triage update          # first run: full sync (~30 min), later: only PRs updated since the last sync
./triage update --full   # re-walk every open PR
```

One GraphQL request per 50 PRs (1 point). Requests are serial with a 1s pause, the sync sleeps when
fewer than `--reserve` (500) points remain, honours `Retry-After`, and halves the page size when
GitHub times out. A full sync is resumable after Ctrl-C. Only one `triage update` runs at a time.

## Categories

`categories.toml` is an ordered list of rules matching labels (`8.has: package (new)`,
`4.workflow: backport`, `10.rebuild-*`, …), authors, the base branch, and title conventions
(`init at`, `a -> b`, `nixos/…`, `attr: …`). The first match is the PR's category; all matches
are kept as tags. Editing the file re-categorizes the database on the next command.

## Curses UI

```sh
./triage ui
```

Categories on the left, their PRs on the right. Columns: `age`, `+/-` (lines), `CI`, `conflict`
(`2.status: merge conflict` label), `draft`, `mark` (local status, `*` = changed since), `check`
(`pass`, `issues` = blocking guideline issues, `FAIL` = job failed), `nixrev`.

| key | action |
|---|---|
| `tab`, `←`/`→` | switch pane |
| `↑`/`↓`, `PgUp`/`PgDn`, `g`/`G` | move |
| `enter` | open the PR in the browser |
| `space` | detail view |
| `c` / `n` | start guideline check / nixpkgs-review |
| `f` | filters: drafts, merge conflicts (hidden by default), CI, check, review |
| `o` | sort: oldest, newest, recently updated |
| `R` | run `triage update` and show its log |
| `S` | settings |
| `q` | quit |

The detail view shows the PR, both jobs, and the report (or the live log while a job runs):
`tab` switches job, `l` log/report, `x` cancel (asks), `P` posts the nixpkgs-review report as a
PR comment.

## Web dashboard

```sh
./triage serve                    # http://127.0.0.1:8080/, runs `triage update` every 300s
./triage serve --listen '[::]:8080' --sync-every 0
```

The curses list as one endlessly scrolling, server-rendered page, plus `approved by` (reviewers
whose latest review approves) and `author` columns. Clicking a row opens the PR on GitHub, clicking
the author their profile; `▸` folds out the details and reports. The
search box filters live with fuzzy matching on number, title and author (`pyth req` finds
`python3Packages.requests`). Filters and search live in the URL. It only reads the database (the
sync loop is the only thing talking to GitHub), is read-only and has no authentication: put it
behind a reverse proxy.

### NixOS module

```nix
{
  inputs.nixpkgs-triage.url = "github:Lassulus/nixpkgs-triage";
  outputs = { nixpkgs, nixpkgs-triage, ... }: {
    nixosConfigurations.server = nixpkgs.lib.nixosSystem {
      modules = [
        nixpkgs-triage.nixosModules.default
        {
          services.nixpkgs-triage = {
            enable = true;
            environmentFile = "/run/secrets/nixpkgs-triage"; # GITHUB_TOKEN=…
          };
        }
      ];
    };
  };
}
```

Options: `address`/`port` (`127.0.0.1`/`8080`), `openFirewall`, `syncInterval` (300, 0 disables),
`environmentFile`, `package`. Runs as a `DynamicUser` with its data in `/var/lib/nixpkgs-triage`.
`nix flake check` runs a VM test.

## Jobs

```sh
./triage check 123456 123457     # guideline check
./triage review 123456           # nixpkgs-review
./triage jobs [--all]
./triage cancel JOB_ID
./triage post 123456             # post the latest nixpkgs-review report
./triage settings [KEY [VALUE] [--reset]]
```

Jobs are detached `triage job-run ID` processes tracked in the `jobs` table; output goes to
`jobs/<PR>/<id>-<kind>/`. `max_checks` (3) and `max_reviews` (1) run at once, the rest queue.
Cancelling sends SIGINT.

| setting | default |
|---|---|
| `agent_command` | `s omp` |
| `agent_model` | omp's default |
| `review_command` | `nixpkgs-review`, or `nix run nixpkgs#nixpkgs-review --` |
| `review_args` | empty, e.g. `--systems …` |
| `nixpkgs_dir` | `~/src/nixpkgs` |
| `max_checks` / `max_reviews` | 3 / 1 |

- **Guideline check**: fetches the PR description, commits, diff, changed files and the guideline
  docs from the GitHub API and runs the agent read-only on them with
  `prompts/guideline-check.md`. The report lists blocking issues and nits; the verdict is
  `PASS` unless something blocks.
- **nixpkgs-review**: `review_command pr N --no-shell` in `nixpkgs_dir`. Without a usable GitHub
  CI evaluation it adds `--eval local` (and retries once with it if nixpkgs-review still finds
  none), unless `review_args` sets `--eval`.

## Review workflow

```sh
./triage stats [--by topic]
./triage list -c new-package --ready     # non-draft, CI not failing, no conflict
./triage next -c new-package --ready -o  # next unreviewed PR, opened in the browser
./triage mark done 123456 -m "approved"
./triage mark skip 123457
./triage list -s stale                   # marked done, changed since
./triage show 123456
./triage list -c other --json | jq …
```
