---
name: koa-toolkit-development
description: "Use when changing, releasing or syncing KOA toolkit."
version: 2.0.0
author: KOA
license: MIT
metadata:
  hermes:
    tags: [koa, hermes-profile, release, git]
    related_skills: [koa-cluster-discovery, cluster-incident-analysis, hermes-agent]
---

# KOA toolkit: change → test → push → profile update

## When to Use

- Adding or fixing a `koa/` tool, catalog entry, doc, SOUL or KOA skill.
- Pulling new commits from the KOA remote into the installed profile.
- The user asks to commit/push and update the profile.
- Changing how cluster knowledge is read, added or suggested (`koa/knowledge.py`, `koa/web.py`).

KOA ships as a Hermes profile distribution. Two copies exist:
- **dev checkout**: the git repo whose remote is `source:` in the installed `distribution.yaml`.
  All edits go here. If its location is unknown, find it with `mdfind -name distribution.yaml | grep -v
  /.hermes/` and confirm `git remote -v` matches `source:` (ripgrep over `~` can fail on protected folders).
  Never edit `koa/`, `docs/` or distributed skills in the installed profile: the next update overwrites them.
- **installed profile**: `~/.hermes/profiles/<profile>/`. `hermes profile update` overwrites the
  `distribution_owned` paths (SOUL.md, config.yaml template, skills/, koa/, k8s/, terminal/, docs/,
  README.md). Profile state that must survive: `config.yaml` (registered MCP servers), `.env`,
  `local/` (cluster profiles, knowledge, suggestions).

## Always-on rules

- **Code must run on any cluster and any machine.** No cluster names, namespaces, absolute local
  paths, or hard-coded kubeconfig in `koa/`, docs examples, or skills. Cluster access goes through the
  shared path (kubernetes MCP by default; kubectl via `KOA_KUBECONFIG`/`--kubeconfig`, refusing
  `~/.kube/config`); result/secret paths through `koa/paths.py`. Classify things by discovery profile →
  catalog image rules → protocol/well-known port, never by name. Before commit, grep the new code for
  the lab cluster's names and `/Users/`.
- **Scratch vs tool.** One-off exploration scripts go in `$HERMES_HOME/cache/scratch/` and are deleted
  when done. If an investigation is worth repeating, turn it into a general rule in a `koa/` tool plus a
  synthetic-data test — do not hand the user a cluster-specific script as "the method".
- **Read-only on the cluster.** New tools only get/list/log-read; print what was sent (call counts) on
  the first output line so the user can audit it. Reachability GETs are allowed; requests that make an
  app do work need the user's OK (SOUL).
- Keep scripts Python 3.9-compatible (macOS system python) and stdlib + PyYAML only.
- **Scope stays with the user.** KOA has no cluster-wide sweep, triage or report pipeline (removed in
  0.2.0 on purpose). Don't add tools that scan everything up front or write report files; new analysis
  helpers take the component/flow names the user gave (`knowledge.context`) and read only those.
- **Knowledge changes only through the user.** Tools never edit `local/knowledge/<c>/flows.yaml` on their
  own: they call `knowledge.add_suggestion` / `suggest_from_flowmap`, and the user accepts in the web UI
  or tells the agent to `knowledge.py add`. `apply_additions` only adds (merges list fields, fills empty
  fields, skips flows already inside a known flow, adds aliases to a flow named with no nodes, fills an
  empty note `kind`) — never deletes or overwrites.
- **Notes carry a kind; topic notes are cluster-wide.** `normal` items are notes `{component, note, kind}`
  (kinds in `knowledge.NOTE_KINDS`). A `component` that is not a component/group name or alias is a topic
  note: `context` always offers it (relevance first, `TOPIC_BUDGET` chars). Keep that budget when adding
  note sources, and measure with `koa/stats.py` (real sessions) or `lab/chaos/compare.py --repeat` (lab)
  before claiming an analysis got faster or cheaper.

## Syncing the installed profile from the remote

1. `git -C <checkout> fetch` → `git log --oneline HEAD..origin/main` + `git diff --stat` to know what
   changed; check which remote branches are already merged before reviewing them.
2. `diff -rq` the installed `distribution_owned` paths against the checkout — any installed-only edit
   must be moved into the checkout first or it is lost.
3. `git pull --ff-only`, back up (step 5 of the release list), update, re-diff.

## Release checklist (user says "푸시하고 프로필 업데이트해")

