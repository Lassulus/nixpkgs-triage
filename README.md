# nixpkgs-triage

A local review workflow for open NixOS/nixpkgs PRs. It mirrors them into SQLite (`triage.db`),
sorts each one into a review queue using `categories.toml`, keeps your review state locally, and
runs per-PR background jobs: an omp agent that checks the contribution guidelines, and
nixpkgs-review.

Needs Python ≥ 3.11 (stdlib only) and a GitHub token: `GITHUB_TOKEN`/`GH_TOKEN`, or `gh auth token`.
Guideline checks need `omp`; nixpkgs-review needs `nix` and a nixpkgs git checkout (`~/src/nixpkgs`).

## Sync

```sh
./triage update           # first run: full sync of all open PRs, then incremental
./triage update           # later runs: only PRs updated since the last sync
./triage update --full    # re-walk every open PR (not needed routinely)
```

How it stays under GitHub's rate limits:

- **One GraphQL query per page** returns labels, up to 100 changed files, CI rollup and metadata
  for 50 PRs. That costs 1–2 points, so a full sync of ~12k PRs uses a few hundred of the
  5000 points/hour.
- **Incremental sync** walks PRs of any state newest-updated first and stops at the previous
  watermark (minus a 5-minute overlap). Merged and closed PRs drop out of the open set this way.
- **Primary limit**: each response includes `rateLimit`. When fewer than `--reserve` (500) points
  remain, the sync sleeps until the reset time, leaving headroom for your other `gh` usage.
- **Secondary limits**: requests are strictly serial with `--delay` (1s) between them.
  403/429 responses honour `Retry-After`; if there is none, the sync backs off exponentially.
- **Server timeouts**: GitHub aborts queries after about 10s (HTTP 502). When that happens the
  page size is halved, then grows back after a few successful pages.
- **Resumable**: the full-sync cursor is saved after every page. Ctrl-C and rerun to continue.
  At the end of a full sync, PRs that are still open locally but were not seen are refreshed
  by node ID.

## Categories

`categories.toml` is an ordered list of rules. The rules use nixpkgs' own labels
(`8.has: package (new)`, `8.has: module (new|update)`, `4.workflow: backport`, `10.rebuild-*`,
`1.severity: security`, …), plus title conventions (`init at`, `a -> b`, `nixos/…`, `treewide:`)
and changed paths for PRs the label bot hasn't handled yet. The first matching rule becomes the
PR's primary category, and every matching rule is kept as a tag. Topics come from the
`6.topic: *` labels.

Edit the file and the next command re-categorizes the database locally, with no API calls.

## Interactive UI

```sh
./triage ui
```

The left pane lists the categories with their open counts, and the right pane lists the PRs in
the selected category. Moving through the categories filters the PR list as you go.

PR columns:

- `age`: time since the PR was opened.
- `CI`: combined GitHub check state of the latest commit: `pass`, `FAIL`, `error`, `pending`,
  or `none` if no checks reported. Rows with failing CI are red.
- `draft`: `yes` for draft PRs.
- `mark`: your local status from `triage mark`. A `*` means the PR changed after you marked it.
- `check`: latest guideline check: `pending`, `running`, `pass`, `issues`, `FAIL` (the job itself
  failed) or `cancel`.
- `nixrev`: latest nixpkgs-review: `pending`, `running`, `pass`, `FAIL` (a build failed or the
  review errored) or `cancel`.

| key | action |
|---|---|
| `tab`, `←`/`→`, `h`/`l` | switch pane |
| `↑`/`↓`, `j`/`k`, `PgUp`/`PgDn`, `g`/`G` | move |
| `enter` | on a category: jump to its PRs; on a PR: open it in the browser (`xdg-open`) |
| `space`/`v` | detail view of the PR |
| `c` | start a guideline check for the PR |
| `n` | start nixpkgs-review for the PR |
| `d` | show or hide drafts |
| `o` | sort: oldest created → newest created → recently updated |
| `R` | refresh view: runs `triage update` and shows its log |
| `q`/`esc` | quit (in the refresh view: back to the list) |

You can leave the refresh view while the sync is still running; the footer shows its progress
and the list reloads when it finishes. In the refresh view, `c` cancels the sync. Cancelling is
safe: the sync time only advances when a sync completes, so the next refresh catches up.

### Detail view

Shows the PR's metadata, the status of both jobs (`not run`, `pending` with queue time,
`running` with duration, `success`/`failed` with the result summary, `cancelled`), and an output
pane. While a job runs, the pane follows its live log; once it finishes, the pane shows the
report.

