# MCP GitHub write-back (write tools)

How the MCP server lets an authenticated AI client **edit a skill's markdown from chat** and have the change committed and pushed to the client repo automatically — no SSH, no local clone, no redeploy. This is the feature that turns the warehouse from a passive query target into a knowledge base that improves in place.

Production reference: `pol-cc/skills-sapiens` runs this in production (FastMCP / Python). Everything below is encoded from that server's `server.py`.

## Why this exists — the iteration loop

The single most valuable property of the MCP server is that **skills get sharper every time they're used** (see [`mcp-skill-folder-pattern.md`](mcp-skill-folder-pattern.md), "Skill iteration loop"). The loop:

1. A user asks claude.ai a question. The agent writes a query.
2. The query is wrong because `context.md` is missing a definition (e.g. "active customer" should exclude returns).
3. The user points out the error **in the same chat**.
4. The agent calls a write tool to patch the relevant `.md` file.
5. The server commits + pushes to `main`. Next query — from anyone — is correct.

Without write tools, step 4 means: open a terminal, SSH to the VPS (or clone the repo), edit a file, commit, push, redeploy. That friction kills the loop in practice. Write tools collapse it into one chat turn. The fix lands the moment the user notices the problem, while the context is fresh.

This is principle 4 (GitHub-native ops) made live: the agent itself participates in the commit history, and `git log` shows every "the agent got X wrong → context.md edit" as a real commit with a real co-author.

## The two write tools

The reference deployment exposes exactly two write tools. They are deliberately narrow — they edit existing skill docs, nothing else.

| Tool | What it does |
|---|---|
| `append_to_section(skill, file_key, section, text)` | Appends `text` under an existing markdown heading in the target file. Used to add a definition, a gotcha, an example note. |
| `replace_in_file(skill, file_key, old, new)` | Replaces an exact substring. Used to correct an existing definition or fix a wrong statement. |

Both resolve `(skill, file_key)` to a concrete path (see "Path-traversal safety" below), apply the edit on the live clone, then call `_commit_and_push`.

> These edit **content within an existing skill folder**. They cannot create new skill folders or arbitrary files. That boundary is the safety model — see below.

## The mechanism

### Source of truth: repo `main`; the VPS is a live clone

The repo's `main` branch is the **single source of truth** for skill docs and server code. The VPS holds a **live git clone** at `/root/<repo>/` (mounted into the container as `/repo`). Two write paths converge on `origin/main`:

- **Human local edits** — edit on a laptop, `git commit`, `git push`, then run `deploy.sh` on the VPS.
- **Chat-driven writes** — the write tools above, which commit + push directly from the container.

Both end at the same place. There is no "the UI has state the repo doesn't" — UI-only state is forbidden (principle 4).

### PAT in the remote URL

Pushes from the container authenticate with a **fine-grained GitHub PAT** scoped to `contents:write` on **this repo only**. It is supplied as the env var `GITHUB_TOKEN` and rotates roughly every 90 days.

The token is never passed on each `git push`. Instead, on container startup `_git_setup()` bakes it into the origin remote URL:

```
https://x-access-token:${GITHUB_TOKEN}@github.com/<owner>/<repo>.git
```

So **PAT rotation only requires editing `.env` and restarting the container** — no code change, no re-clone. The token lives in container env + the remote URL inside the clone; it is never committed.

### claude-bot identity + co-author

`_git_setup()` also configures the committing identity so chat-driven commits are attributable but distinct from a human:

```bash
git config --global --add safe.directory /repo          # container UID != host UID
git config user.name  "claude-bot"
git config user.email "claude-bot@users.noreply.github.com"
```

The `safe.directory` line is required because the container runs as a different UID than the host owner of `/repo`; without it git refuses to operate on the clone ("dubious ownership").

Every chat-driven commit also records the human who asked, via a co-author trailer:

```
Co-Authored-By: <caller_login> <<caller_login>@users.noreply.github.com>
```

`<caller_login>` is the GitHub `login` claim of the authenticated user (the same claim used by the inbound allowlist — see [`mcp-server-architecture.md`](../../create-mds/references/mcp-server-architecture.md)). So the commit author is `claude-bot`, the co-author is the real person, and `git blame` tells the true story.

