"""Qualified CLI launch/store bindings, distinct from T3's managed ACP home."""
from pathlib import Path

from . import antigravity_cli_cleanup as native, frozen_files as ff
from .record_identity import canonical_path

# Native format qualification runs against the actual bound store. Runtime
# fingerprints bind one operation to its selected executable across preflight
# and execution. Compatible upgrades receive a fresh fingerprint during planning.
FIELDS = {'native_layout', 'cli_binary', 'cli_user_home'}


def is_cli(binding):
    return binding.get('native_layout') == 'cli'


def check_binding(manifest, binding):
    if (not is_cli(binding) or not FIELDS <= binding.keys()
            or 'acp_runtime_binaries' in binding or 'codex_binary' in binding):
        native.fail('binding_layout_unqualified')
    home = native.root_path(binding['cli_user_home'])
    binary = Path(binding['cli_binary'])
    native.root_path(binary.parent)
    if (binary.name.lower() != 'agy.exe'
            or canonical_path(binding['root']) != canonical_path(home/'.gemini/antigravity-cli')
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
    command = provider.get('command', ['agy'])
    if not isinstance(command, list) or len(command) != 1 or not isinstance(command[0], str):
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
