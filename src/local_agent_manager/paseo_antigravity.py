"""Paseo's built-in Antigravity plugin persistence contract.

ACP saves data.sessionId. Paseo can also reopen data.conversationId in an
existing record. Both identify the native session inside the plugin envelope.
Every independently stored resume handle must agree.
"""
import json

from .antigravity_cli_cleanup import native_id, fail
from .environment_config import _unique_object


def conversation(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'data'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['data'], dict)):
        fail('paseo_handle_shape_unqualified')
    data = value['data']
    if set(data) == {'sessionId'}:
        return native_id(data['sessionId'])
    if set(data) == {'conversationId'}:
        return native_id(data['conversationId'])
    fail('paseo_handle_shape_unqualified')


def decode(value):
    if not isinstance(value, str) or not value.startswith('plugin:') or len(value) > 4096:
        fail('paseo_handle_shape_unqualified')
    try:
        return conversation(json.loads(value[7:], object_pairs_hook=_unique_object))
    except (ValueError, TypeError, RecursionError):
        fail('paseo_handle_shape_unqualified')


def references(value):
    result = []
    persistence = value.get('persistence')
    if not isinstance(persistence, dict) or persistence.get('provider') != 'antigravity':
        fail('paseo_handle_shape_unqualified')
    if set(persistence) - {'provider', 'sessionId', 'metadata'}:
        fail('paseo_handle_shape_unqualified')
    sid = decode(persistence.get('sessionId'))
    result.append(('persistence/sessionId', sid))
    metadata = persistence.get('metadata')
    if metadata is not None:
        if (not isinstance(metadata, dict) or 'pluginProviderPersistence' not in metadata
                or set(metadata) - {'pluginProviderPersistence', 'cwd', 'mcpServers'}
                or 'mcpServers' in metadata and not isinstance(metadata['mcpServers'], dict)):
            fail('paseo_handle_shape_unqualified')
        if 'cwd' in metadata:
            # Core AgentStorage also saves the agent's working directory beside
            # the plugin envelope. It is neither a native home nor a handle.
            from .paseo_store import local_root
            from .record_identity import canonical_path
            try:
                same = canonical_path(local_root(metadata['cwd'])) == canonical_path(local_root(value.get('cwd')))
            except (TypeError, ValueError, OSError):
                same = False
            if not same:
                fail('paseo_handle_shape_unqualified')
        other = conversation(metadata['pluginProviderPersistence'])
        if other != sid:
            fail('paseo_handle_conflict')
        result.append(('persistence/metadata/pluginProviderPersistence', other))
    runtime = value.get('runtimeInfo')
    if runtime is not None:
        if (not isinstance(runtime, dict) or runtime.get('provider') != 'antigravity'
                or set(runtime) - {'provider', 'sessionId', 'model', 'thinkingOptionId', 'modeId'}):
            fail('paseo_handle_shape_unqualified')
        if runtime.get('sessionId') is not None:
            other = decode(runtime['sessionId'])
            if other != sid:
                fail('paseo_handle_conflict')
            result.append(('runtimeInfo/sessionId', other))
    return [{'provider': 'antigravity', 'kind': 'id', 'locator': location, 'value': sid}
            for location, sid in result]
