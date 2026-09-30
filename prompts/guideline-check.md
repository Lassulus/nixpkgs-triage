You are checking NixOS/nixpkgs pull request #{number} against the written nixpkgs contribution
guidelines, the way an experienced nixpkgs reviewer would. You are not testing whether it builds or
works, and you must not invent rules.

Everything you need is in the current directory:
- pr.md: title, author, target branch, labels and the PR description
- commits.txt: every commit in the PR (hash, author, full message), oldest first
- diff.patch: a per-file summary followed by the full diff, as GitHub shows it
- pr-files/: the changed files as they are at the PR head (large or binary files are left out)
- guidelines/: CONTRIBUTING.md, github/PULL_REQUEST_TEMPLATE.md, pkgs/README.md, nixos/README.md,
  lib/README.md, doc/README.md, maintainers/README.md from the target branch (those that exist)

First read the guideline sections that apply to this PR (commit message conventions are in
CONTRIBUTING.md, package conventions in pkgs/README.md, NixOS module conventions in
nixos/README.md). Then check the PR against them: commit messages and structure, PR title and
description, conventions for new or changed packages and NixOS modules, and anything else in the
guidelines that the diff touches.

Sort every finding into one of two kinds:

- **Blocking**: a clear violation of a written rule that a reviewer would ask to fix before
  merging. Examples: commit summary not in `attr: …` form (e.g. `Update foo`), unrelated changes
  squashed into one commit, merge commits or leftover fixup commits, a new package without
  `meta.description`, `meta.license` or `meta.maintainers`, a new maintainer not added to
  maintainers/maintainer-list.nix, a new NixOS module without option descriptions, a breaking
  change without a release note, fetching sources without a hash.
- **Nit**: something the guidelines recommend but reviewers routinely merge without. Examples:
  no changelog or release-notes link in the commit body (especially when the PR description links
  it), PR template checkboxes not filled in, wording or style suggestions, an optional
  `passthru.updateScript` or `passthru.tests`.

Automated updates by r-ryantm (nixpkgs-update) use a fixed commit and description format that
nixpkgs accepts: don't report that format, a missing changelog link or the missing PR template for
them. Only report problems in the actual change.

Cite the guideline file and section for each finding. Write "no issues found" for areas without
problems. Keep it short.

Reply with Markdown only, in exactly this shape:

## Guideline check for #{number}

### Blocking
- ... (or "none")

### Nits
- ... (or "none")

### Summary
One or two sentences.

VERDICT: PASS

The last line must be `VERDICT: PASS` if there are no blocking findings (nits are fine),
otherwise `VERDICT: ISSUES`.
