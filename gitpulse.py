from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time

from git import GitCommandError, Repo
import yaml
import logging
from notifypy import Notify
from datetime import datetime


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def send_notification(title: str, message: str):
    try:
        notification = Notify()
        notification.title = title
        notification.message = message

        # Send the notification
        notification.send()
        log.info("Notification sent successfully!")
    except Exception as e:
        log.exception(f"Error sending notification: {e}")


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


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


def sync_repo(repo_cfg: dict, state: dict) -> dict | None:
    name = repo_cfg["name"]
    path = repo_cfg["path"]
    branch = repo_cfg["branch"]
    backup_prefix = repo_cfg.get("backup_branch_prefix", "backup")

    def snotify(msg: str) -> None:
        send_notification("Git Pulse", msg)

    try:
        repo = Repo(path)

        with_retry(lambda: repo.remotes.origin.fetch())

        remote_ref = f"origin/{branch}"
        remote_commit = repo.commit(remote_ref).hexsha

        if state.get(name) == remote_commit:
            log.info("No-op: %s (already at %s)", name, remote_commit[:8])
            return None

        ahead_commits = list(repo.iter_commits(f"{remote_ref}..HEAD"))
        if ahead_commits:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_branch = f"{backup_prefix}-{timestamp}"
            repo.create_head(backup_branch)
            log.info("Backup branch created: %s → %s", name, backup_branch)
            snotify(f"Backup created: {name} → {backup_branch}")

        with_retry(lambda: (repo.git.reset("--hard", remote_ref), repo.git.clean("-fd")))

        log.info("Updated: %s → %s", name, remote_commit[:8])
        snotify(f"Updated: {name} → {remote_commit[:8]}")

        return {"name": name, "commit": remote_commit}

    except GitCommandError as e:
        log.error("Git error in %s: %s", name, e)
        snotify(f"ERROR syncing {name}: {e}")
        return None
    except Exception as e:
        log.error("Unexpected error in %s: %s", name, e)
        snotify(f"ERROR syncing {name}: {e}")
        return None


def main() -> None:
    cfg = load_config("config.yaml")
    g = cfg["global"]
    max_parallel = int(g.get("max_parallel", 4))

    state_file = Path("state.json")
    state: dict = json.loads(state_file.read_text()) if state_file.exists() else {}

    repos = cfg.get("repos", [])

    # ThreadPoolExecutor gives us the same bounded-parallelism behaviour
    # as the PowerShell while-jobs.Count loop, without asyncio complexity.
    with ThreadPoolExecutor(max_workers=max_parallel) as pool:
        futures = {
            pool.submit(sync_repo, repo, state): repo
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
    main()