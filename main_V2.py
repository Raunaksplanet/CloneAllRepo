#!/usr/bin/env python3
"""
Bulk-clone all repos of a GitHub user/org. Cross-platform (Linux/macOS/Windows).

Usage:
    python clone_repos.py <github_url_or_username> [options]

Examples:
    python clone_repos.py https://github.com/Raunaksplanet
    python clone_repos.py Raunaksplanet --token %GITHUB_TOKEN% --include-forks
    python clone_repos.py someorg --org --workers 8 --out ./targets

Env:
    GITHUB_TOKEN   used automatically if --token not passed (raises rate limit to 5000/hr)
    GITHUB_API_URL override for GitHub Enterprise (e.g. https://ghe.company.com/api/v3)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
IS_WINDOWS = platform.system() == "Windows"

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_CHARS = '<>:"/\\|?*' + "".join(chr(c) for c in range(32))

_stop = False


def _sigint_handler(_sig, _frame):
    global _stop
    _stop = True
    print("\n[!] Interrupt received, finishing in-flight clones then exiting...", file=sys.stderr)


signal.signal(signal.SIGINT, _sigint_handler)


def check_git_available() -> str:
    git_path = shutil.which("git")
    if not git_path:
        sys.exit(
            "[-] git not found on PATH.\n"
            "    Install: https://git-scm.com/downloads "
            "(winget install --id Git.Git on Windows, brew install git on macOS, apt/dnf on Linux)"
        )
    return git_path


def parse_target(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)          # drop scheme
    raw = re.sub(r"^(www\.)?github\.com/", "", raw, flags=re.IGNORECASE)  # drop bare/with-scheme host
    raw = raw.strip("/")
    if "/" in raw:
        raw = raw.split("/")[0]
    return raw


def sanitize_dirname(name: str, is_windows: bool = IS_WINDOWS) -> str:
    safe = name
    if is_windows:
        safe = "".join("_" if c in _INVALID_CHARS else c for c in safe)
        safe = safe.rstrip(" .")
        base = safe.split(".", 1)[0].upper()  # Windows reserves the stem even with an extension
        if base in _WIN_RESERVED or not safe:
            safe = f"_{safe or 'repo'}"
    else:
        safe = safe.replace("/", "_")
    return safe or "repo"


def http_get_json(url: str, headers: dict, params: dict, timeout: int = 20, retries: int = 3):
    qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    full_url = f"{url}?{qs}" if qs else url
    last_err = None

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(full_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                # resp.headers (an email.message.Message) is case-insensitive on .get();
                # keep the object itself instead of flattening to a plain dict.
                return resp.status, resp.headers, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = None
            return e.code, e.headers, parsed
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < retries:
                wait = 2 ** attempt
                print(f"[!] Network error ({e}), retrying in {wait}s ({attempt}/{retries})...", file=sys.stderr)
                time.sleep(wait)
            continue

    sys.exit(f"[-] Network error after {retries} attempts: {last_err}")


def fetch_repos(target: str, token: str | None, is_org: bool, include_forks: bool,
                 include_archived: bool) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "clone_repos.py"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    kind = "orgs" if is_org else "users"
    repos: list[dict] = []
    page = 1

    while not _stop:
        url = f"{API_BASE}/{kind}/{target}/repos"
        params = {"per_page": 100, "page": page, "type": "all", "sort": "full_name"}
        status, resp_headers, data = http_get_json(url, headers, params)

        if status == 404:
            if not is_org:
                return fetch_repos(target, token, True, include_forks, include_archived)
            sys.exit(f"[-] '{target}' not found as user or org (404). Check spelling/case.")

        if status == 401:
            sys.exit("[-] 401 Unauthorized -- token is invalid or expired.")

        if status == 403:
            retry_after = resp_headers.get("Retry-After")
            remaining = resp_headers.get("X-RateLimit-Remaining")
            if retry_after:
                wait = int(retry_after)
                print(f"[!] Secondary rate limit hit, sleeping {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if remaining == "0":
                reset = resp_headers.get("X-RateLimit-Reset")
                reset_msg = f" Resets at epoch {reset}." if reset else ""
                sys.exit(f"[-] Rate limited (403).{reset_msg} Use --token / set GITHUB_TOKEN.")
            sys.exit("[-] 403 Forbidden (blocked, private account, or SSO-protected org).")

        if status is None or status >= 500:
            sys.exit(f"[-] Server error (status {status}) fetching repo list.")

        if status != 200:
            sys.exit(f"[-] Unexpected status {status} fetching repo list: {data}")

        if not data:
            break

        for r in data:
            if not include_forks and r.get("fork"):
                continue
            if not include_archived and r.get("archived"):
                continue
            repos.append(r)

        if len(data) < 100:
            break
        page += 1

    return repos


def _git_env(use_ssh: bool) -> dict:
    """
    Non-interactive git environment.

    GIT_TERMINAL_PROMPT=0 alone makes git fail fast instead of hanging on an
    HTTPS credential prompt -- no GIT_ASKPASS needed (and GIT_ASKPASS=echo is
    actually broken on Windows, where `echo` is a cmd.exe builtin, not an exe).

    For SSH, GIT_TERMINAL_PROMPT does NOT cover OpenSSH's own prompts (new
    host-key confirmation, password auth). Without handling that, a clone of
    a host never connected to before hangs until the subprocess timeout.
    BatchMode=yes disables all SSH interactive prompts; accept-new auto-trusts
    a first-seen host key without a fingerprint prompt. Available cross-platform
    (OpenSSH ships built in on Windows 10 1809+, macOS, and all major Linux distros).
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if use_ssh:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    return env


