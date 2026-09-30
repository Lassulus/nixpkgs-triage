# nixpkgs-triage

A local review workflow for open NixOS/nixpkgs PRs. It mirrors them into SQLite (`triage.db`),
sorts each one into a review queue using `categories.toml`, and keeps your review state locally.

Needs Python ≥ 3.11 (stdlib only) and a GitHub token: `GITHUB_TOKEN`/`GH_TOKEN`, or `gh auth token`.

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
- `review`: your local status from `triage mark`. A `*` means the PR changed after you marked it.

| key | action |
|---|---|
| `tab`, `←`/`→`, `h`/`l` | switch pane |
| `↑`/`↓`, `j`/`k`, `PgUp`/`PgDn`, `g`/`G` | move |
| `enter` | on a category: jump to its PRs; on a PR: open it in the browser (`xdg-open`) |
| `d` | show or hide drafts (drafts are marked `D`) |
| `o` | sort: oldest created → newest created → recently updated |
| `R` | refresh view: runs `triage update` and shows its log |
| `q`/`esc` | quit (in the refresh view: back to the list) |

You can leave the refresh view while the sync is still running; the footer shows its progress
and the list reloads when it finishes. In the refresh view, `c` cancels the sync. Cancelling is
safe: the sync time only advances when a sync completes, so the next refresh catches up.

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

Environment: `TRIAGE_DB` and `TRIAGE_CATEGORIES` override the file locations.
