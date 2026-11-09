"""Strict JSON and pure projections shared by inventory and Skill binding."""
from __future__ import annotations

import json
from typing import Any


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid_json_constant")


def strict_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def provider_instance(settings: dict, instance_id: str, driver: str) -> dict:
    instances = settings.get("providerInstances")
    instance = instances.get(instance_id) if isinstance(instances, dict) else None
    if not isinstance(instance, dict) or instance.get("driver") != driver:
        raise ValueError("provider_instance_or_driver_unknown")
    config = instance.get("config", {})
    environment = instance.get("environment", {})
    if not isinstance(config, dict) or not isinstance(environment, dict):
        raise ValueError("provider_setting_shape_unknown")
    for value in (instance.get("enabled"), config.get("enabled")):
        if value is not None and type(value) is not bool:
            raise ValueError("provider_enabled_shape_unknown")
    for key in ("binaryPath", "homePath"):
        if config.get(key) is not None and not isinstance(config[key], str):
            raise ValueError("provider_setting_shape_unknown")
    return {
        "config": config,
        "environment": environment,
        "enabled": instance.get("enabled", True) and config.get("enabled", True),
    }