| key | action |
|---|---|
| `c` / `n` | start the guideline check / nixpkgs-review |
| `tab` | switch the output pane between the two jobs |
| `l` | toggle log / report |
| `x` | cancel the shown job (asks first) |
| `P` | post the nixpkgs-review report as a comment on the PR (asks first; shows if already posted) |
| `enter` | open the PR in the browser |
| `↑`/`↓`, `PgUp`/`PgDn`, `g`/`G` | scroll the output |
| `q`/`esc` | back to the list |

## Background jobs

```sh
./triage check 123456 123457     # guideline check with omp
./triage review 123456           # nixpkgs-review
./triage jobs [--all]            # active (or all) jobs with status
./triage cancel JOB_ID
./triage post 123456             # post the latest nixpkgs-review report as a PR comment
```

Each job is a detached `triage job-run ID` process. Its state lives in the `jobs` table, so jobs
keep running after you quit the UI, and the UI picks up their state again when you restart it.
Output goes to `jobs/<PR>/<id>-<kind>/` (`job.log`, `report.md`). At most
`TRIAGE_MAX_CHECKS` (3) checks and `TRIAGE_MAX_REVIEWS` (1) reviews run at once; the others stay
`pending` and start in submission order. If a runner dies (kill, reboot), its job is marked
failed with "runner died". Cancelling interrupts the tool with SIGINT, so nixpkgs-review removes
its worktree.

- **Guideline check**: gets the PR from the GitHub API: `pr.md` (title, description, labels),
  `commits.txt` (full messages; merge commits marked), `diff.patch` (per-file summary and the
  diff as GitHub shows it), the changed files at the PR head, and the guideline docs from the
  target branch (CONTRIBUTING.md, the PR template, pkgs/nixos/lib/doc/maintainers READMEs).
  It doesn't use the local checkout, so shallow clones and merged PRs work too. That costs about
  3–6 API requests. omp runs on these files in print mode with only read-only tools (`read`,
  `grep`, `glob`). Its report has to cite the guideline section for every finding and ends with
  `VERDICT: PASS|ISSUES`.
- **nixpkgs-review**: `nixpkgs-review pr N --no-shell` in the nixpkgs checkout, with its cache
  directory inside the job directory. It uses GitHub's CI evaluation when possible (your token is
  passed through). Status is `success` only if nothing failed to build. `nixpkgs-review` from
  PATH is used if installed, otherwise `nix run nixpkgs#nixpkgs-review`.
- **Posting** posts the nixpkgs-review `report.md` as a comment, as your GitHub user, the same
  way `nixpkgs-review post-result` does.

## Review workflow

```sh
./triage stats                          # open / drafts / ready / done per category
./triage stats --by topic               # same by 6.topic label
./triage list -c new-package --ready    # queue: non-draft, CI not failing, no conflict/needs-changes
./triage list -c nixos-module-new -t python --sort oldest
./triage list --tag nixos-module -c package-update   # secondary tags
./triage next -c new-package --ready -o # next unreviewed PR in a queue, opened in the browser
./triage mark done 123456 -m "approved"
./triage mark skip 123457               # not my area; never shown by `next` again
./triage list -s stale                  # marked done, but the PR changed since
./triage show 123456
./triage list -c other --json | jq …
```

`next` shows PRs that have no status, are `todo`, or are `done` but were updated after you marked
them. In `list`, a `*` after the status means the PR changed since you marked it.
Review state is stored in the `reviews` table, and syncs never overwrite it.

Environment: `TRIAGE_DB` and `TRIAGE_CATEGORIES` override the file locations. For jobs:
`TRIAGE_NIXPKGS` (nixpkgs checkout, default `~/src/nixpkgs`), `TRIAGE_OMP` (omp command, default
`omp`), `TRIAGE_NIXPKGS_REVIEW` (nixpkgs-review command), `TRIAGE_JOBS_DIR`,
`TRIAGE_MAX_CHECKS`, `TRIAGE_MAX_REVIEWS`.

## Code layout

`./triage` is the entry point; the code is in `nixpkgs_triage/`:

| module | contents |
|---|---|
| `cli.py` | argument parsing, subcommands |
| `config.py` | paths and environment settings |
| `util.py` | logging, time formatting, URLs |
| `github.py` | rate-limit-aware GraphQL client and queries |
| `db.py` | SQLite schema |
| `categorize.py` | categories.toml rules |
| `sync.py` | `update`: full and incremental sync |
| `query.py` | `list`, `next`, `show`, `mark`, `stats` |
| `jobs.py` | starting, tracking, cancelling jobs; posting reports |
| `runner.py` | the detached runner: guideline check and nixpkgs-review |
| `ui.py` | curses UI |
