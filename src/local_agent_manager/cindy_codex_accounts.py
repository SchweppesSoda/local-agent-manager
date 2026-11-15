"""Resolve exact Cindy native-account bindings without reading credentials."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import frozen_files
from .providers.t3 import _unique_object, _reject_constant


MAX_ACCOUNT_CONFIG_BYTES = 256 * 1024


def codex_account_bindings(database: Path, profile_root: Path) -> dict[str, Path]:
    """Use native-thread bindings from this database owner's registry.

    Task bindings describe the current account and cannot locate historical
    native threads. Older databases without this registry keep their existing
    explicitly supplied or dedicated-home mapping.
    """
    name = database.stem
    if not name.startswith("cindy-") or name == "cindy-local-v1":
        return {}
    owner = name.removeprefix("cindy-")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner):
        raise ValueError("cindy_account_owner_unqualified")
    digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    relative = f"owners/{digest[:20]}/subscription-accounts.json"
    try:
        _, raw = frozen_files.read_file(profile_root, relative, content=True,
                                        limit=MAX_ACCOUNT_CONFIG_BYTES)
    except FileNotFoundError:
        return {}
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)
    bindings = data.get("bindings") if isinstance(data, dict) else None
    if not isinstance(bindings, dict):
        raise ValueError("cindy_account_bindings_schema_unknown")
    pools = data.get("pools", {})
    if not isinstance(pools, dict) or not isinstance(pools.get("openai", {}), dict):
        raise ValueError("cindy_account_pool_schema_unknown")
    pool = pools.get("openai", {})
    legacy_default = pool.get("legacyCodexDefault", False)
    if not isinstance(legacy_default, bool):
        raise ValueError("cindy_legacy_default_binding_unqualified")
    result = {}
    prefix = "openai:native:"
    account_pattern = re.compile(r"cindy-" + digest[:12] + r"-[A-Za-z0-9_-]{1,100}")
    for key, binding in bindings.items():
        if not key.startswith(prefix):
            continue
        thread_id = key.removeprefix(prefix)
        if not thread_id or not isinstance(binding, dict) or binding.get("providerId") != "openai":
            raise ValueError("cindy_native_account_binding_unqualified")
        account_id = binding.get("accountId")
        if account_id == "default":
            home = profile_root / "codex-home" if legacy_default else Path.home() / ".codex"
        elif isinstance(account_id, str) and account_pattern.fullmatch(account_id):
            home = Path.home() / f".codex-{account_id}"
        else:
            raise ValueError("cindy_native_account_directory_unqualified")
        result[thread_id] = home
    return result
