"""
MCP server skeleton — BigQuery read tools + GitHub write-back, FastMCP / Python.
=============================================================================

This is a STARTER you copy into the client's MDS repo and customize. It is
consistent with:
  - create-mds/references/mcp-bigquery-server-deploy.md  (overall shape, env vars)
  - add-mcp-skill/references/mcp-github-writeback.md      (the write-tools mechanism)
  - add-mcp-skill/references/mcp-skill-folder-pattern.md  (skills/<name>/ layout)

What it exposes:
  - run_bq_query(skill, sql)   read-only, SELECT-only, scoped to the skill's
                               descriptor.json table allowlist, byte + row caps
  - list_skills()              skills discovered under <repo>/skills
  - get_skill_context(skill)   returns the skill's context.md / schema.md / examples.sql
  - append_to_section(...)     WRITE tool: append under a markdown heading, open a PR
  - replace_in_file(...)       WRITE tool: replace an exact substring, open a PR

WRITE TOOLS ARE OFF BY DEFAULT. They register only when MCP_WRITE_TOOLS is on
(and a GITHUB_TOKEN is present). The server exposes externally-ingested
(untrusted) data to an AI agent, so a write tool is a prompt-injection path:
attacker-controlled synced row -> agent -> repo write. Two controls break that
chain: (1) writes default OFF; (2) when ON they OPEN A PULL REQUEST for human
review — they NEVER push to main. See
add-mcp-skill/references/mcp-github-writeback.md ("Security posture").

SAFETY MODEL (do not weaken these):
  - SELECT-only: every statement parsed must be a single SELECT (no DML/DDL/procedures).
  - Table allowlist: a BQ dry-run reports referenced tables; any table not in the
    skill's descriptor.json `tables[]` is rejected.
  - Caps: maximum_bytes_billed + MAX_ROWS on every query.
  - Write tools OFF by default: MCP_WRITE_TOOLS must be explicitly on to register them.
  - PR-not-push: enabled write tools commit to a claude-bot/edit-<ts> branch and
    open a PR (base=main) via the GitHub API. They never push to main.
  - Path-traversal guard: write tools resolve only to files INSIDE skills/<skill>/.
  - Sync-before-write: _sync_to_origin() refuses a dirty tree and hard-resets to
    origin/main before branching, so the PR diffs cleanly against current main.
  - Rollback-on-failure: a failed branch push or PR-open deletes the orphan branch.

Sections marked `# TODO: harden` are intentionally simplified — fill them in for
production. The SAFETY-critical code below is NOT a stub.

NOTE ON FASTMCP API: symbols used here (FastMCP, GitHubProvider, @mcp.tool,
mcp.run(transport="streamable-http", ...)) mirror what the reference docs show.
Verify the auth-context accessor (`_current_token_login()` below) against the
installed FastMCP version — the exact accessor for the verified token's `login`
claim may differ between releases.  # verify against installed FastMCP version
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml  # noqa: F401  (used by skill loaders / future descriptor extensions)
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from google.cloud import bigquery

# Some FastMCP releases expose the verified token via a context dependency.
# Import is best-effort; _current_token_login() degrades gracefully if absent.
try:  # verify against installed FastMCP version
    from fastmcp.server.dependencies import get_access_token  # type: ignore
except Exception:  # pragma: no cover
    get_access_token = None  # type: ignore


# --- configuration -----------------------------------------------------------

REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo")).resolve()
SKILLS_DIR = REPO_DIR / "skills"

GCP_PROJECT = os.environ.get("GCP_PROJECT")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")
MAX_BYTES_BILLED = int(os.environ.get("MAX_BYTES_BILLED", 2 * 1024 ** 3))  # 2 GiB
MAX_ROWS = int(os.environ.get("MAX_ROWS", 1000))

ALLOWED_GITHUB_USERS = [
    u.strip() for u in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",") if u.strip()
]

# --- write-tools gate: OFF BY DEFAULT ----------------------------------------
# Write tools are a prompt-injection surface (untrusted synced data -> agent ->
# repo write), so they register only when MCP_WRITE_TOOLS is explicitly on.
# When on, they OPEN A PR (base=main) for human review — they never push to main.
_TRUTHY = {"1", "true", "on", "yes"}
WRITE_TOOLS_FLAG = os.environ.get("MCP_WRITE_TOOLS", "off").strip().lower() in _TRUTHY
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # fine-grained PAT, needed only for writes
PR_BASE_BRANCH = os.environ.get("MCP_PR_BASE", "main")

# Writes require BOTH the flag AND a token. Fail loudly on a half-configured
# server rather than silently running read-only when the operator meant to write.
if WRITE_TOOLS_FLAG and not GITHUB_TOKEN:
    raise RuntimeError(
        "MCP_WRITE_TOOLS is on but GITHUB_TOKEN is unset. The fine-grained PAT "
        "(scopes: contents:write + pull_requests:write) is required to push the "
        "branch and open the PR. Set GITHUB_TOKEN, or set MCP_WRITE_TOOLS=off."
    )
WRITE_TOOLS_ENABLED = WRITE_TOOLS_FLAG and bool(GITHUB_TOKEN)

# Owner/repo for the PR API call; populated by _git_setup() when writes are on.
_REPO_OWNER: str | None = None
_REPO_NAME: str | None = None


# --- inbound auth: GitHub OAuth + username allowlist -------------------------

auth = GitHubProvider(
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    base_url=os.environ["PUBLIC_BASE_URL"],  # OAuth callback base; no trailing slash
)

mcp = FastMCP("client-mds", auth=auth)


def _current_token_login() -> str:
    """Read the `login` claim from the verified access token.

    # verify against installed FastMCP version: the accessor and the exact
    # claim key ('login') may differ. GitHubProvider stores the GitHub username
    # on the verified token's claims.
    """
    if get_access_token is None:
        # TODO: harden — wire to the real FastMCP auth context for this version.
        raise PermissionError("auth_context_unavailable")
    token = get_access_token()
    claims = getattr(token, "claims", None) or {}
    login = claims.get("login") or getattr(token, "login", None)
    if not login:
        raise PermissionError("no_login_claim_on_token")
    return str(login)


def _authorize_request() -> str:
    """Verify the caller and enforce the allowlist. Returns the GitHub login.

    Empty ALLOWED_GITHUB_USERS == any authenticated GitHub user.
    """
    login = _current_token_login()
    if ALLOWED_GITHUB_USERS and login not in ALLOWED_GITHUB_USERS:
        raise PermissionError(f"github_user_not_allowed: {login}")
    return login


# --- BigQuery client ---------------------------------------------------------

bq = bigquery.Client(project=GCP_PROJECT, location=BQ_LOCATION)


# --- skill loading -----------------------------------------------------------

def _load_descriptor(skill: str) -> dict:
    """Read and validate skills/<skill>/descriptor.json. Guards the skill name too."""
    # Guard the skill name before touching the filesystem.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill or ""):
        raise ValueError(f"invalid_skill_name: {skill!r}")
    descriptor_path = (SKILLS_DIR / skill / "descriptor.json").resolve()
    # Defense in depth: the resolved descriptor must stay under SKILLS_DIR/<skill>/.
    skill_root = (SKILLS_DIR / skill).resolve()
    if not str(descriptor_path).startswith(str(skill_root) + os.sep):
        raise PermissionError("path_escape_detected")
    if not descriptor_path.is_file():
        raise FileNotFoundError(f"skill_not_found: {skill}")
    import json
    return json.loads(descriptor_path.read_text(encoding="utf-8"))


# --- SAFETY: SELECT-only assertion -------------------------------------------

# Statement keywords that must never appear as the leading statement. We strip
# comments and leading CTEs, then require the executable statement to be SELECT.
_FORBIDDEN_LEADING = (
    "insert", "update", "delete", "merge", "create", "drop", "alter",
    "truncate", "grant", "revoke", "call", "declare", "begin", "export",
    "load", "replace",
)


def _strip_sql_comments(sql: str) -> str:
    # Remove block comments and line comments so keyword checks see real tokens.
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _assert_select_only(sql: str) -> None:
    """Reject anything that isn't a single read-only SELECT statement.

    SAFETY-critical: no DML, no DDL, no procedures, no multi-statement scripts.
    """
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise ValueError("empty_query")

    # Reject multiple statements (allow a single optional trailing semicolon).
    body = cleaned[:-1] if cleaned.endswith(";") else cleaned
    if ";" in body:
        raise ValueError("multiple_statements_not_allowed")

    # The executable statement must start with SELECT or WITH (a CTE that
    # terminates in a SELECT). Disallow any forbidden leading keyword outright.
    first_token = re.split(r"\s+", body.lstrip("("), maxsplit=1)[0].lower()
    if first_token in _FORBIDDEN_LEADING:
        raise ValueError(f"non_select_statement: {first_token}")
    if first_token not in ("select", "with"):
        raise ValueError(f"only_select_allowed: starts with {first_token!r}")

    # A WITH...AS(...) chain must ultimately run a SELECT, not a DML CTE
    # (BigQuery has no DML-in-CTE, but block the keywords defensively anyway).
    lowered = body.lower()
    for kw in _FORBIDDEN_LEADING:
        # word-boundary match so e.g. "created_at" doesn't trip "create"
        if re.search(rf"\b{kw}\b", lowered):
            raise ValueError(f"forbidden_keyword_in_query: {kw}")


# --- SAFETY: table allowlist via BQ dry-run ----------------------------------

def _referenced_tables(sql: str) -> set[str]:
    """Run a dry-run job and return the fully-qualified tables the query reads.

    A dry-run does NOT scan data (no cost) but BigQuery resolves and reports the
    referenced tables, which is far more reliable than regex-parsing SQL.
    """
    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    tables = set()
    for ref in (job.referenced_tables or []):
        tables.add(f"{ref.project}.{ref.dataset_id}.{ref.table_id}")
    return tables


def _assert_tables_in_allowlist(skill: str, sql: str) -> None:
    """SAFETY-critical: reject queries touching tables outside the skill's allowlist."""
    descriptor = _load_descriptor(skill)
    allowlist = set(descriptor.get("tables", []))
    if not allowlist:
        raise PermissionError(f"skill_has_no_table_allowlist: {skill}")
    referenced = _referenced_tables(sql)
    out_of_scope = referenced - allowlist
    if out_of_scope:
        raise PermissionError(f"tables_not_in_skill_allowlist: {sorted(out_of_scope)}")


