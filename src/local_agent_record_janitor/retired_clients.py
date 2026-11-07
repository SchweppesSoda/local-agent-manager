"""Reject retired client identities before old plans can acquire writers."""
from collections.abc import Mapping


RETIRED_CLIENTS = frozenset({"aionui", "aion-ui", "qoderwork", "qoderwork-cn"})


def require_active_client(value: object) -> None:
    if str(value).strip().casefold().replace("_", "-") in RETIRED_CLIENTS:
        raise ValueError("client_support_removed: " + str(value))


def require_active_plan(value: object) -> None:
    """A native-scoped legacy plan can still contain retired frontend owners.

    Inspect only identity fields, including nested bindings and guard sources;
    never migrate those approvals to a different client or storage namespace.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"client", "owner_client", "platform", "engine", "backend"}:
                require_active_client(item)
            elif key in {"platforms", "engines"} and isinstance(item, (tuple, list)):
                for identity in item:
                    require_active_client(identity)
            require_active_plan(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            require_active_plan(item)
