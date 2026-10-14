"""A complete Jenkins report; validation preserves all accepted input strings."""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class DeploymentStatus(StrEnum):
    DEPLOYING = "DEPLOYING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def validate_service_url(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("service URL must contain 1–2048 characters")
    if _has_control(value) or any(char.isspace() for char in value):
        raise ValueError(
            "service URL must not contain whitespace or control characters"
        )
    if any(char in value for char in '\\<>"'):
        raise ValueError("service URL contains unsafe characters")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "service URL must be absolute HTTP/HTTPS without credentials"
            )
        # Accessing port also validates malformed/non-numeric or out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(
            "service URL must be absolute HTTP/HTTPS without credentials"
        ) from exc


@dataclass(frozen=True)
class DeploymentReport:
    deployment_ref: str
    deployment_commit: str
    deployment_status: DeploymentStatus
    deployment_frontend_url: str
    deployment_admin_url: str

    def __post_init__(self) -> None:
        ref = self.deployment_ref
        if (
            not isinstance(ref, str)
            or not 1 <= len(ref) <= 512
            or not ref.strip()
            or _has_control(ref)
        ):
            raise ValueError(
                "deployment_ref must contain 1–512 characters and no controls"
            )
        if not isinstance(self.deployment_commit, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", self.deployment_commit
        ):
            raise ValueError(
                "deployment_commit must be a full 40 or 64 character Git hash"
            )
        if not isinstance(self.deployment_status, (str, DeploymentStatus)):
            raise ValueError("invalid deployment_status")  # noqa: TRY004 — validation errors share ValueError
        object.__setattr__(
            self, "deployment_status", DeploymentStatus(self.deployment_status)
        )
        validate_service_url(self.deployment_frontend_url)
        validate_service_url(self.deployment_admin_url)