def git_dir_is_valid_repo(path: Path) -> bool:
    return (path / ".git").exists()


def clone_or_update(repo: dict, out_dir: Path, use_ssh: bool, git_path: str,
                     resume: bool, depth: int | None, timeout: int) -> str:
    if _stop:
        return f"[!] skipped {repo['name']} (interrupted)"

    name = sanitize_dirname(repo["name"])
    dest = out_dir / name

    if dest.exists():
        if git_dir_is_valid_repo(dest):
            if resume:
                env = _git_env(use_ssh=False)
                try:
                    result = subprocess.run(
                        [git_path, "-C", str(dest), "pull", "--ff-only", "--quiet"],
                        env=env, capture_output=True, text=True, timeout=timeout,
                    )
                    if result.returncode == 0:
                        return f"[~] updated {name}"
                    err = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown"
                    return f"[-] pull failed {name}: {err}"
                except subprocess.TimeoutExpired:
                    return f"[-] timeout updating {name}"
            return f"[+] skip {name} (exists)"
        return f"[-] skip {name} (dir exists, not a git repo -- remove manually to retry)"

    url = repo.get("ssh_url") if use_ssh else repo.get("clone_url")
    if not url:
        return f"[-] fail {name}: no clone URL in API response"

    env = _git_env(use_ssh=use_ssh)
    cmd = [git_path, "clone", "--quiet"]
    if depth:
        cmd += ["--depth", str(depth)]
    cmd += [url, str(dest)]

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return f"[-] fail {name}: timed out after {timeout}s"
    except OSError as e:
        return f"[-] fail {name}: {e}"

    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        err = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return f"[-] fail {name}: {err}"

    return f"[OK] cloned {name}"


def main():
    ap = argparse.ArgumentParser(description="Bulk-clone a GitHub user/org's repos (cross-platform)")
    ap.add_argument("target", help="GitHub profile/org URL or username")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub PAT (or set GITHUB_TOKEN)")
    ap.add_argument("--org", action="store_true", help="Force treat target as an org")
    ap.add_argument("--include-forks", action="store_true", help="Include forked repos")
    ap.add_argument("--include-archived", action="store_true", help="Include archived repos")
    ap.add_argument("--ssh", action="store_true", help="Clone via SSH instead of HTTPS")
    ap.add_argument("--workers", type=int, default=4, help="Parallel clone workers (default 4)")
    ap.add_argument("--out", default=None, help="Output directory (default: <target>-repos)")
    ap.add_argument("--resume", action="store_true", help="git pull existing repos instead of just skipping")
    ap.add_argument("--depth", type=int, default=None, help="Shallow clone depth (e.g. 1)")
    ap.add_argument("--timeout", type=int, default=300, help="Per-repo clone timeout in seconds (default 300)")
    ap.add_argument("--dry-run", action="store_true", help="List repos that would be cloned, don't clone")
    args = ap.parse_args()

    git_path = check_git_available()

    user_or_org = parse_target(args.target)
    if not re.match(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$", user_or_org):
        sys.exit(f"[-] Invalid GitHub username/org: '{user_or_org}'")

    if args.workers < 1:
        sys.exit("[-] --workers must be >= 1")

    out_dir = Path(args.out) if args.out else Path(f"{sanitize_dirname(user_or_org)}-repos")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"[-] Cannot create output dir '{out_dir}': {e}")

    if not os.access(out_dir, os.W_OK):
        sys.exit(f"[-] Output dir '{out_dir}' is not writable.")

    if IS_WINDOWS:
        abs_len = len(str(out_dir.resolve()))
        if abs_len > 150:
            print(f"[!] Output path is {abs_len} chars deep -- Windows MAX_PATH (260) may bite. "
                  f"Consider a shorter --out, or: git config --system core.longpaths true", file=sys.stderr)

    print(f"[*] Fetching repo list for '{user_or_org}'...")
    repos = fetch_repos(user_or_org, args.token, args.org, args.include_forks, args.include_archived)

    if not repos:
        sys.exit("[-] No repos found (empty account, all filtered out, or private-only with no token).")

    seen = set()
    unique_repos = []
    for r in repos:
        key = r.get("full_name") or r["name"]
        if key not in seen:
            seen.add(key)
            unique_repos.append(r)
    repos = unique_repos

    print(f"[*] Found {len(repos)} repos.")
    if args.dry_run:
        for r in repos:
            flags = []
            if r.get("private"):
                flags.append("private")
            if r.get("fork"):
                flags.append("fork")
            if r.get("archived"):
                flags.append("archived")
            tag = f" ({', '.join(flags)})" if flags else ""
            print(f"  - {r['name']}{tag}")
        return

    print(f"[*] Cloning into {out_dir}/ with {args.workers} workers "
          f"({'SSH' if args.ssh else 'HTTPS'}, timeout={args.timeout}s)...")

    ok = fail = skip = updated = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(clone_or_update, r, out_dir, args.ssh, git_path,
                      args.resume, args.depth, args.timeout): r
            for r in repos
        }
        try:
            for f in concurrent.futures.as_completed(futures):
                line = f.result()
                print(line)
                if line.startswith("[OK]"):
                    ok += 1
                elif line.startswith("[-]"):
                    fail += 1
                elif line.startswith("[~]"):
                    updated += 1
                else:
                    skip += 1
        except KeyboardInterrupt:
            global _stop
            _stop = True
            ex.shutdown(wait=True, cancel_futures=True)

    print(f"\n[*] Done. cloned={ok} updated={updated} skipped={skip} failed={fail}")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
