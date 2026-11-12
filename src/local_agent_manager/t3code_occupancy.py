"""Exact pending-store resources for lifecycle evidence, excluding binaries."""
from pathlib import Path


def resources(evidence):
    groups = {}

    def add(root, paths):
        root = Path(root)
        names = groups.setdefault(str(root), set())
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            if not path.is_relative_to(root):
                raise ValueError('handoff_occupancy_resource_outside_store')
            if path.is_file():
                names.add(str(path))

    def proofs(root, value):
        if isinstance(value, list):
            for item in value:
                proofs(root, item)
        elif isinstance(value, dict):
            if 'sha256' in value and isinstance(value.get('path'), str):
                add(root, [value['path']])
                # LevelDB's lock protects the shared family, but is deliberately
                # not part of its content hash.
                if Path(value['path']).name == 'CURRENT':
                    add(root, [str(Path(value['path']).parent / 'LOCK')])
            else:
                for key, item in value.items():
                    if key not in {'runtime', 'preserved', 'lifecycle', 'binding_manifest'}:
                        proofs(root, item)

    root = Path(evidence['root'])
    add(root, ['userdata/' + name + suffix for name in ('statev2.sqlite', 'server-owner.sqlite')
               for suffix in ('', '-wal', '-shm', '-journal')])
    proofs(root, evidence['files']['changes'])
    for target in evidence['native_targets']:
        if not target['action_ids'] and target['engine'] != 'antigravity':
            continue
        native_root = Path(target['root'])
        add(native_root, target['paths'])
        if target['engine'] == 'codex':
            add(native_root, [str(p) for p in native_root.glob('state_*.sqlite*')])
        if target.get('native_files'):
            proofs(native_root, target['native_files'])
        for action in evidence['native_actions']:
            if action['action_id'] in target['action_ids']:
                impact = action.get('impact', {})
                add(native_root, impact.get('rollout_paths', []))
                path = impact.get('resource_path')
                if path and Path(path).is_relative_to(native_root):
                    add(native_root, [path])
    for desktop in evidence['desktop']:
        storage = desktop['localstorage']
        proofs(Path(storage['root']), storage.get('files'))
        indexed = storage.get('indexeddb')
        if indexed:
            proofs(Path(storage['root']), indexed.get('evidence', {}))
    return {root: sorted(paths) for root, paths in groups.items() if paths}
