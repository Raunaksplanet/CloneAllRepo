# clone_repos.py

Bulk-clone (or update) every repo of a GitHub user or org. Cross-platform
(Linux / macOS / Windows), zero third-party dependencies.

## Requirements

- Python 3.8+
- `git` on your PATH

**Verify both:**

```bash
python3 --version      # Windows: python --version
git --version
```

If `git` is missing:

| OS | Install |
|---|---|
| Windows | `winget install --id Git.Git -e` or https://git-scm.com/download/win |
| macOS | `brew install git` (or it prompts to install Xcode CLT the first time you run `git`) |
| Linux | `sudo apt install git` / `sudo dnf install git` / `sudo pacman -S git` |

The script itself checks for `git` on startup and exits with an install hint if it's not found.

## Quick start

```bash
# Public repos only, no auth needed
python3 clone_repos.py https://github.com/torvalds

# Include forks
python3 clone_repos.py torvalds --include-forks

# Your own account, including PRIVATE repos
python3 clone_repos.py <your-username> --private --token $GITHUB_TOKEN

# Everything: your repos + forks + private
python3 clone_repos.py <your-username> --private --include-forks --token $GITHUB_TOKEN

# An org, including private repos you have access to
python3 clone_repos.py my-org --org --private --token $GITHUB_TOKEN
```

## Getting a GitHub token (needed for `--private`, and recommended for everything else)

Unauthenticated API calls are capped at **60 requests/hour**; a token raises that
to **5,000/hour** and is *required* to see private repos at all.

### Fine-grained token (recommended)

1. https://github.com/settings/personal-access-tokens/new
2. **Resource owner**: yourself, or the org (if cloning org repos)
3. **Repository access**: "All repositories" (or select specific ones)
4. **Permissions → Repository permissions**:
   - `Contents`: Read-only
   - `Metadata`: Read-only (mandatory, auto-selected)
5. Generate, copy the `github_pat_...` value once (it's not shown again)

If the target is an **org with SSO enabled**, you must also click
**"Authorize"** next to that org for the token after creating it, or every
API call returns `403`.

### Classic token (simpler, broader access)

1. https://github.com/settings/tokens/new
2. Scopes: check **`repo`** (full control of private repos) — or just
   **`public_repo`** if you don't need `--private`
3. Generate, copy the `ghp_...` value once

### Using the token

```bash
# Linux/macOS
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
python3 clone_repos.py yourname --private

# Windows PowerShell
$env:GITHUB_TOKEN="ghp_xxxxxxxxxxxx"
python clone_repos.py yourname --private

# Windows cmd.exe
set GITHUB_TOKEN=ghp_xxxxxxxxxxxx
python clone_repos.py yourname --private

# or pass it explicitly instead of an env var
python3 clone_repos.py yourname --private --token ghp_xxxxxxxxxxxx
```

## `--private` — how it actually works (and its limit)

GitHub's public listing endpoints (`/users/{name}/repos`, and `/orgs/{org}/repos`
for a *plain* user target) **never return private repos, token or not** — that's
a GitHub API restriction, not a bug in this script. To see private repos of
your own account, the script switches to the authenticated `/user/repos`
endpoint instead, which only works when:

- the target username **is the account the token belongs to**, or
- the target is an **org** you belong to (org private repos already work
  through the normal org endpoint, given a token with access)

Trying `--private` against someone else's username fails with a clear error —
there's no API path to another user's private repos short of being an org
admin, which this script doesn't attempt.

## Cloning private repos over HTTPS: does git need more setup?

No extra git config needed — the script passes your token to the GitHub API
to *find* the repos, and git itself authenticates the actual `clone`/`pull`
over HTTPS using the credential embedded by GitHub's `clone_url` transaction,
**provided your token has repo access**. If you hit an auth prompt/failure
during the actual `git clone` step (not the listing step), your token likely
lacks the `Contents: Read` (fine-grained) or `repo` (classic) scope — recheck
the scopes above.

### Cloning over SSH instead (`--ssh`)

Uses your existing SSH key, not the token. Verify SSH access first:

```bash
ssh -T git@github.com
# should say "Hi <username>! You've successfully authenticated..."
```

If that fails, set up a key: https://docs.github.com/en/authentication/connecting-to-github-with-ssh

## All options

```
python3 clone_repos.py <target> [options]

  --token TOKEN        GitHub PAT (or set GITHUB_TOKEN env var)
  --org                Force treat target as an org (auto-detected on 404 otherwise)
  --private            Include private repos (requires --token; own account or org only)
  --include-forks      Include forked repos (excluded by default)
  --include-archived   Include archived repos (excluded by default)
  --ssh                Clone via SSH instead of HTTPS
  --workers N           Parallel clone workers (default 4)
  --out DIR            Output directory (default: <target>-repos)
  --resume             git pull existing repos instead of skipping them
  --depth N            Shallow clone (e.g. --depth 1 for latest commit only)
  --timeout SECONDS    Per-repo clone/pull timeout (default 300)
  --dry-run            List what would be cloned without cloning anything
```

## Notes / known limitations

- Re-running the script skips repos already cloned; add `--resume` to `git pull`
  them instead of skipping.
- First-time SSH clones auto-accept the host key (`StrictHostKeyChecking=accept-new`)
  so they don't hang waiting for interactive confirmation.
- Windows: very deeply nested `--out` paths can hit the 260-char `MAX_PATH`
  limit; the script warns if your output path is already long. Fix with a
  shorter `--out`, or `git config --system core.longpaths true`.
- Ctrl+C finishes any clones already in progress before exiting, rather than
  killing them mid-write.