# --- read tool ---------------------------------------------------------------

@mcp.tool
def run_bq_query(skill: str, sql: str) -> list[dict]:
    """Run a read-only SELECT scoped to the skill's descriptor.json allowlist.

    Enforces: caller allowlist, SELECT-only, table allowlist (dry-run),
    maximum_bytes_billed, and MAX_ROWS.
    """
    _authorize_request()
    _assert_select_only(sql)
    _assert_tables_in_allowlist(skill, sql)

    # Per-skill caps may tighten (never widen) the server defaults.
    descriptor = _load_descriptor(skill)
    bytes_cap = min(int(descriptor.get("max_query_bytes", MAX_BYTES_BILLED)), MAX_BYTES_BILLED)
    row_cap = min(int(descriptor.get("max_rows", MAX_ROWS)), MAX_ROWS)

    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=bytes_cap,
            dry_run=False,
            use_query_cache=True,
        ),
    )
    return [dict(row) for row in job.result(max_results=row_cap)]


# --- discovery / context tools -----------------------------------------------

@mcp.tool
def list_skills() -> list[dict]:
    """List skills discovered under <repo>/skills with a valid descriptor.json."""
    _authorize_request()
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for child in sorted(SKILLS_DIR.iterdir()):
        if not child.is_dir():
            continue
        try:
            descriptor = _load_descriptor(child.name)
        except Exception:
            continue  # skip folders without a valid descriptor
        out.append({
            "name": descriptor.get("name", child.name),
            "description": descriptor.get("description", ""),
            "version": descriptor.get("version", ""),
        })
    return out


