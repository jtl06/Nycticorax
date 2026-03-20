from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha1
from importlib.resources import files
import subprocess

from nycti.config import Settings


@dataclass(frozen=True, slots=True)
class ChangelogAnnouncement:
    content: str
    fingerprint: str
    snapshot: str


def build_changelog_announcement(
    settings: Settings,
    *,
    previous_snapshot: str | None = None,
    changelog_reader=None,
    commit_subject_reader=None,
    commit_sha_reader=None,
) -> ChangelogAnnouncement | None:
    read_changelog = changelog_reader or _read_changelog_markdown
    read_subject = commit_subject_reader or _read_latest_commit_subject
    read_sha = commit_sha_reader or _read_latest_commit_sha

    changelog_body = read_changelog()
    if changelog_body:
        delta = _extract_snapshot_delta(previous_snapshot, changelog_body)
        if delta is None:
            return None
        version = read_sha()
        fingerprint = version or _fingerprint_text(changelog_body)
        return ChangelogAnnouncement(
            content=delta,
            fingerprint=fingerprint,
            snapshot=changelog_body,
        )

    message = read_subject()
    if not message:
        return None

    version = read_sha()
    lines = [f"changelog: {message}"]
    if version:
        lines.append(f"version: `{version}`")
    snapshot = "\n".join(lines)
    if previous_snapshot == snapshot:
        return None
    fingerprint = version or message
    return ChangelogAnnouncement(
        content=snapshot,
        fingerprint=fingerprint,
        snapshot=snapshot,
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


def _extract_snapshot_delta(previous_snapshot: str | None, current_snapshot: str) -> str | None:
    current_clean = current_snapshot.strip()
    if not current_clean:
        return None

    previous_clean = (previous_snapshot or "").strip()
    if not previous_clean:
        return current_clean
    if previous_clean == current_clean:
        return None

    previous_lines = previous_clean.splitlines()
    current_lines = current_clean.splitlines()
    matcher = SequenceMatcher(a=previous_lines, b=current_lines)
    added_chunks: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"insert", "replace"}:
            continue
        chunk = "\n".join(current_lines[j1:j2]).strip()
        if chunk:
            added_chunks.append(chunk)

    if added_chunks:
        return "\n".join(added_chunks).strip()
    return current_clean
