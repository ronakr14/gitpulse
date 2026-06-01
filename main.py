"""
git_sync.py — parallel git repo sync with Telegram notifications and idempotency.

Dependencies:
    pip install pyyaml gitpython requests
    (no python-telegram-bot needed)
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import certifi
import requests
import yaml
from git import GitCommandError, Repo
import os
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from dotenv import load_dotenv


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Requests session — certifi CA bundle fixes SSL on Windows Poetry venvs
# ---------------------------------------------------------------------------

class _CertifiAdapter(HTTPAdapter):
    """Force requests to use certifi's CA bundle regardless of OS trust store."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.load_verify_locations(certifi.where())
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)


_session = requests.Session()
_session.mount("https://", _CertifiAdapter())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Telegram  (plain requests — no SDK version dependency)
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = _session.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        # resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        # print(resp.raise_for_status())
    except requests.RequestException as e:
        log.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

def with_retry(fn, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                raise
            sleep_sec = 2 * attempt
            log.warning(
                "Attempt %d/%d failed (%s). Retrying in %ds…",
                attempt, retries, e, sleep_sec,
            )
            time.sleep(sleep_sec)


# ---------------------------------------------------------------------------
# Per-repo sync  (runs in a thread)
# ---------------------------------------------------------------------------

def sync_repo(repo_cfg: dict, state: dict, bot_token: str, chat_id: str) -> dict | None:
    """
    Returns {name, commit} on update, None on no-op.
    Thread-safe: reads state but never writes it (caller handles state update).
    """
    name          = repo_cfg["name"]
    path          = repo_cfg["path"]
    branch        = repo_cfg["branch"]
    backup_prefix = repo_cfg.get("backup_branch_prefix", "backup")

    def tg(msg: str) -> None:
        send_telegram(bot_token, chat_id, msg)

    try:
        repo = Repo(path)

        with_retry(lambda: repo.remotes.origin.fetch())

        remote_ref    = f"origin/{branch}"
        remote_commit = repo.commit(remote_ref).hexsha

        if state.get(name) == remote_commit:
            log.info("No-op: %s (already at %s)", name, remote_commit[:8])
            return None

        ahead_commits = list(repo.iter_commits(f"{remote_ref}..HEAD"))
        if ahead_commits:
            timestamp     = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_branch = f"{backup_prefix}-{timestamp}"
            repo.create_head(backup_branch)
            log.info("Backup branch created: %s → %s", name, backup_branch)
            tg(f"Backup created: {name} → {backup_branch}")

        with_retry(lambda: (repo.git.reset("--hard", remote_ref), repo.git.clean("-fd")))

        log.info("Updated: %s → %s", name, remote_commit[:8])
        tg(f"Updated: {name} → {remote_commit[:8]}")

        return {"name": name, "commit": remote_commit}

    except GitCommandError as e:
        log.error("Git error in %s: %s", name, e)
        tg(f"ERROR syncing {name}: {e}")
        return None
    except Exception as e:
        log.error("Unexpected error in %s: %s", name, e)
        tg(f"ERROR syncing {name}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg          = load_config("config.yaml")
    g            = cfg["global"]
    bot_token    = os.getenv("TELEGRAM_BOT_ID")
    chat_id      = os.getenv("TELEGRAM_CHAT_ID")
    max_parallel = int(g.get("max_parallel", 4))

    state_file = Path("state.json")
    state: dict = json.loads(state_file.read_text()) if state_file.exists() else {}

    repos = cfg.get("repos", [])

    # ThreadPoolExecutor gives us the same bounded-parallelism behaviour
    # as the PowerShell while-jobs.Count loop, without asyncio complexity.
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(sync_repo, repo, state, bot_token, chat_id): repo
            for repo in repos
        }

        for future in as_completed(futures):
            repo_cfg = futures[future]
            try:
                result = future.result()
                if result:
                    state[result["name"]] = result["commit"]
            except Exception as e:
                log.error("Unhandled exception for %s: %s", repo_cfg["name"], e)

    state_file.write_text(json.dumps(state, indent=2))
    log.info("State saved to %s", state_file)


if __name__ == "__main__":
    load_dotenv()
    main()