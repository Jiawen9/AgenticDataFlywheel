"""Internal website integration boundary; default implementation sends nothing.

Implement is_configured() and upload_excel(). See INTERNAL_UPLOAD.md.
"""
from dataclasses import dataclass
from pathlib import Path


class UploadError(RuntimeError):
    """Actionable, credential-free message safe to display to the user."""


@dataclass(frozen=True)
class UploadContext:
    file_path: Path
    filename: str
    dataset_name: str
    release_id: str
    file_index: int
    sha256: str
    idempotency_key: str


@dataclass(frozen=True)
class UploadResult:
    success: bool
    remote_id: str | None = None
    url: str | None = None


def is_configured() -> bool:
    """Check server-side configuration without submitting any files."""
    return False


def upload_excel(context: UploadContext) -> UploadResult:
    """Stream the existing Excel; return only after confirmed remote acceptance.

    Set network timeouts and reuse idempotency_key. Do not put credentials or
    raw HTTP responses in UploadError messages.
    """
    raise UploadError("云道S3上传尚未配置")