### `_sync_to_origin()` — self-healing before every write

Before **every** write, the server reconciles the local clone with origin:

1. **Refuse if the working tree is dirty.** A dirty tree signals a prior failed write — abort rather than commit on top of an inconsistent state.
2. `git fetch origin main`.
3. If local `HEAD` != `origin/main`, `git reset --hard origin/main`.

This makes writes **resistant to non-fast-forward errors**. Origin can move ahead of the local clone for legitimate reasons: a parallel chat session pushed, or a human pushed a local edit without redeploying. Resetting hard to `origin/main` before editing guarantees the write is built on the current tip, so the subsequent push fast-forwards cleanly.

Because `main` is the source of truth, discarding local divergence with `reset --hard` is safe — there is no local-only work worth keeping; anything valuable is already on origin.

### `_commit_and_push(message, caller_login, *paths)` — with rollback

After the edit is applied to the working tree:

1. Stage exactly the `paths` that were touched (not `git add -A`).
2. Commit with `message` plus the `Co-Authored-By: <caller_login> ...` trailer.
3. `git push origin HEAD:main`.

If the push **fails** (e.g. a race slipped a new commit onto origin between sync and push), the server rolls the local commit back:

```bash
git reset --hard HEAD~1
```

This keeps the working tree coherent with origin so the next `_sync_to_origin()` starts clean rather than inheriting an orphan commit. The tool returns one of:

- `"pushed"` — change committed and on `origin/main`.
- `"no_changes"` — the edit was a no-op (e.g. the `old` string already matched `new`); nothing committed.

Surface this return to the agent so it can tell the user whether the change actually landed.

## Path-traversal safety — scoping, not filesystem perms

The `/repo` volume is mounted **read-write** (it must be, to push). Safety does **not** come from filesystem permissions — it comes from **tool scoping**.

`_resolve_skill_file(skill, file_key)` maps a logical `file_key` to an absolute path under `skills/<skill>/`:

| `file_key` | Resolves to |
|---|---|
| `context` | `skills/<skill>/context.md` |
| `examples` | `skills/<skill>/examples.sql` |
| `sources/<name>/schema` | `skills/<skill>/sources/<name>/schema.md` |
| `sources/<name>/semantics` | `skills/<skill>/sources/<name>/semantics.md` |

After resolving, it **rejects any path that escapes `skills/<skill>/`** — a path-traversal guard catching `..`, absolute paths, and symlink tricks. So even though the volume is `rw`, the write tools can only touch files inside an **existing** skill's directory.

> SAFETY is by tool scoping, not by filesystem perms. The container *could* technically write anywhere on `/repo`, but no tool exposes that ability. The only mutations reachable from chat are "edit a doc inside an existing skill folder".

## What the write tools CANNOT do

By design, the tools are an in-place editor, not a repo manager. They cannot:

- **Create a new skill folder.** Adding `skills/<new>/` requires a local edit + push + redeploy (the `add-mcp-skill` workflow). A new skill is a structural change with a new `descriptor.json` allowlist — that warrants human review, not a chat turn.
- **Create arbitrary files** anywhere outside an existing skill folder.
- **Delete files or folders.**
- **Touch server code, the Dockerfile, `docker-compose.yml`, or `.env`.** Code changes go through local edit + `deploy.sh` (which rebuilds the image).
- **Change the `descriptor.json` table allowlist.** Widening data scope is a security decision; keep it off the chat path. (Edits are limited to the prose files: `context.md`, `examples.sql`, per-source `schema.md` / `semantics.md`.)

If the agent needs any of these, it should tell the user to do it locally and run `deploy.sh`.

## How it coexists with `deploy.sh`

`deploy.sh` is the human/code path; the write tools are the chat/content path. They share `origin/main` and stay coherent because both respect the same invariants.

`deploy.sh` preflight refuses to deploy if:

- the local working tree is dirty, or
- local `HEAD` != `origin/main`.

On the VPS, `deploy.sh` does `git fetch` + `git reset --hard origin/main`, then rebuilds the container.

Interaction notes:

