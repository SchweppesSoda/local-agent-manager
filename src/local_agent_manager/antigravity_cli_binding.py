"""Qualified CLI launch/store bindings, distinct from T3's managed ACP home."""
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import antigravity_cli_cleanup as native, frozen_files as ff
from .record_identity import canonical_path

# Native format qualification runs against the actual bound store. Runtime
# fingerprints bind one operation to its selected executable across preflight
# and execution. Compatible upgrades receive a fresh fingerprint during planning.
FIELDS = {'native_layout', 'cli_binary', 'cli_user_home'}
_instance_lease = ContextVar('antigravity_instance_settings', default=None)


def is_cli(binding):
    return binding.get('native_layout') in ('cli', 't3-instance-cli')


def instance_root(profile, instance_id):
    key = hashlib.sha256(instance_id.encode()).hexdigest()
    return Path(profile) / 'userdata/providers/antigravity' / key / 'antigravity-acp'


def is_instance_path(root):
    path = Path(root)
    return (len(path.parts) >= 6 and Path(path.name) == Path('antigravity-acp')
            and Path(*path.parts[-5:-2]) == Path('userdata/providers/antigravity'))


def present_ids(store, ids):
    found = set()
    for sid in ids:
        try:
            path = ff.checked_path(store, 'conversations/'+native.native_id(sid)+'.db')
            ff._plain(path.lstat())
        except FileNotFoundError:
            continue
        found.add(sid)
    return found


def instance_layout(profile, instance_id, home, ids):
    """A CLI override may have saved different threads in different stores."""
    local = instance_root(profile, instance_id)
    shared = Path(home)/'.gemini/antigravity-cli'
    private_ids = present_ids(local, ids)
    if private_ids:
        if private_ids != set(ids) or present_ids(shared, ids):
            native.fail('ambiguous_native_store')
        return 't3-instance-cli', native.root_path(local)
    return 'cli', native.root_path(shared)


@contextmanager
def hold_instance_selection(evidence):
    """Admit the read-only T3 source without granting a T3 write ticket."""
    instance = evidence.get('t3_instance')
    if instance is None:
        yield
        return
    from .windows_held_files import HeldFiles
    from .mutation_guard import MutationScope, _check_siblings, mutation_roots
    root = Path(instance['root'])
    with mutation_roots((root, Path(evidence['root']))), HeldFiles() as held:
        _check_siblings(MutationScope(root, None), None)
        held.acquire(root, instance['settings'])
        token = _instance_lease.set((root, held))
        try:
            yield
        finally:
            _instance_lease.reset(token)


def instance_selection(root, binary=None):
    """Qualify the instance directory against its actual T3 provider configuration."""
    root = native.root_path(root)
    if not is_instance_path(root):
        native.fail('instance_root_unproven')
    profile = root.parents[4]
    from .antigravity_cli_joint import held_settings
    relative = 'userdata/settings.json'
    current = held_settings(profile, relative)
    lease = _instance_lease.get()
    if lease is not None and lease[0] == profile:
        current = lease[1].read(profile, relative, content=True)
    proof, raw = current if current is not None else ff.read_file(profile, relative, content=True, limit=1024*1024)
    instances = native._json(raw).get('providerInstances')
    if not isinstance(instances, dict):
        native.fail('instance_root_unproven')
    matches = [v for k, v in instances.items() if instance_root(profile, k) == root]
    if len(matches) != 1 or not isinstance(matches[0], dict) or matches[0].get('driver') != 'antigravity':
        native.fail('instance_root_unproven')
    config = matches[0].get('config')
    if (not isinstance(config, dict) or not isinstance(config.get('binaryPath'), str)
            or any(config.get(k) for k in ('homePath','launchArgs'))):
        native.fail('instance_runtime_unproven')
    selected = Path(config['binaryPath'])
    if (not selected.is_absolute() or selected.name.lower() != 'agy.exe'
            or binary is not None and canonical_path(selected) != canonical_path(binary)):
        native.fail('instance_runtime_unproven')
    return {'root': str(profile), 'settings': proof}


def check_binding(manifest, binding):
    if (not is_cli(binding) or not FIELDS <= binding.keys()
            or 'acp_runtime_binaries' in binding or 'codex_binary' in binding):
        native.fail('binding_layout_unqualified')
    home = native.root_path(binding['cli_user_home'])
    binary = Path(binding['cli_binary'])
    native.root_path(binary.parent)
    expected = home/'.gemini/antigravity-cli'
    if binding['native_layout'] == 't3-instance-cli':
        if 'environment_id' not in manifest or not isinstance(binding.get('provider_instance_id'), str):
            native.fail('instance_root_unproven')
        expected = instance_root(manifest['profile_root'], binding['provider_instance_id'])
    if (binary.name.lower() != 'agy.exe'
            or canonical_path(binding['root']) != canonical_path(expected)
            or canonical_path(binary) not in set(map(canonical_path, manifest['runtime_binaries']))):
        native.fail('binding_root_or_runtime_unproven')


