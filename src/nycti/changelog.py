from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from importlib.resources import files
import subprocess

from nycti.config import Settings


@dataclass(frozen=True, slots=True)
class ChangelogAnnouncement:
    content: str
    fingerprint: str


def build_changelog_announcement(
    settings: Settings,
    *,
    changelog_reader=None,
    commit_subject_reader=None,
    commit_sha_reader=None,
) -> ChangelogAnnouncement | None:
    read_changelog = changelog_reader or _read_changelog_markdown
    read_subject = commit_subject_reader or _read_latest_commit_subject
    read_sha = commit_sha_reader or _read_latest_commit_sha

    changelog_body = read_changelog()
    if changelog_body:
        version = read_sha()
        fingerprint = version or _fingerprint_text(changelog_body)
        return ChangelogAnnouncement(
            content=changelog_body,
            fingerprint=fingerprint,
        )

    message = read_subject()
    if not message:
        return None

    version = read_sha()
    lines = [f"changelog: {message}"]
    if version:
        lines.append(f"version: `{version}`")
    fingerprint = version or message
    return ChangelogAnnouncement(
        content="\n".join(lines),
        fingerprint=fingerprint,
    )


def _read_changelog_markdown() -> str | None:
    try:
        content = files("nycti").joinpath("changelog.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return None
    cleaned = content.strip()
    return cleaned or None


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


def _fingerprint_text(text: str) -> str:
    return sha1(text.encode("utf-8")).hexdigest()[:12]
