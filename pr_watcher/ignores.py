"""Persistence for PRs the user has temporarily ignored."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def load_ignores(path: Path) -> tuple[dict[str, datetime], str | None]:
    """Return ignores keyed by row key, plus a warning when the file is unreadable.

    The state file is app-managed, so a corrupt file is reported and treated as
    empty rather than blocking startup.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"Could not read {path.name}: {exc}"
    try:
        raw = json.loads(text)["ignored"]
        if not isinstance(raw, dict):
            raise TypeError("'ignored' must be an object")
    except (ValueError, KeyError, TypeError) as exc:
        return {}, f"Ignoring unreadable {path.name} ({exc}); it will be overwritten"

    ignores: dict[str, datetime] = {}
    bad: list[str] = []
    for key, value in raw.items():
        try:
            ignores[str(key)] = _parse_utc(value)
        except (ValueError, TypeError):
            bad.append(str(key))
    if bad:
        return ignores, f"Skipped invalid entries in {path.name}: {', '.join(bad)}"
    return ignores, None


def save_ignores(path: Path, ignores: dict[str, datetime]) -> None:
    """Write ignores atomically so a crash never leaves a half-written file."""
    data = {
        "ignored": {
            key: ignores[key].astimezone(timezone.utc).isoformat(timespec="seconds")
            for key in sorted(ignores)
        }
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def prune_expired(ignores: dict[str, datetime], now: datetime) -> dict[str, datetime]:
    return {key: until for key, until in ignores.items() if until > now}


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("ignore expiry must be an ISO timestamp string")
    # Python 3.10's fromisoformat does not accept a trailing "Z".
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
