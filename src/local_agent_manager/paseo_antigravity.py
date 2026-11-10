"""Paseo's built-in Antigravity plugin persistence contract.

Qualified against getpaseo/paseo d91ae138cb931447635e94296023414a58bedcc5
(0.15.3) and ef7de3d066256901396e1f47d9e309d569589b2c:
plugins/antigravity-provider/server/internal/session.ts and
packages/server/src/server/agent/plugin-provider.ts. The outer sessionId is
`plugin:` plus JSON, not the conversation UUID. Metadata is a second resume
source and must agree. Custom ACP/nativeHandle variants are not this format.
"""
import json

from .antigravity_cli_cleanup import native_id, fail
from .environment_config import _unique_object


def conversation(value):
    if (not isinstance(value, dict) or set(value) != {'version', 'data'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['data'], dict) or set(value['data']) != {'conversationId'}):
        fail('paseo_handle_shape_unqualified')
    return native_id(value['data']['conversationId'])


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
        if not isinstance(metadata, dict) or set(metadata) != {'pluginProviderPersistence'}:
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
