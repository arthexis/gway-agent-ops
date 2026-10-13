"""Validated free-form Agent Ops request contract (no execution side effects)."""
from __future__ import annotations

import hashlib
import re

from .dispatch import timestamp

REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_PROMPT = 16_384


def validate_prompt_request(repository: str, prompt: str, at: str, *,
                            allowed_repositories: tuple[str, ...]) -> dict:
    """Fail closed before any durable marker or execution admission."""
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError("invalid_repository")
    if not allowed_repositories or repository not in allowed_repositories:
        raise ValueError("repository_not_allowed")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
        raise ValueError("invalid_prompt")
    if any(ord(char) == 0 for char in prompt):
        raise ValueError("invalid_prompt")
    if not isinstance(at, str):
        raise ValueError("invalid_timestamp")
    instant = timestamp(at)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return {"repository": repository, "prompt": prompt, "at": instant,
            "prompt_sha256": digest,
            "request_id": hashlib.sha256(
                (repository + "\n" + instant + "\n" + digest).encode("utf-8")
            ).hexdigest()}
