"""Exact producer's authenticated, empty mainline trajectories, recovery only.

The producer saves a real Trajectory, not telemetry. Only the field subset below
has a proven empty restoration closure. Parent/base/steps and every other field
are refused, even if protobuf itself could preserve or ignore them.
"""
import ctypes
import hashlib
import re
from urllib.parse import unquote, urlsplit

from .antigravity_cli_cleanup import fail, native_id

RUNTIME_SHA256 = '47b1bec8c4a6d4afa1587177530e329cee4f5c6e76573a12ac9ab47b846e50e6'
RUNTIME_SIZE = 225159320
# File offsets in that exact PE, established from its Go producer and descriptors.
KEY_OFFSET = 68012862
SCHEMAS = (
    (72631807, 17660, '6fcef61542ce973dda431c9c276a854d5cced14f75734f0eeaf08928da38cef0'),
    (72096164, 13713, '0a6ac2c7c925c34c6e255809057baacab02064422187be825f070cdaf36ab5a4'),
    (76634702, 213655, '389e60a7aaec04c2b2ca127c0e27f16ec283b12128c8fc853ce67eec9005e24e'),
    (69179893, 258, '677ffd2bf8f2b78cd407039ddf76d8033df80f0a885e23a793c99298b926895f'),
    (76247702, 186891, '0f54e917acf3630ffae0d874a4a847c5f698bea2570b67477966b6aaa67947aa'),
)


def _decrypt(key, raw):
    """Windows CNG GCM authentication. No optional package or plaintext file."""
    from ctypes import wintypes as w
    pointer = ctypes.c_void_p
    class Auth(ctypes.Structure):
        _fields_ = [('size', w.ULONG), ('version', w.ULONG),
            ('nonce', pointer), ('nonce_size', w.ULONG),
            ('aad', pointer), ('aad_size', w.ULONG),
            ('tag', pointer), ('tag_size', w.ULONG),
            ('mac', pointer), ('mac_size', w.ULONG), ('aad_done', w.ULONG),
            ('data_done', ctypes.c_ulonglong), ('flags', w.ULONG)]
    if len(raw) < 28 or len(raw) > 16384:
        fail('implicit_envelope_unqualified')
    lib = ctypes.WinDLL('bcrypt')
    signatures = {
        'BCryptOpenAlgorithmProvider': [ctypes.POINTER(pointer), w.LPCWSTR, w.LPCWSTR, w.ULONG],
        'BCryptSetProperty': [pointer, w.LPCWSTR, pointer, w.ULONG, w.ULONG],
        'BCryptGetProperty': [pointer, w.LPCWSTR, pointer, w.ULONG, ctypes.POINTER(w.ULONG), w.ULONG],
        'BCryptGenerateSymmetricKey': [pointer, ctypes.POINTER(pointer), pointer, w.ULONG, pointer, w.ULONG, w.ULONG],
        'BCryptDecrypt': [pointer, pointer, w.ULONG, pointer, pointer, w.ULONG, pointer, w.ULONG, ctypes.POINTER(w.ULONG), w.ULONG],
        'BCryptDestroyKey': [pointer], 'BCryptCloseAlgorithmProvider': [pointer, w.ULONG],
    }
    for name, args in signatures.items():
        getattr(lib, name).argtypes = args
        getattr(lib, name).restype = w.LONG
    def checked(status):
        if status < 0:
            fail('implicit_authentication_failed')
    algorithm, handle = pointer(), pointer()
    secret = ctypes.create_string_buffer(key)
    output = ctypes.create_string_buffer(len(raw))
    try:
        checked(lib.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), 'AES', None, 0))
        mode = ctypes.create_unicode_buffer('ChainingModeGCM')
        checked(lib.BCryptSetProperty(algorithm, 'ChainingMode', mode, ctypes.sizeof(mode), 0))
        length, copied = w.ULONG(), w.ULONG()
        checked(lib.BCryptGetProperty(algorithm, 'ObjectLength', ctypes.byref(length), 4, ctypes.byref(copied), 0))
        if length.value > 65536:
            fail('implicit_crypto_budget_exceeded')
        storage = ctypes.create_string_buffer(length.value)
        checked(lib.BCryptGenerateSymmetricKey(algorithm, ctypes.byref(handle), storage,
            length, secret, len(key), 0))
        nonce, tag = ctypes.create_string_buffer(raw[:12]), ctypes.create_string_buffer(raw[-16:])
        ciphertext = ctypes.create_string_buffer(raw[12:-16])
        auth = Auth()
        auth.size, auth.version = ctypes.sizeof(Auth), 1
        auth.nonce, auth.nonce_size = ctypes.addressof(nonce), 12
        auth.tag, auth.tag_size = ctypes.addressof(tag), 16
        checked(lib.BCryptDecrypt(handle, ciphertext, len(raw)-28, ctypes.byref(auth),
            None, 0, output, len(output), ctypes.byref(copied), 0))
        return output.raw[:copied.value]
    finally:
        ctypes.memset(secret, 0, ctypes.sizeof(secret))
        ctypes.memset(output, 0, ctypes.sizeof(output))
        if handle:
            lib.BCryptDestroyKey(handle)
        if algorithm:
            lib.BCryptCloseAlgorithmProvider(algorithm, 0)


