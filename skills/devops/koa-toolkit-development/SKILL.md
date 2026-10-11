---
name: koa-toolkit-development
description: "Use when changing, releasing or syncing KOA toolkit."
version: 1.0.0
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
- Merging generated candidates (e.g. flowmap) into `local/knowledge/`.

KOA ships as a Hermes profile distribution. Two copies exist:
- **dev checkout**: the git repo whose remote is `source:` in the installed `distribution.yaml`.
  All edits go here. If its location is unknown, find it with `mdfind -name distribution.yaml | grep -v
  /.hermes/` and confirm `git remote -v` matches `source:` (ripgrep over `~` can fail on protected folders).
  Never edit `koa/`, `docs/` or distributed skills in the installed profile: the next update overwrites them.
- **installed profile**: `~/.hermes/profiles/<profile>/`. `hermes profile update` overwrites the
  `distribution_owned` paths (SOUL.md, config.yaml template, skills/, koa/, k8s/, terminal/, docs/,
  README.md). Profile state that must survive: `config.yaml` (registered MCP servers), `.env`,
  `local/` (cluster profiles, knowledge, requests).

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

Then tell the user: a running `koa/web.py` still serves the old code until restarted; SOUL/skill
changes apply from a new chat (restart the desktop app).

## Testing technique

- Tests are plain `unittest` files runnable as `python3 koa/tests/test_<x>.py` (no pytest).
- Feed rule functions synthetic objects (dicts shaped like `kubectl get -o json` items, fake log lines)
  — no cluster needed, so they run anywhere.
- For anything that writes `local/knowledge/`, point `knowledge.cluster_dir` at a `tempfile` dir in the
  test; never touch the real knowledge folder from a test.
- Validate against the lab cluster only after the synthetic tests pass; compare with known-good
  hand-made results (e.g. existing knowledge flows must come back as "already present").
- Prove a new test catches the bug: copy the installed (old) module over the checkout file, run the
  test (it must fail), restore. A test that also passes on old code tests nothing.
- Before/after harness for rule changes (e.g. `knowledge.plan`): load the installed and checkout
  modules side by side with `importlib.util.spec_from_file_location` under different names, run the
  same sample questions against the real `local/knowledge/<c>/flows.yaml` (pass `data=`, `incidents=[]`),
  print only rows whose output changed. Include questions that must NOT change (other flows) as a guard.
  Keep the harness in scratch; show the user the before/after table.

## Knowledge plan rules (`knowledge.py plan`) — pitfalls

- Flow-node → component matching is substring-based (aliases, `flow_nodes`). A node that equals a
  component name must resolve to that component only, or `x-worker` leaks into the component whose alias
  is `x` and drags unrelated flows into the plan. Keep that exact-name short-circuit in `node_to_comps`.
- Paths must reach the end of the flow for request-level symptoms ("X 생성이 안 돼" → pending/errors):
  the target pod can be fine while the queue consumer or an external call fails. Walk downstream within
  each hit flow only (don't hop across flows), and resolve flow-node names (`대상 사이트`) to components so
  their `normal` patterns attach.
- After any change, rerun `knowledge.py check <c>` (must stay clean) and the before/after harness.

## Cluster knowledge after a flowmap merge

- Regenerate the candidate file right before merging (an old file reflects old config/logs) and back up
  `local/knowledge/<c>/`. Re-running the same merge must report "더할 흐름 0" (idempotency check).
- Merged components arrive without `aliases`, so `plan` finds no target for everyday names ("그라파나
  접속이 안 돼" → 대상 ?). Add distinct KO+EN aliases right after merging: `K.load_flows(c)` → edit
  `components[n]["aliases"]` → `K.save_shards(c, {components, flows: [{name, kind, nodes:
  K._flow_nodes(f)}], normal})`; then `knowledge.py check` and `plan` on one question per new component
  plus the old sample questions (their plans must not change).
- Check what the web UI sees with `GET /api/knowledge/<c>`: count `understood.components/flows/normal`
  and expect `check == []`; the `text` field is the prose view, not the flow list.
