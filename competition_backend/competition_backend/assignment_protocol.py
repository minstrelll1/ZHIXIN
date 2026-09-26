from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


def assignment_checksum(message: Dict[str, Any]) -> str:
    body = {key: value for key, value in message.items() if key != "assignment_checksum"}
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
