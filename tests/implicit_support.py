"""Synthetic empty implicit records. No production profile is read or written."""
import os
import uuid

from tests.native_recovery_support import field
from local_agent_manager.antigravity_cli_implicit import KEY_OFFSET

WORKSPACE = b'file:///D:/isolated/work'


def plaintext(owner, *, trajectory=None, state=None, uri=WORKSPACE,
              trajectory_extra=b'', metadata_extra=b'', outer_extra=b''):
    workspace = field(1, uri)+field(3, b'')
    timestamp = b'\x08\x80\xe2\xcf\xaa\x06'
    metadata = field(1, workspace)+field(2, timestamp)+field(3, (state or str(uuid.uuid4())).encode())+metadata_extra
    trajectory = (field(1, (trajectory or str(uuid.uuid4())).encode())+field(6, owner.encode())
                  + b'\x20\x01'+field(7, metadata)+trajectory_extra)
    return field(1, trajectory)+field(5, field(1, uri))+outer_extra


def encrypted(image, plain):
    # Optional test dependency, used only by the opt-in installed-image tests.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce+AESGCM(image[KEY_OFFSET:KEY_OFFSET+32]).encrypt(nonce, plain, None)


def write_implicit(root, binary, owner, **kwargs):
    (root/'implicit').mkdir(exist_ok=True)
    raw = encrypted(binary.read_bytes(), plaintext(owner, **kwargs))
    (root/'implicit'/(owner+'.pb')).write_bytes(raw)


def seed_implicit(root, binary, owner):
    """A full producer retention window so prune/rotation is legitimate."""
    image = binary.read_bytes()
    (root/'implicit').mkdir(exist_ok=True)
    for sid in [owner, *(str(uuid.uuid4()) for _ in range(99))]:
        (root/'implicit'/(sid+'.pb')).write_bytes(encrypted(image, plaintext(sid)))
