---
name: dn
description: Organize a messy folder by content and build an AI-readable knowledge base with the Distill Nexus CLI (`dn`). Use when the user says "このフォルダを整理して", "organize this folder", "sort these files", "build a knowledge base / glossary from these documents", or asks to undo a previous organization.
---

<!-- @covers AC-080, AC-105 -->

# Distill Nexus (`dn`) — organize files and distill knowledge

All classification and knowledge decisions are made **inside the `dn` CLI** (its own prompts and
schemas), so the result is the same whether a human or you run it. Do not classify files yourself,
do not move files with shell commands, and do not edit `plan.json` by hand. Your job is to run the
CLI, read its `--json` output, summarize it for the user, and get their approval before anything moves.

## Prerequisites

- `dn --version` works (install: `uv tool install .` in the distill-nexus repo, or `pipx install .`).
- `ANTHROPIC_API_KEY` is set. If it is not, stop and ask the user; do not work around it.
  (`--dry-llm` only produces placeholder output for wiring checks — never present it as a result.)
- The target is a specific folder. `dn` refuses `/`, a drive root or the home directory itself;
  do not pass `--i-know-what-i-am-doing` unless the user explicitly asked for exactly that.

## Procedure: organize a folder

1. **Taxonomy.** If `<dir>/taxonomy.yaml` does not exist, run
   `dn plan <dir> --propose-taxonomy --json`, show the categories from
   `<dir>/.dn/taxonomy.proposed.yaml` (slug, name, description), and ask the user to accept or edit
   them. Save the accepted version as `<dir>/taxonomy.yaml`. Do not continue without it.
2. **Cost check (optional).** `dn status <dir> --json` → report `estimate.cost_usd`. If the user gave
   a budget, pass it as `--max-cost <usd>` in the next step (exit code 4 = over budget, nothing sent).
3. **Plan (dry run, moves nothing).** Run `dn plan <dir> --json`.
   - Exit code **not 0** → stop. Report `error` (and `.dn/errors.jsonl` entries for exit 3) to the
     user. **Do not run `dn apply`.**
   - Exit code 0 → read `plan.summary` (counts per op) and `plan.entries[]`
     (`from`, `to`, `category`, `confidence`, `op`). Summarize for the user:
     how many files move where (group by category), the `misc` files, low-confidence files
     (`confidence` < 0.6), duplicates (`op` = `move` into `_duplicates/`, `trash`, or `skip`).
     Point out any `trash` entries: those cannot be restored by `dn undo`.
4. **Approval.** Ask the user explicitly whether to apply this plan. If they decline, change
   nothing — do not run `dn apply`. (To adjust categories, they edit `taxonomy.yaml`; rerun step 3.)
5. **Apply only after approval.** `dn apply <dir> --yes --json`.
   - Exit 0: report `apply.moved` / `copied` / `trashed` and the `run_id`.
   - Exit 3: some files failed (e.g. locked by another program); list them from `.dn/errors.jsonl`.
   - Exit 1: pre-flight validation failed (files changed since the plan); nothing moved. Rerun step 3.
   - Without a TTY `dn apply` requires `--yes`; never add `--yes` before the user approved.
   Images and scanned PDF pages are not sent to the API unless the user asks for `--images`
   (or sets `images: true`); if the plan shows many image files classified as `misc`, offer `--images`.
6. **Knowledge base.** `dn distill <dir> --json` builds `<dir>/organized/_knowledge/`. Tell the user
   to start from `INDEX.md`. `dn export <dir> --format md` bundles it into one file to paste into a chat.

## Undo

`dn undo <dir> --json` restores the latest apply run (`--run-id <id>` for an older one).
Report `undo.restored` and every entry of `undo.skipped` / `undo.trashed` (exit 3 means some files
need manual attention).

## Reading `--json` output

stdout is exactly one JSON document; progress goes to stderr. Common keys: `exit_code`, `run_id`,
`errors` (count in `.dn/errors.jsonl`), `warnings`, `llm.cost_usd`, and on failure `error`.
Exit codes: 0 ok, 1 error, 2 configuration / arguments, 3 some files failed, 4 cost limit.