@mcp.tool
def get_skill_context(skill: str) -> dict:
    """Return the skill's context.md, schema.md and examples.sql for the agent."""
    _authorize_request()
    _load_descriptor(skill)  # validates name + existence
    skill_root = (SKILLS_DIR / skill).resolve()

    def _read(name: str) -> str:
        p = (skill_root / name).resolve()
        if not str(p).startswith(str(skill_root) + os.sep):
            raise PermissionError("path_escape_detected")
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    return {
        "context": _read("context.md"),
        "schema": _read("schema.md"),
        "examples": _read("examples.sql"),
    }


# =============================================================================
# WRITE TOOLS — edit skill docs, then OPEN A PR (base=main) for human review.
# OFF BY DEFAULT (register only when MCP_WRITE_TOOLS is on). They NEVER push to
# main: the edit lands on a claude-bot/edit-<ts> branch and a PR is opened.
# See add-mcp-skill/references/mcp-github-writeback.md for the full mechanism.
# =============================================================================

# Logical file_key -> path inside skills/<skill>/. Edits are limited to prose
# files; descriptor.json is deliberately NOT editable from chat (widening the
# table allowlist is a security decision that warrants human review).
_FILE_KEY_MAP = {
    "context": "context.md",
    "examples": "examples.sql",
    # skills-sapiens per-source layout:
    # "sources/<name>/schema":    "sources/<name>/schema.md"
    # "sources/<name>/semantics": "sources/<name>/semantics.md"
}


