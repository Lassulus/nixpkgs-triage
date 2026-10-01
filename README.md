# nixpkgs-triage

A local review workflow for open NixOS/nixpkgs PRs. It mirrors them into SQLite (`triage.db`),
sorts each one into a review queue using `categories.toml`, keeps your review state locally, and
runs per-PR background jobs: an omp agent that checks the contribution guidelines, and
nixpkgs-review. You browse it in a curses UI or a web dashboard.

Needs Python ≥ 3.11 (stdlib only) and a GitHub token: `GITHUB_TOKEN`/`GH_TOKEN`, or `gh auth token`.
Guideline checks need `omp`; nixpkgs-review needs `nix` and a nixpkgs git checkout (`~/src/nixpkgs`).

## Sync

```sh
./triage update           # first run: full sync of all open PRs, then incremental
./triage update           # later runs: only PRs updated since the last sync
./triage update --full    # re-walk every open PR (not needed routinely)
```

How it stays under GitHub's rate limits:

- **One GraphQL query per page** returns labels, CI rollup and metadata (including line and file
  counts, but not the file list) for 50 PRs. That costs 1 point and about 5s of GitHub's time,
  so a full sync of ~12k PRs takes about half an hour and a few hundred of the 5000 points/hour.
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
`1.severity: security`, …), plus title conventions (`init at`, `a -> b`, `nixos/…`, `treewide:`,
`attr: …`) for PRs the label bot hasn't handled yet. The first matching rule becomes the
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
- `+/-`: lines added and deleted.
- `CI`: combined GitHub check state of the latest commit: `pass`, `FAIL`, `error`, `pending`,
  or `none` if no checks reported. Rows with failing CI are red.
- `conflict`: `yes` if the PR has the `2.status: merge conflict` label (set by the nixpkgs bot;
  as fresh as the last sync).
- `draft`: `yes` for draft PRs.
- `mark`: your local status from `triage mark`. A `*` means the PR changed after you marked it.
- `check`: latest guideline check: `pending`, `running`, `pass` (no blocking issues; nits are
  allowed), `issues` (at least one blocking issue), `FAIL` (the job itself failed) or `cancel`.
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
| `f` | filter view |
| `o` | sort: oldest created → newest created → recently updated |
| `R` | refresh view: runs `triage update` and shows its log |
| `S` | settings screen |
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
| `P` | post the nixpkgs-review report as a comment on the PR (no confirmation) |
| `enter` | open the PR in the browser |
| `↑`/`↓`, `PgUp`/`PgDn`, `g`/`G` | scroll the output |
| `q`/`esc` | back to the list |

### Filters (`f`)

| filter | options (first is the default) |
|---|---|
| drafts | hide, show, only |
| merge conflicts | hide, show, only |
| CI | any, not failing, failing |
| guideline check | any, not run, pass, issues, failed |
| nixpkgs-review | any, not run, pass, failed |

`↑`/`↓` selects a filter, and `←`/`→` or `space` changes it. Changes apply at once to the PR
list and the category counts, and `r` resets everything to the defaults. The header shows the
active filters and "N of M open". Filters are not saved; each UI start uses the defaults.

### Settings (`S`)

Lists the job settings with their values; `(default)` marks the ones you haven't changed.
`enter` edits a value in a line editor (`enter` saves, `esc` cancels, `ctrl-u` clears), and `r`
resets it to the default. Values are checked when you save: commands must parse and their
program should be in PATH, and counts must be ≥ 1. A value that parses but looks wrong (program
not found, nixpkgs checkout not a git repo) is saved with a warning.

## Web dashboard

```sh
./triage serve                                   # http://127.0.0.1:8080/, syncs every 5 minutes
./triage serve --listen '[::]:8080' --sync-every 120
./triage serve --sync-every 0                    # no sync loop (a timer or you run `triage update`)
```

The same data as the curses UI on one endlessly scrolling page: the category sidebar with
counts, the same filters and sort orders, and the same columns. Clicking a row expands its
detail inline: metadata, job states, and tabs for the guideline check and nixpkgs-review
(report, or the live log while the job runs, refreshed every 3s). Category, sort and filters
are kept in the URL, so views can be bookmarked. The page checks for new data every 30s. If the
list is scrolled to the top with no detail open, it reloads the list; otherwise it shows a "new
data" button.

