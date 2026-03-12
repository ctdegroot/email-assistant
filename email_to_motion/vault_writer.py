"""
vault_writer.py — Push generated Markdown notes to an Obsidian vault via Git.

Stage 2 of the notes pipeline: after note_generator writes a local .md file,
this module commits it to the vault's Git repository and pushes to the remote
so the Obsidian Git plugin can pull it on the Mac.

Prerequisites:
  - OBSIDIAN_VAULT_PATH: absolute (or ~/…) path to a local clone of the vault
  - OBSIDIAN_NOTES_SUBFOLDER: subfolder within the vault where notes go
      e.g. "Notes/Inbox" — will be created automatically if missing
  - The clone must have 'origin' configured with SSH/HTTPS write access.

Enable by setting OBSIDIAN_DELIVERY=git in .env.
When OBSIDIAN_DELIVERY is 'local' (or unset), this module is never called and
the note only lives in NOTES_OUTPUT_PATH.
"""

from pathlib import Path

import git  # gitpython

from . import config


def push_to_vault(markdown: str, filename: str) -> Path:
    """
    Write a Markdown note into the Obsidian vault and push it to the remote.

    Workflow:
      1. Resolve the vault path and ensure the notes subfolder exists.
      2. Write the note file.
      3. Pull latest from origin (fast-forward only) to avoid push conflicts.
      4. Stage the new file, commit, and push.

    Returns the Path of the written note inside the vault.
    Raises git.GitCommandError (or any other exception) on failure —
    the caller (slack_notes_handler) catches this and reports to Slack.
    """
    vault_path = Path(config.OBSIDIAN_VAULT_PATH).expanduser().resolve()
    notes_dir  = vault_path / config.OBSIDIAN_NOTES_SUBFOLDER
    notes_dir.mkdir(parents=True, exist_ok=True)

    note_path = notes_dir / filename
    note_path.write_text(markdown, encoding="utf-8")

    repo = git.Repo(vault_path)

    # Pull → commit → push, with one automatic retry on push rejection.
    # A rejection (non-fast-forward) can happen if Obsidian Git on the Mac
    # auto-pushes a manual note in the narrow window between our pull and push.
    # Retrying after a fresh pull resolves it cleanly.
    _commit_and_push(repo, note_path, vault_path, filename)

    return note_path


def _commit_and_push(
    repo: git.Repo,
    note_path: Path,
    vault_path: Path,
    filename: str,
    *,
    _retry: bool = True,
) -> None:
    """Pull, stage, commit, and push.  Retries once on push rejection."""
    repo.remotes.origin.pull()

    relative = str(note_path.relative_to(vault_path))
    repo.index.add([relative])
    repo.index.commit(f"Add note: {filename}")

    push_info = repo.remotes.origin.push()

    # push_info is a list of PushInfo objects; flag 4 = REJECTED, 32 = REMOTE_REJECTED
    rejected = any(pi.flags & (git.remote.PushInfo.REJECTED | git.remote.PushInfo.REMOTE_REJECTED)
                   for pi in push_info)

    if rejected:
        if _retry:
            # Reset the local commit so we can re-pull and re-commit cleanly
            repo.git.reset("HEAD~1")
            _commit_and_push(repo, note_path, vault_path, filename, _retry=False)
        else:
            raise git.GitCommandError("push", "rejected after retry — check vault Git state")