def _resolve_skill_file(skill: str, file_key: str) -> Path:
    """Map (skill, file_key) -> absolute path, REJECTING anything escaping skills/<skill>/.

    SAFETY-critical path-traversal guard. Catches `..`, absolute paths, and
    symlink tricks by resolving the real path and asserting it stays under the
    skill root. Safety is by tool scoping, not by filesystem perms (the /repo
    mount is read-write so the tools can push).
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill or ""):
        raise ValueError(f"invalid_skill_name: {skill!r}")

    skill_root = (SKILLS_DIR / skill).resolve()
    if not skill_root.is_dir():
        raise FileNotFoundError(f"skill_not_found: {skill}")

    # Resolve the file_key to a relative path within the skill folder.
    if file_key in _FILE_KEY_MAP:
        rel = _FILE_KEY_MAP[file_key]
    else:
        # Support the skills-sapiens per-source keys: sources/<name>/{schema,semantics}
        m = re.fullmatch(r"sources/([a-z0-9][a-z0-9_-]*)/(schema|semantics)", file_key or "")
        if not m:
            raise ValueError(f"unknown_file_key: {file_key!r}")
        rel = f"sources/{m.group(1)}/{m.group(2)}.md"

    # Resolve and enforce containment. realpath collapses .. and follows symlinks,
    # so a symlink pointing outside the skill root is caught by the prefix check.
    candidate = (skill_root / rel).resolve()
    if candidate != skill_root and not str(candidate).startswith(str(skill_root) + os.sep):
        raise PermissionError(f"path_escape_detected: {file_key!r}")
    if candidate.is_dir():
        raise PermissionError("refusing_to_edit_directory")
    return candidate


# --- git helpers -------------------------------------------------------------

def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside the repo clone."""
    return subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _git_setup() -> None:
    """On startup: safe.directory, claude-bot identity, PAT-in-remote-URL.

    Idempotent. Only runs when write tools are enabled. A read-only server (the
    default) never touches git config or the remote URL.
    """
    global _REPO_OWNER, _REPO_NAME
    if not WRITE_TOOLS_ENABLED:
        print("[mcp] write tools: disabled (read-only) — git not configured")
        return

    # safe.directory is required: the container UID differs from the host owner
    # of /repo; without it git refuses to operate ("dubious ownership").
    _git("config", "--global", "--add", "safe.directory", str(REPO_DIR))
    _git("config", "user.name", "claude-bot")
    _git("config", "user.email", "claude-bot@users.noreply.github.com")

    # Bake the fine-grained PAT into the origin remote URL so the branch push
    # authenticates without passing the token per-command. Rotation = edit .env
    # + restart. The same PAT is reused for the PR-open API call (_open_pr).
    remote = _git("config", "--get", "remote.origin.url").stdout.strip()
    # TODO: harden — parse owner/repo robustly; this assumes an https github URL.
    m = re.search(r"github\.com[/:]([^/]+)/(.+?)(?:\.git)?$", remote)
    if not m:
        raise RuntimeError(f"cannot_parse_origin_remote: {remote!r}")
    _REPO_OWNER, _REPO_NAME = m.group(1), m.group(2)
    authed = (
        f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
        f"{_REPO_OWNER}/{_REPO_NAME}.git"
    )
    _git("remote", "set-url", "origin", authed)
    print(f"[mcp] git_setup: safe.directory={REPO_DIR}, identity=claude-bot, "
          f"origin=x-access-token@github.com/{_REPO_OWNER}/{_REPO_NAME}, "
          f"mode=PR-not-push (base={PR_BASE_BRANCH})")


