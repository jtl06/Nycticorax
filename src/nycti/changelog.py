from __future__ import annotations

from dataclasses import dataclass
import subprocess

from nycti.config import Settings


@dataclass(frozen=True, slots=True)
class ChangelogAnnouncement:
    channel_id: int
    content: str
    fingerprint: str


def build_changelog_announcement(
    settings: Settings,
    *,
    commit_subject_reader=None,
    commit_sha_reader=None,
) -> ChangelogAnnouncement | None:
    if settings.changelog_channel_id is None:
        return None

    read_subject = commit_subject_reader or _read_latest_commit_subject
    read_sha = commit_sha_reader or _read_latest_commit_sha

    message = settings.changelog_message or read_subject()
    if not message:
        return None

    version = settings.changelog_version or read_sha()
    lines = [f"changelog: {message}"]
    if version:
        lines.append(f"version: `{version}`")
    fingerprint = version or message
    return ChangelogAnnouncement(
        channel_id=settings.changelog_channel_id,
        content="\n".join(lines),
        fingerprint=fingerprint,
    )


def _read_latest_commit_subject() -> str | None:
    return _run_git_command(["git", "log", "-1", "--pretty=%s"])


def _read_latest_commit_sha() -> str | None:
    return _run_git_command(["git", "rev-parse", "--short", "HEAD"])


def _run_git_command(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = completed.stdout.strip()
    return output or None
