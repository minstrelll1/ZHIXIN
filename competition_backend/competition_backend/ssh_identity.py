"""Select the per-computer SSH key used by the competition services."""

from pathlib import Path


KEY_NAME = "zhixin_competition_ed25519"


def ssh_identity_args():
    """Use the competition key when present; retain legacy login until migrated."""
    key = Path.home() / ".ssh" / KEY_NAME
    return ["-i", str(key), "-o", "IdentitiesOnly=yes"] if key.is_file() else []