def _sync_to_origin() -> None:
    """SAFETY-critical: refuse a dirty tree, return to base, hard-reset to origin.

    Ensures the next edit branches from the CURRENT tip of the base branch. Origin
    can legitimately move ahead (a prior PR was merged, or a human pushed). A prior
    write may also have left us checked out on a stale claude-bot/* branch, so we
    return to the base branch first. Discarding local divergence is safe because
    origin/<base> is the single source of truth and the claude-bot/* branches are
    already pushed.
    """
    status = _git("status", "--porcelain").stdout.strip()
    if status:
        # A dirty tree signals a prior failed write — abort rather than build on
        # an inconsistent state.
        raise RuntimeError("working_tree_dirty_refusing_write")
    # Return to the base branch (no-op if already there) so we never branch off a
    # leftover claude-bot/* tip.
    _git("checkout", PR_BASE_BRANCH)
    _git("fetch", "origin", PR_BASE_BRANCH)
    local = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("rev-parse", f"origin/{PR_BASE_BRANCH}").stdout.strip()
    if local != remote:
        _git("reset", "--hard", f"origin/{PR_BASE_BRANCH}")


def _open_github_pr(branch: str, title: str, body: str) -> str:
    """POST /repos/{owner}/{repo}/pulls — open a PR for `branch` against the base.

    Returns the PR html_url. Uses the same fine-grained PAT as the branch push;
    that PAT needs `pull_requests:write` (the push needed `contents:write`).
    # verify against installed GitHub API: payload keys and the html_url field
    # are the documented shape, but pin to the API version your image targets.
    """
    if not (_REPO_OWNER and _REPO_NAME):
        raise RuntimeError("repo_owner_unknown_did_git_setup_run")
    url = f"https://api.github.com/repos/{_REPO_OWNER}/{_REPO_NAME}/pulls"
    payload = json.dumps({
        "title": title,
        "head": branch,
        "base": PR_BASE_BRANCH,
        "body": body,
        "maintainer_can_modify": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "claude-bot-mcp",
        },
    )
    # gh CLI equivalent (if `gh` is installed in the image instead of calling the API):
    #   gh pr create --head <branch> --base <base> --title <title> --body <body>
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 (https only)
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 403 = missing pull_requests:write; 422 = dup/empty
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"pr_open_failed: HTTP {exc.code}: {detail}") from exc
    return data.get("html_url", f"(pr opened on {branch}, url unavailable)")