1. Tests and static checks in the checkout:
   ```bash
   for t in koa/tests/test_*.py; do python3 "$t" || exit 1; done
   for f in koa/*.py; do python3 -m py_compile "$f"; done
   python3 -c "import yaml;yaml.safe_load(open('koa/catalog.yaml'))"
   ```
   Then grep new code for environment-specific strings and scan `git diff` + untracked files for secrets.
1b. `diff -rq <checkout>/skills <installed>/skills/<category>` for KOA skills that exist only in the
   installed profile (e.g. a skill created by an agent mid-session): copy them into the checkout and commit
   them, or the update deletes them. Hermes-bundled skills (listed in `skills/.bundled_manifest`) stay out
   of the repo; just include them in the backup.
2. Bump `version:` in `distribution.yaml` (the update message shows it; same version hides the change).
3. Update the human docs with the change (`docs/koa-*.md` section, `koa/README.md` tool table, SOUL
   tool list if a new tool) and the governing skill in the checkout.
4. Commit with a body listing what/why per area, `git push origin main`.
5. Back up profile state: `cp config.yaml` + `cp -R local` into
   `backups/before-update-<version>-<timestamp>/`.
6. `env -u HERMES_HOME ~/.local/bin/hermes profile update <profile> -y` (the agent's terminal exports
   this profile's `HERMES_HOME`; unset it so the CLI resolves the profile root itself).

## Verify after update

| Check | Expected |
|---|---|
| tracked files checkout vs installed | identical except `config.yaml` (registered MCPs kept), `distribution.yaml` (install metadata added); `lab/`, `clusters/`, `.gitignore` are not distributed |
| `config.yaml`, `local/` vs backup | unchanged |
| tests run from the installed copy | OK |
| `python3 koa/query.py --probe` | every registered MCP ✅ |
| the new tool, run once from the installed copy | same result as from the checkout |

Then restart the web (`launchctl kickstart -k gui/$(id -u)/ai.koa.web`, below) so it serves the new code;
SOUL/skill changes apply from a new chat.

## Restarting `koa/web.py` (user asks "웹 재시작해", or after a profile update)

On this Mac the web runs as a LaunchAgent `ai.koa.web` (`~/Library/LaunchAgents/ai.koa.web.plist`, KeepAlive,
log `$HERMES_HOME/logs/koa-web.log`). Don't start it as a Hermes background process: those are killed when the
agent's process cleanup runs (that is how 8765 died once) — and don't `pkill` it either, launchd restarts it.
1. `launchctl kickstart -k gui/$(id -u)/ai.koa.web` (restart with the new code).
2. Verify with read-only calls: `GET /` → 200; `GET /api/knowledge/<c>` → counts, `check == []`; `GET /api/learned`.
   (All POSTs need header `X-KOA: 1` and write knowledge — don't use them to verify.)
3. Tell the user to refresh the browser tab.
On a machine without the LaunchAgent: copy that plist (paths adjusted) and `launchctl bootstrap gui/$(id -u) <plist>`.
The page also does this itself: it shows the running version and folder in the top bar, and when `distribution.yaml`
on disk has another version than the running server (`GET /api/about`: `version` vs `disk_version`) it shows a
"웹 다시 시작" button. `POST /api/restart` imports the new code in a child process first and, if that works, `exec`s
the same command in place (same PID, so launchd/KeepAlive is unaffected). A server older than 0.2.18 has no
`/api/about`; the page then says to restart it by hand.

## Testing technique

- Web UI changes before release: copy the checkout's `koa/` + `distribution.yaml` into
  `$HERMES_HOME/cache/scratch/<dir>/`, add an empty `memories/` (makes `paths.INSTALLED` true) and **copy** the profile's `local/`
  (not a symlink: accept/dismiss/incident clicks write knowledge), then run `/usr/bin/python3 -u koa/web.py --port <other port>` there. The
  real 8765 server stays untouched; check with the browser tool + `browser_console` (expect no JS errors),
  measure table column widths via a console expression. To see suggestion cards, seed one with
  `python3 koa/knowledge.py suggest <c> '<json>' --title …` in the copy. Delete the scratch dir afterwards.
- The browser tool uses the real Chrome profile (`browser.use_real_profile`); if it fails with a profile
  lock, ask the user to quit Chrome rather than changing that setting.
- Korean text in narrow table cells breaks one character per line under `overflow-wrap: anywhere`; use
  `word-break: keep-all` + a cell `min-width` for markdown tables.
- Tests are plain `unittest` files runnable as `python3 koa/tests/test_<x>.py` (no pytest).
- Feed rule functions synthetic objects (dicts shaped like `kubectl get -o json` items, fake log lines)
  — no cluster needed, so they run anywhere.
- For anything that writes `local/knowledge/`, point `knowledge.cluster_dir` at a `tempfile` dir in the
  test; never touch the real knowledge folder from a test.
- Validate against the lab cluster only after the synthetic tests pass; compare with known-good
  hand-made results (e.g. existing knowledge flows must come back as "already present").
- Prove a new test catches the bug: copy the installed (old) module over the checkout file, run the
  test (it must fail), restore. A test that also passes on old code tests nothing.
- Before/after harness for rule changes (e.g. `knowledge.context`): load the installed and checkout
  modules side by side with `importlib.util.spec_from_file_location` under different names, run the
  same sample questions through `context` against the real `local/knowledge/<c>/flows.yaml` (pass `data=`, `incidents=[]`),
  print only rows whose output changed. Include questions that must NOT change (other flows) as a guard.
  Keep the harness in scratch; show the user the before/after table.

## Learned skill (`koa/learned.py`)

- KOA's own lessons go to the `koa-learned` skill, which is **not in the repo**: `hermes profile update`
  replaces only the files the distribution ships (per-file merge), so a profile-only skill survives while
  edits to shipped skills (`cluster-incident-analysis`, ...) are overwritten. Never add `koa-learned` to the
  repo; to keep a lesson for every install, promote it into the shipped skill in the checkout.
- "Skills KOA made or changed" = `koa-learned` ∪ skills in `skills/.curator_ledger.jsonl` ∪ `.usage.json`
  records with `created_by: agent`. Bundled skills (in `.bundled_manifest`) never show unless edited.
- The shipped `config.yaml` sets `auxiliary.background_review.enabled: false`: the background review fork
  creates skills unrelated to cluster analysis without asking (it once made a session-recovery skill). New
  skills need the user's OK (SOUL 5). `hermes profile update` keeps the installed `config.yaml`, so a new
  config key in the repo must also be applied to existing installs with
  `env -u HERMES_HOME hermes -p <profile> config set <key> <value>` (never hand-edit or yaml-dump it).
- The web does not show or edit memory (removed in 0.2.13: memory belongs to Hermes' own memory tools).
  Memory changes reach the agent only in a new chat (system prompt is frozen per session).

## Knowledge rules (`knowledge.py context` / `add`) — pitfalls

- A flow node is a component name, matched exactly (`node_to_comps`; the web `known()` uses the same rule).
  No alias or substring matching for flow nodes: two rules (fuzzy server, exact UI) made the screen and the
  analysis disagree. Aliases are only for finding components in the user's question (`mentions`,
  `name_to_comps`). A node may also be a `group` name (components share one `group`); it resolves to all members.
- `flow_nodes` is gone. `load_flows` runs `migrate` once on old files (rewrites nodes, turns shared names into a `group`,
  moves the rest to aliases) and saves with `flows.yaml.pre-names.bak` — the one place a tool rewrites
  knowledge without a suggestion, approved by the user (2026-10-12) as a format change, not new content.
- `context` returns the named components + every flow through them + the components on those flows +
  incidents touching them + their normal patterns. It must not grow beyond that (no "related" guesses).
- `apply_additions` must not invent fields: adding a flow does not add its node names to aliases
  (that silently changes which flows later questions match). Test with a temp `cluster_dir`.
- Suggestions are deduplicated by content key and remember dismissals (`suggestions.yaml: dismissed`);
  a flowmap re-run must leave 0 new suggestions when nothing changed (idempotency check).
- Components added from flowmap arrive without `aliases`, so everyday names ("그라파나 접속이 안 돼") match
  nothing. After the user accepts, propose KO+EN aliases for them (one `knowledge.py add`), then run
  `knowledge.py check <c>` (must stay clean) and `context` on one question per new component.
- Check what the web UI sees with `GET /api/knowledge/<c>`: count `understood.components/flows/normal`
  and expect `check == []`.
- The web saves its whole state, so it sends `base` (`version` = hash of the flows.yaml it read + that state).
  `save_shards(base=…)` merges with whatever was written meanwhile (`merge3`: per-field for components, list
  fields and normal patterns keep both sides' adds/removes) and raises `Conflict` only when both sides changed
  the same field or flow differently. Any read-modify-write of flows.yaml passes `base` the same way
  (`apply_additions` does). Without it, a stale web page erases what `knowledge.py add` just wrote.
- Normal patterns and incidents may name a group (`component: wrapper`); `context` and `check` resolve it with
  `node_to_comps` like a flow node. An incident's group counts as one place for the "two components in scope" rule.