- Skill `.md` files are mounted as a volume, so a **content-only** change picked up by the write tools (or a human push) is live **without a rebuild** — the container reads the file off the mounted clone. The push from the write tool already updated that clone.
- **Server code / Dockerfile** changes DO need a rebuild → that's what `deploy.sh` is for.
- If a human pushes a local content edit but forgets to run `deploy.sh`, the next chat-driven write still self-heals: `_sync_to_origin()` fetches and resets to the human's new tip before editing. No drift, no lost edit.

## Security considerations

- **PAT scope.** Fine-grained, `contents:write`, **single repo**. Not classic, not org-wide. If the token leaks, blast radius is "write to this one repo's content" — and the repo's `main` history makes any malicious edit visible and revertible.
- **PAT rotation.** ~90 days. Rotation = edit `GITHUB_TOKEN` in `.env`, restart container. No re-clone, no code change (the token is read into the remote URL on startup).
- **Inbound allowlist.** Who can call a write tool is gated by the same `ALLOWED_GITHUB_USERS` allowlist as reads. An empty allowlist means "any authenticated GitHub user" — for write tools, set an explicit allowlist of trusted editors.
- **Attribution.** `claude-bot` author + human co-author on every commit. Audit trail is `git log`.
- **No silent escalation.** The tools cannot widen the BigQuery allowlist (`descriptor.json` is off-limits) or deploy code, so a compromised chat session cannot turn read-only data access into broader access.
- **Branch protection caveat.** If `main` has branch protection requiring PRs, direct pushes from the PAT will fail. Either exempt `claude-bot` / the PAT, or run write tools against an unprotected branch and reconcile via PR. The reference deployment pushes directly to an unprotected `main`.

## Common gotchas

- **`dubious ownership in repository at '/repo'`** → the `safe.directory` config didn't run or the path is wrong. Confirm `_git_setup()` ran on startup; the container UID differs from the host owner of the clone.
- **`could not read Username` / 403 on push** → `GITHUB_TOKEN` is missing, expired, or lacks `contents:write` on this repo. Check the token in `.env` and that the origin URL contains `x-access-token:${GITHUB_TOKEN}@`.
- **Write tool returns `no_changes` unexpectedly** → for `replace_in_file`, the `old` string didn't match exactly (whitespace, smart quotes, or it was already replaced). Re-read the file via a read tool and copy the exact substring.
- **Push fails with non-fast-forward despite sync** → a parallel session pushed in the race window. The rollback (`reset --hard HEAD~1`) already cleaned up; the agent should simply retry — the next `_sync_to_origin()` will pick up the new tip.
- **Edit lands on origin but chat still sees the old answer** → the agent cached the prior `context.md` in its context window. Start a fresh turn or re-call the read tool so the agent reloads the updated file.
- **Working tree dirty, every write refused** → a prior write crashed mid-apply. SSH in, inspect `git status` on `/root/<repo>/`, `git checkout -- .` (or `git reset --hard origin/main`) to clean it, then writes resume.
- **Branch protection blocks the bot** → see "Branch protection caveat" above.

## Marker state

When write tools are enabled, record it in the deployment marker so re-runs and `verify-pipeline` know the capability is live:

```jsonc
{
  "stack": {
    "mcp": true
  },
  "decisions": {
    "mcp_write_tools": true,
    "mcp_write_tools_pat_ref": "secrets/<client>-mcp-github-pat.json",
    "mcp_write_tools_pat_rotated_at": "2026-05-29",
    "mcp_write_allowed_users": ["acme-cto", "acme-data-analyst"]
  }
}
```

`"mcp_write_tools": false` (or absent) means the server runs read-only. The PAT secret reference points at where the fine-grained token is stored in the agent's local secrets — never in the repo. Track `pat_rotated_at` so the ~90-day rotation is visible to the agent.

## Reference deployment

`pol-cc/skills-sapiens` (private) is the production reference. Its `server.py` implements `append_to_section`, `replace_in_file`, `_git_setup()`, `_sync_to_origin()`, `_commit_and_push()`, and `_resolve_skill_file()` exactly as described here, against per-source `schema.md` / `semantics.md` files. The write tools have been used in anger over months of skill iteration — the pattern works.