def _varint(raw, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(raw):
            fail('implicit_proto_truncated')
        byte = raw[pos]
        pos += 1
        if shift == 63 and byte > 1:
            fail('implicit_proto_varint_invalid')
        value |= (byte & 127) << shift
        if byte < 128:
            if shift and byte == 0:
                fail('implicit_proto_noncanonical')
            return value, pos
    fail('implicit_proto_varint_invalid')


def _message(raw, contract):
    pos, result = 0, {}
    while pos < len(raw):
        tag, pos = _varint(raw, pos)
        number, wire = tag >> 3, tag & 7
        if number not in contract or contract[number] != wire or number in result:
            fail('implicit_fields_unqualified')
        if wire == 0:
            value, pos = _varint(raw, pos)
        else:
            size, pos = _varint(raw, pos)
            if size > len(raw)-pos:
                fail('implicit_proto_truncated')
            value = raw[pos:pos+size]
            pos += size
        result[number] = value
    return result


def _id(raw, selected):
    try:
        value = native_id(raw.decode('ascii'))
    except (UnicodeError, AttributeError):
        fail('implicit_id_unqualified')
    if value in selected:
        fail('implicit_target_reference')
    return value


def _workspace(raw):
    try:
        value = raw.decode('utf-8')
        uri = urlsplit(value)
        path = unquote(uri.path, errors='strict')
    except (ValueError, UnicodeError):
        fail('implicit_workspace_unqualified')
    if (len(raw) > 4096 or uri.scheme != 'file' or uri.netloc or uri.query or uri.fragment
            or not re.fullmatch(r'/[A-Za-z]:/[^\\\x00-\x1f]*', path)
            or any(c in path[3:] for c in '%:<>"|?*')
            or any(not p or p.rstrip(' .') != p or p in {'.', '..'}
                   or re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p)
                   for p in path[4:].split('/'))):
        fail('implicit_external_workspace')
    return hashlib.sha256(raw).hexdigest()


def empty_trajectory(raw, filename_id, selected):
    outer = _message(raw, {1: 2, 5: 2})
    if set(outer) != {1, 5}:
        fail('implicit_fields_unqualified')
    trajectory = _message(outer[1], {1: 2, 6: 2, 4: 0, 7: 2})
    if set(trajectory) != {1, 6, 4, 7} or trajectory[4] != 1:
        fail('implicit_trajectory_role_unqualified')
    trajectory_id = _id(trajectory[1], selected)
    cascade_id = _id(trajectory[6], selected)
    if cascade_id != filename_id or trajectory_id == cascade_id:
        fail('implicit_owner_unqualified')
    metadata = _message(trajectory[7], {1: 2, 2: 2, 3: 2})
    if set(metadata) != {1, 2, 3}:
        fail('implicit_metadata_unqualified')
    state_id = _id(metadata[3], selected)
    timestamp = _message(metadata[2], {1: 0, 2: 0})
    if (not 0 < timestamp.get(1, 0) <= 253402300799
            or timestamp.get(2, 0) > 999999999):
        fail('implicit_timestamp_unqualified')
    workspace = _message(metadata[1], {1: 2, 3: 2})
    scope = _message(outer[5], {1: 2})
    if (set(workspace) != {1, 3} or workspace[3] != b'' or set(scope) != {1}
            or scope[1] != workspace[1]):
        fail('implicit_workspace_scope_unqualified')
    return {'trajectory_id': trajectory_id, 'cascade_id': cascade_id,
        'initialization_state_id': state_id, 'workspace_uri_sha256': _workspace(scope[1]),
        'role': 'empty_user_mainline', 'steps': 0, 'restoration_edges': []}


def qualify(runtime, files, reader, selected):
    proof, image = runtime()
    if (proof['sha256'] != RUNTIME_SHA256 or proof['size'] != RUNTIME_SIZE
            or len(image) != RUNTIME_SIZE or hashlib.sha256(image).hexdigest() != RUNTIME_SHA256):
        fail('implicit_producer_unqualified')
    for offset, size, digest in SCHEMAS:
        if hashlib.sha256(image[offset:offset+size]).hexdigest() != digest:
            fail('implicit_schema_unqualified')
    if not 1 <= len(files) <= 100:
        fail('implicit_namespace_unqualified')
    key = image[KEY_OFFSET:KEY_OFFSET+32]
    records = []
    for file in files:
        match = re.fullmatch(r'implicit/([0-9a-f-]{36})\.pb', file['path'])
        if not match:
            fail('implicit_path_unqualified')
        owner = native_id(match[1])
        current, raw = reader(file['path'])
        if current != file:
            fail('implicit_file_changed')
        plain = _decrypt(key, raw)
        record = empty_trajectory(plain, owner, selected)
        del plain
        records.append({'file': file, **record})
    all_ids = [r['trajectory_id'] for r in records] + [r['cascade_id'] for r in records]
    if len(all_ids) != len(set(all_ids)):
        fail('implicit_duplicate_identity')
    return {'schema_version': 'lam.agy-empty-implicit.v1', 'runtime': proof,
        'descriptor_sha256': [d for _, _, d in SCHEMAS],
        'envelope': 'AES-256-GCM nonce12 ciphertext tag16, no AAD, no compression',
        'records': records, 'target_absent': True, 'restoration_closure_empty': True,
        'original_missing_bytes_claimed': False}
