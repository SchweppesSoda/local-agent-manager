# Authenticated empty implicit recovery

The T3 completed-child joint recovery ticket can qualify one exact Antigravity
producer's empty mainline implicit records. Ordinary planning, apply and native
remaining verification retain frozen preservation. Paseo and other producers
retain that preservation requirement too.

## Producer evidence

The qualified Windows PE has SHA256
`47b1bec8c4a6d4afa1587177530e329cee4f5c6e76573a12ac9ab47b846e50e6`
and size 225159320. Its Go function table and embedded descriptors identify the
following implementation. Addresses are preferred virtual addresses in this PE.

| Function | Address | Established behavior |
| --- | --- | --- |
| `language_server.setUpImplicitTrajectorySaver` | `0x14317d1a0` | Creates the implicit DiskSaver with a 32-byte encryption option and `WithMaxSaveCount(100)` |
| `DiskSaver.Save` | `0x142766260` | Marshals protobuf into GCM storage, generates a nonce, writes a temporary file, renames it, then prunes |
| `DiskSaver.loadUnlocked` | `0x1427679a0` | Reads the file, creates AES/GCM, splits nonce from ciphertext, authenticates with empty AAD, then unmarshals |
| `DiskSaver.LoadAll` | `0x142769100` | Enumerates saved paths and loads each by its filename ID |
| `DiskSaver.pruneUnlocked` | `0x142767500` | Sorts by file modification time, removes the oldest excess entries above its configured count |
| `UserImplicitTrajectoryManager.saveTrajectory` | `0x142d02360` | Saves type 1 trajectories under their wrapper's CascadeID, with trajectory scope |
| `asyncLoadTrajectoryKeyToIdMapFromDisk.func1` | `0x142d098c0` | Loads saved implicit records, selects type 1, reconstructs keys from scope and maps keys to saved IDs |
| `createTrajectoryKeyFromScope` | `0x142d08a40` | Normalizes workspace/git-root URI and combines them with branch |
| `UserImplicitTrajectoryWrapper.pruneTrajectory` | `0x142d05180` | Converts and prunes trajectory data, rebuilds a wrapper and schedules saving |
| `implicit.pruneTrajectoryProto` | `0x142d056c0` | Creates a new trajectory UUID and filters steps |
| `implicit.newImplicitTrajectoryWrapper` | `0x142d03e00` | Generates a fresh initialization-state UUID and installs it in trajectory metadata with workspace metadata and current timestamp |

The wire envelope is a 12-byte nonce followed by AES-256-GCM ciphertext and a
16-byte authentication tag. No compression or outer protobuf wrapper occurs on
this path. The decoder obtains encryption material in memory from the exact
leased PE. Reports and receipts contain producer/schema hashes and structural
metadata only.

`ImplicitTrajectory` has field 1 `gemini_coder.Trajectory` and field 5
`TrajectoryScope`. Trajectory identity and cascade identity are distinct fields.
`Trajectory` also defines steps, parent references and metadata with parent/root
conversation IDs. `BaseTrajectoryIdentifier` separately defines cascade,
inline trajectory, fork and implicit-file-URI alternatives. These descriptors
establish real restoration semantics. A particular empty record's qualification
requires decoding its entire accepted subset and proving the absence of those
restoration fields.

## Accepted subset

The implementation binds five exact embedded descriptor hashes and the whole
runtime hash. It accepts a flat namespace of 1–100 canonical UUIDv4 `.pb` files.
Each file must authenticate before parsing. Its entire plaintext must contain
exactly the following subset.

- Implicit fields 1 and 5, each once.
- Trajectory fields 1, 6, 4 and 7, each once. Type is USER_MAINLINE (1).
  Canonical trajectory and cascade IDs differ, and cascade ID matches filename.
- Metadata fields 1, 2 and 3, each once. A single workspace, a bounded protobuf
  Timestamp and a canonical initialization-state UUID.
- Workspace fields 1 and 3, each once. Repository is an empty message.
- Scope has only workspace URI. It matches the metadata workspace bytes and
  names an absolute local Windows file URI without authority, traversal,
  query, fragment, control characters or alternate stream.

Selected IDs are refused in trajectory, cascade and initialization identity.
Missing original members require a full current 100-file retention window,
matching the producer's excess-only pruning rule. A shrunken window is refused.
Every additional field, duplicate, incompatible wire type, malformed encoding,
step, parent/base reference, external URI or authentication failure is refused.
Nonempty trajectories need separate semantic qualification. Current files,
identities, hashes, IDs and empty restoration closures are retained in audit
alongside the original/current preservation trees.

All files remain under the same Windows leases through qualification,
publication and lock disposition. The runtime content reader uses the original
frontend lifecycle handle. Unknown opaque formats receive no namespace-wide
exemption.

## Historical boundary

The original strict completion receipts prove the selected child actions and
their original after-state. The retained original implicit hashes prove which
bytes were preserved at that checkpoint. They cannot reconstruct a later
missing file's plaintext. Producer pruning explains a supported mechanism for
natural disappearance, but does not attribute each missing file to a particular
prune invocation. Recovery proves all surviving current sources' target absence
and empty restoration closure. It does not claim that missing old bytes remain
on disk or that the manager deleted them.