def _open_pr(message: str, caller_login: str, *paths: Path) -> str:
    """Branch, stage `paths`, commit, push the BRANCH, open a PR. NEVER push to main.

    SAFETY-critical posture: write tools only PROPOSE. The edit lands on a
    claude-bot/edit-<ts> branch and a PR is opened against the base branch for
    human review. On any failure, the orphan branch is cleaned up and we return
    to base so the next _sync_to_origin() starts clean.

    Returns the PR html_url on success, or "no_changes" if the edit was a no-op.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    branch = f"claude-bot/edit-{ts}"

    # Stage only the touched files — never `git add -A`.
    rels = [str(p.relative_to(REPO_DIR)) for p in paths]

    # Nothing changed => the edit was a no-op. Don't create a branch or a PR.
    _git("add", "--", *rels)
    if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return "no_changes"
    # Reset the index; we re-stage on the branch below to keep the flow explicit.
    _git("reset", "--quiet", check=False)

    # Create the work branch off the synced base, then commit the edit there.
    _git("checkout", "-b", branch)
    _git("add", "--", *rels)
    full_message = (
        f"{message}\n\n"
        f"Co-Authored-By: {caller_login} <{caller_login}@users.noreply.github.com>"
    )
    _git("commit", "-m", full_message)

    # Push the BRANCH (HEAD:<branch>) — explicitly NOT HEAD:main.
    push = _git("push", "origin", f"HEAD:{branch}", check=False)
    if push.returncode != 0:
        _cleanup_branch(branch, pushed=False)
        raise RuntimeError(f"branch_push_failed_rolled_back: {push.stderr.strip()}")

    # Open the PR for human review. On failure, delete the now-orphaned remote branch.
    body = (
        f"Proposed by `claude-bot` on behalf of @{caller_login} via an MCP write tool.\n\n"
        f"{message}\n\n"
        f"Edits skill docs only. Review the diff before merging — this server "
        f"exposes externally-ingested data to an AI agent, so treat the change "
        f"as a *proposal*, not a vetted edit."
    )
    try:
        return _open_github_pr(branch, title=message, body=body)
    except Exception:
        _cleanup_branch(branch, pushed=True)
        raise


def _cleanup_branch(branch: str, pushed: bool) -> None:
    """Best-effort rollback: return to base, delete the local (and pushed) branch."""
    _git("checkout", PR_BASE_BRANCH, check=False)
    _git("branch", "-D", branch, check=False)
    if pushed:
        _git("push", "origin", "--delete", branch, check=False)


# --- markdown edit helpers ---------------------------------------------------

def _append_under_heading(path: Path, section: str, text: str) -> None:
    """Append `text` under the markdown heading `section` (matched by text).

    Inserts just before the next heading of the same-or-higher level, or at EOF.
    """
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)

    # Find the target heading line (e.g. "## Channels" matches section "Channels").
    heading_idx = None
    heading_level = None
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if m and m.group(2).strip() == section.strip():
            heading_idx = i
            heading_level = len(m.group(1))
            break
    if heading_idx is None:
        raise ValueError(f"section_not_found: {section!r}")

    # Find the end of the section: next heading at same-or-higher level, else EOF.
    insert_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= heading_level:
            insert_idx = j
            break

    block = ("\n" if not text.startswith("\n") else "") + text.rstrip("\n") + "\n"
    new_lines = lines[:insert_idx] + [block] + lines[insert_idx:]
    path.write_text("".join(new_lines), encoding="utf-8")


def _replace_substring(path: Path, old: str, new: str) -> None:
    """Replace the FIRST exact occurrence of `old` with `new`. No-op if absent."""
    content = path.read_text(encoding="utf-8")
    if old not in content:
        return  # no-op leaves the tree clean; _open_pr then returns "no_changes"
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


# --- the two write tools -----------------------------------------------------

if WRITE_TOOLS_ENABLED:

    @mcp.tool
    def append_to_section(skill: str, file_key: str, section: str, text: str) -> str:
        """Append text under a markdown heading in a skill doc, then OPEN A PR.

        Returns the PR URL (for human review) or "no_changes". Never pushes to main.
        """
        login = _authorize_request()
        path = _resolve_skill_file(skill, file_key)  # path-traversal guarded
        _sync_to_origin()                             # checkout base + reset to origin
        _append_under_heading(path, section, text)
        return _open_pr(f"docs({skill}): append to {section}", login, path)

    @mcp.tool
    def replace_in_file(skill: str, file_key: str, old: str, new: str) -> str:
        """Replace an exact substring in a skill doc, then OPEN A PR.

        Returns the PR URL (for human review) or "no_changes". Never pushes to main.
        """
        login = _authorize_request()
        path = _resolve_skill_file(skill, file_key)
        _sync_to_origin()
        _replace_substring(path, old, new)
        return _open_pr(f"docs({skill}): replace text", login, path)


# --- entrypoint --------------------------------------------------------------

if __name__ == "__main__":
    _git_setup()  # safe.directory, identity, PAT remote URL (no-op if read-only)
    print(f"[mcp] connected to BigQuery project {GCP_PROJECT} ({BQ_LOCATION})")
    print(f"[mcp] allowlist: {ALLOWED_GITHUB_USERS or '[any authed GitHub user]'}")
    if WRITE_TOOLS_ENABLED:
        print(f"[mcp] write tools: ENABLED — PR-not-push (base={PR_BASE_BRANCH}); "
              f"edits open a PR for human review, never push to main")
    else:
        print("[mcp] write tools: disabled (read-only) — set MCP_WRITE_TOOLS=on "
              "+ GITHUB_TOKEN to enable")
    # verify against installed FastMCP version: transport name + kwargs.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
