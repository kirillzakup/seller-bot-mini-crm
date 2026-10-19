"""Mirror admin-entered secrets back into the project ``.env`` file.

The DB ``settings`` table is the live runtime source (so admin edits apply
instantly, no restart). This module additionally writes the env-shaped keys
into ``.env`` so the file reflects what was configured in the panel — the admin
fills ``.env``, not the operator by hand.

In the Docker deploy ``runtime/`` is a directory bind mount from the host, so
writes persist across restarts/rebuilds and are visible at
``/opt/seller-bot/runtime/.env``. (A single-file bind mount is intentionally
avoided — container writes to it don't reliably propagate to the host.)
"""

from __future__ import annotations

import logging

from app.config import BASE_DIR

logger = logging.getLogger(__name__)

# Bootstrap secrets stay in BASE_DIR/.env; admin-managed values are written here,
# under the runtime/ directory mount (config also loads it — see config.py).
ENV_PATH = BASE_DIR / "runtime" / ".env"


def write_env_values(updates: dict[str, str]) -> bool:
    """Update/insert ``KEY=VALUE`` lines in ``.env``, preserving everything else.

    Existing keys are updated in place; unknown keys are appended. Comments and
    unrelated lines are kept. Returns False (logged, not raised) if the file
    cannot be written — a missing/read-only ``.env`` must never break a save.
    """
    if not updates:
        return True
    try:
        existing = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
        remaining = dict(updates)
        out: list[str] = []
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    out.append(f"{key}={remaining.pop(key)}")
                    continue
            out.append(line)
        for key, value in remaining.items():
            out.append(f"{key}={value}")
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except OSError as e:
        logger.warning("Could not write %s: %s", ENV_PATH, e)
        return False