To stay fast: the first view is one API request, and the filtered and sorted list is cached until
the data changes, so later pages and category switches answer in well under a millisecond. JSON
is gzipped, static files are revalidated by ETag, and connections are kept alive. Opening a
detail fetches the PR and the shown job output in parallel.

It is **read-only**: no starting, cancelling or posting jobs, and no authentication. Anyone who
can reach it sees the PR data and the job reports and logs. For hosting, keep it on localhost
behind a reverse proxy that adds TLS and, if needed, auth.

The server keeps the open PRs in memory and reloads them when the database changes (syncs, job
runners and the curses UI write to the same `triage.db`). Its sync loop runs `triage update`
`--sync-every` seconds (default 300) after the previous run ended, and the sync log goes to
stderr. An incremental run costs about 2 points per 50 changed PRs, so even a 1-minute interval
stays far below GitHub's 5000 points/hour. Only one `triage update` runs at a time: a second one
(the UI's `R`, a manual run) exits with "another `triage update` is running".

JSON API: `/api/status`, `/api/prs?category=&sort=&<filter>=&after=&limit=` (keyset-paged; `next`
is the cursor for `after`), `/api/pr/N`, `/api/pr/N/check|review[?log=1]`.

### NixOS module

The flake exports `nixosModules.default` (and `packages.<system>.default`):

```nix
{
  inputs.nixpkgs-triage.url = "git+https://…/nixpkgs-triage";  # or path:/home/lass/src/nixpkgs-triage

  outputs = { nixpkgs, nixpkgs-triage, ... }: {
    nixosConfigurations.server = nixpkgs.lib.nixosSystem {
      modules = [
        nixpkgs-triage.nixosModules.default
        {
          services.nixpkgs-triage = {
            enable = true;
            environmentFile = "/run/secrets/nixpkgs-triage";  # GITHUB_TOKEN=…
          };
        }
      ];
    };
  };
}
```

| option | default | |
|---|---|---|
| `address` / `port` | `127.0.0.1` / `8080` | where the dashboard listens (no auth: use a reverse proxy) |
| `openFirewall` | `false` | open `port` |
| `syncInterval` | `300` | seconds between syncs; `0` disables the loop |
| `environmentFile` | `null` | systemd EnvironmentFile with `GITHUB_TOKEN`; required while syncing |
| `package` | built with the server's nixpkgs | |

The service runs as a hardened `DynamicUser`, with `triage.db` and `jobs/` in
`/var/lib/nixpkgs-triage`. Its first sync is a full one (about 40 minutes). `nix flake check`
runs a VM test of the module.

### Push updates from GitHub

Webhooks for NixOS/nixpkgs need admin rights on that repository (or a GitHub App installed by
the NixOS org), so a third-party dashboard can't receive them. The ways to get close:

- **Poll more often** (what the sync loop does): the incremental query already asks "what changed
  since last time", so `--sync-every 60` gives about 1-minute freshness for about 120 points/hour.
- **Repository events API** (`GET /repos/NixOS/nixpkgs/events` with `If-None-Match`): 304
  responses don't count against the rate limit, so it could trigger a sync only when something
  happened. GitHub delays events by 30s to 6h, and the feed holds only the latest 300 events,
  which on nixpkgs can cover less than a polling interval. It works as a trigger, not as the source
  of truth.

## Background jobs

```sh
./triage check 123456 123457     # guideline check (agent)
./triage review 123456           # nixpkgs-review
./triage jobs [--all]            # active (or all) jobs with status
./triage cancel JOB_ID
./triage post 123456             # post the latest nixpkgs-review report as a PR comment
./triage settings                # list settings; `settings KEY VALUE` sets, `settings KEY --reset`
```

Each job is a detached `triage job-run ID` process. Its state lives in the `jobs` table, so jobs
keep running after you quit the UI, and the UI picks up their state again when you restart it.
Output goes to `jobs/<PR>/<id>-<kind>/` (`job.log`, `report.md`). At most `max_checks` (3) checks
and `max_reviews` (1) reviews run at once; the others stay
`pending` and start in submission order. If a runner dies (kill, reboot), its job is marked
failed with "runner died". Cancelling interrupts the tool with SIGINT, so nixpkgs-review removes
its worktree.

Job settings are stored in `triage.db` and edited on the settings screen or with `triage settings`.
A job reads them when it starts running, so changes also apply to jobs that are still pending.

| setting | default | used as |
|---|---|---|
| `agent_command` | `s omp` | the omp command for guideline checks |
| `agent_model` | empty | `--model` for the agent; empty uses omp's default |
| `review_command` | `nixpkgs-review` if in PATH, else `nix run nixpkgs#nixpkgs-review --` | `<command> pr N --no-shell --build-graph nix <arguments>` |
| `review_args` | empty | extra nixpkgs-review arguments, e.g. `--systems '…'`, `--tests` |
| `nixpkgs_dir` | `~/src/nixpkgs` | checkout nixpkgs-review runs in |
| `max_checks` / `max_reviews` | 3 / 1 | parallel jobs per kind |

If a job fails, its summary shows the tool's last `error:` line, e.g. when the agent command
doesn't exist.

- **Guideline check**: gets the PR from the GitHub API: `pr.md` (title, description, labels),
  `commits.txt` (full messages; merge commits marked), `diff.patch` (per-file summary and the
  diff as GitHub shows it), the changed files at the PR head, and the guideline docs from the
  target branch (CONTRIBUTING.md, the PR template, pkgs/nixos/lib/doc/maintainers READMEs).
  It doesn't use the local checkout, so shallow clones and merged PRs work too. That costs about
  3–6 API requests. The agent (`agent_command`, omp flags) runs on these files in print mode with
  only read-only tools (`read`, `grep`, `glob`). Its report has two sections:
  - **Blocking:** clear violations a reviewer would want fixed before merging, e.g. commit
    summaries not in `attr: …` form, fixup or merge commits, a new package without meta or
    maintainers.
  - **Nits:** things the guidelines recommend but that routinely get merged anyway, e.g. no
    changelog link in the commit body, unticked template boxes.

  Each finding cites its guideline section. The verdict is `PASS` unless something is blocking,
  and r-ryantm's standard nixpkgs-update format counts as accepted. The prompt is
  `prompts/guideline-check.md` (`{number}` is the PR number); edit it to change what counts as
  blocking. The next check uses the edited prompt.
- **nixpkgs-review**: `review_command pr N --no-shell` in `nixpkgs_dir`, with its cache
  directory inside the job directory. Status is `success` only if nothing failed to build.
  Before a review starts, the job checks whether GitHub CI has a usable evaluation of the PR
  head: a non-expired `comparison` artifact, which is what nixpkgs-review downloads.
  - **Available, or CI still evaluating:** nixpkgs-review uses it (your token is passed through).
  - **Missing, e.g. artifacts expired on older PRs:** the job adds `--eval local`. Otherwise
    nixpkgs-review would poll for 10 minutes and then fail with "No evaluation seems to be
    available on GitHub".
  - **Turns out unusable anyway:** if nixpkgs-review still reports that, the job retries once
    with `--eval local`.
  - **Your override:** an explicit `--eval …` in `review_args` is left alone.

  Reviews that evaluated locally have `local eval` in their summary.
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

Environment: `TRIAGE_DB`, `TRIAGE_CATEGORIES` and `TRIAGE_JOBS_DIR` override the file locations.

## Code layout

`./triage` is the entry point; the code is in `nixpkgs_triage/`:

| module | contents |
|---|---|
| `cli.py` | argument parsing, subcommands |
| `config.py` | paths |
| `settings.py` | job settings (stored in the database) |
| `util.py` | logging, time formatting, URLs |
| `github.py` | rate-limit-aware GraphQL client and queries |
| `db.py` | SQLite schema |
| `categorize.py` | categories.toml rules |
| `sync.py` | `update`: full and incremental sync; `SyncJob` (`update` as a child process) |
| `query.py` | `list`, `next`, `show`, `mark`, `stats` |
| `jobs.py` | starting, tracking, cancelling jobs; posting reports |
| `runner.py` | the detached runner: guideline check and nixpkgs-review |
| `listing.py` | the open-PR list shared by both UIs: filters, sort orders, category counts |
| `ui.py` | curses UI |
| `web.py`, `static/` | `serve`: web dashboard, JSON API, sync loop |
