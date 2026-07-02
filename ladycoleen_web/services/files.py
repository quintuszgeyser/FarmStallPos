import os
import uuid
import logging
from flask import current_app

log = logging.getLogger(__name__)


def save_upload(file_storage, subfolder: str, allowed_extensions: set) -> tuple[str | None, str | None]:
    """
    Save an uploaded file to uploads/<subfolder>/<uuid>.<ext>.
    Returns (relative_path, None) on success or (None, error_message) on failure.
    """
    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in allowed_extensions:
        return None, f"File type .{ext} not allowed. Allowed: {', '.join(sorted(allowed_extensions))}"

    # Check size before reading fully
    file_storage.seek(0, 2)
    size = file_storage.tell()
    file_storage.seek(0)
    max_bytes = current_app.config.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
    if size > max_bytes:
        return None, f"File too large. Max {max_bytes // (1024*1024)}MB"

    upload_root = current_app.config["UPLOAD_PATH"]
    target_dir  = os.path.join(upload_root, subfolder)
    os.makedirs(target_dir, exist_ok=True)

    safe_name = f"{uuid.uuid4().hex}.{ext}"
    full_path = os.path.join(target_dir, safe_name)

    try:
        file_storage.save(full_path)
    except Exception as e:
        log.error("Failed to save upload to %s: %s", full_path, e)
        return None, "Upload failed - please try again"

    relative = f"{subfolder}/{safe_name}"
    log.info(json_line("file_saved", {"path": relative, "size": size}))
    return relative, None


def json_line(action, extra=None):
    import json
    d = {"action": action}
    if extra:
        d.update(extra)
    return json.dumps(d)