def runtime(binary, manifest=None, *, profile_root=None):
    binary = Path(binary)
    if profile_root is not None:
        from .t3code_lifecycle import read_binary
        proof = {'root': str(binary.parent), 'file': read_binary(profile_root, binary)}
    elif manifest:
        from . import t3code_lifecycle, paseo_lifecycle
        boundary = (t3code_lifecycle.current_boundary(manifest['profile_root'])
                    or paseo_lifecycle.current_boundary(manifest['profile_root']))
        if boundary:
            matches = [p for p in boundary['lifecycle']['binaries']
                       if canonical_path(Path(p['root'])/p['file']['path']) == canonical_path(binary)]
            if len(matches) != 1:
                native.fail('runtime_not_held')
            proof = matches[0]
            if t3code_lifecycle.current_boundary(manifest['profile_root']):
                proof = {'root': proof['root'], 'file': t3code_lifecycle.read_binary(manifest['profile_root'], binary)}
        else:
            proof = {'root': str(binary.parent), 'file': ff.read_file(binary.parent, binary.name)[0]}
    else:
        proof = {'root': str(binary.parent), 'file': ff.read_file(binary.parent, binary.name)[0]}
    if binary.name.lower() != 'agy.exe' or not proof['file']['size']:
        native.fail('runtime_file_unqualified')
    return proof


def environment(*layers):
    result = {}
    for layer in layers:
        if isinstance(layer, list):
            pairs = [(p.get('name'), p.get('value')) for p in layer if isinstance(p, dict)]
            if len(pairs) != len(layer):
                native.fail('launch_environment_unqualified')
        elif isinstance(layer, dict):
            pairs = list(layer.items())
        else:
            native.fail('launch_environment_unqualified')
        names = set()
        for name, value in pairs:
            if not isinstance(name, str) or not isinstance(value, str) or name.upper() in names:
                native.fail('launch_environment_unqualified')
            names.add(name.upper())
            result[name.upper()] = value
    return result


def home_for(env):
    # Never use GEMINI_HOME as the native CLI store. Conflicting OS home
    # overrides require separate runtime qualification, not a guessed alias.
    home = native.root_path(env.get('USERPROFILE', Path.home()))
    if env.get('HOME') and canonical_path(env['HOME']) != canonical_path(home):
        native.fail('launch_home_conflict')
    return home


def paseo_launch(provider, overlay=None, manifest=None):
    if not isinstance(provider, dict) or provider.get('extends') is not None:
        native.fail('provider_implementation_unqualified')
    env = environment(provider.get('env', {}), overlay or {})
    command = provider.get('command', ['agy', 'acp', '--disable-telemetry'])
    if (not isinstance(command, list) or not command or not isinstance(command[0], str)
            or command[1:] not in ([], ['acp'], ['acp', '--disable-telemetry'])):
        native.fail('provider_command_unqualified')
    binary = command[0]
    if not Path(binary).is_absolute():
        # This process's PATH is not evidence of the daemon's inherited PATH.
        # No launch-observation format is currently qualified for bare names.
        native.fail('runtime_resolution_unproven')
    runtime(binary, manifest)
    return Path(binary), home_for(env)


def selection(manifest, binding, client, *, overlay=None):
    check_binding(manifest, binding)
    root = Path(manifest['profile_root'])
    relative = 'userdata/settings.json' if client == 't3code' else 'config.json'
    from .antigravity_cli_joint import held_settings
    current = held_settings(root, relative)
    proof, raw = current if current is not None else ff.read_file(root, relative, content=True, limit=1024*1024)
    data = native._json(raw)
    if client == 't3code':
        instance = data.get('providerInstances', {}).get(binding.get('provider_instance_id'))
        if not isinstance(instance, dict) or instance.get('driver') != 'antigravity':
            native.fail('provider_selection_unproven')
        config = instance.get('config', {})
        if not isinstance(config, dict) or any(config.get(k) for k in ('homePath', 'launchArgs')):
            native.fail('provider_command_unqualified')
        binary = config.get('binaryPath')
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            native.fail('runtime_resolution_unproven')
        home = home_for(environment(instance.get('environment', {})))
    elif client == 'paseo':
        if type(data.get('version')) is not int or data['version'] != 1:
            native.fail('provider_config_unqualified')
        provider = data.get('agents', {}).get('providers', {}).get('antigravity', {})
        if overlay is None:
            from .paseo_cleanup_files import rows
            entries, _ = rows(root)
            values = [e['value'].get('launchEnv', {}) for e in entries
                      if e['kind'] == 'agent' and e['row']['id'] == binding['agent_id']]
            if not values or any(v != values[0] for v in values):
                native.fail('launch_environment_unqualified')
            overlay = values[0]
        binary, home = paseo_launch(provider, overlay, manifest)
    else:
        native.fail('frontend_unqualified')
    if (canonical_path(binary) != canonical_path(binding['cli_binary'])
            or canonical_path(home) != canonical_path(binding['cli_user_home'])):
        native.fail('provider_selection_changed')
    return {'settings': proof, 'runtime': runtime(binary, manifest)}
