"""Held, synchronous Windows boundary for this owner's legacy Cindy import.

Only the admitted task controller calls this. The launcher/package handles keep
the actual product writer out while parent namespace handles permit atomic
replacement of its selected configuration entries. They are not a claim that
an exclusive destination handle can survive os.replace. Other manager writers
are serialized by the existing three physical-root mutexes.
"""
from contextlib import ExitStack, closing, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil

from . import frozen_files as ff
from .cindy_isolation_plan import IsolationPlanError, closure_batches, directory_identity, fingerprint
from .cindy_isolation_recovery import (auth_state, compare_packet, digest, file_state,
                                       projected_registry, q)
from .operation_store import write_new_json, _atomic_replace_with_retry
from .operation_store import utc_now
from .windows_held_files import HeldFiles, ensure_descriptor_capacity

AUTH_LOCK_PROTOCOL = 'auth.lock-refresh-commit.v1'


def fail(code):
    raise IsolationPlanError(code)


def no_cindy():
    from .office_runtime import processes
    if any(str(row.get("name", "")).casefold() in {"cindy", "cindy.exe"} for row in processes()):
        fail("cindy_must_be_closed_before_startup_fence")


def auth_protocol_census():
    from .codex_desktop_state import _running_related_process_records
    return _running_related_process_records()


class NativeAuthLocks:
    """The native cross-process auth.lock protocol.

    This preserves T3 while serializing its credential commits with the one
    atomic auth-link replacement. Empty native coordination files persist;
    existing locks and their ACLs are never changed or removed.
    """
    def __init__(self, homes):
        self.homes = sorted({Path(home) for home in homes}, key=lambda p: str(p).casefold())
        self.stack = ExitStack()
        self.created = []

    def __enter__(self):
        from .mutation_guard import _os_lock
        try:
            for home in self.homes:
                path = home / 'auth.lock'
                self.stack.enter_context(ff._parent_fence(home, 'auth.lock'))
                if not os.path.lexists(path):
                    try:
                        created = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_BINARY, 0o600)
                    except FileExistsError: pass
                    else:
                        os.close(created); self.created.append(str(path))
                before = path.lstat(); ff._plain(before)
                fd = ff._windows_fd(path)
                self.stack.callback(os.close, fd)
                if ff._identity(os.fstat(fd)) != ff._identity(before): fail('native_auth_lock_replaced')
                _os_lock(fd)
                self.stack.callback(_os_lock, fd, unlock=True)
            return self
        except BaseException:
            self.stack.close(); raise

    def __exit__(self, *args): return self.stack.__exit__(*args)


def qualify_auth_writers(spec):
    approved = spec['controller'].get('auth_lock_runtimes', {})
    homes = [Path(spec[key]['path']) for key in ('source', 'target')]
    for record in auth_protocol_census():
        if record['name'].casefold() != 'codex.exe': continue
        home = record.get('codex_home')
        if not home: fail('native_auth_writer_home_unproven')
        if not any(os.path.samefile(home, root) for root in homes): continue
        binary = record.get('executable_path')
        if not binary: fail('native_auth_writer_image_unproven')
        physical = Path(binary).resolve(strict=True)
        if not os.path.samefile(binary, physical): fail('native_auth_writer_alias_changed')
        entry = approved.get(str(physical))
        if not entry or entry.get('lock_protocol') != AUTH_LOCK_PROTOCOL:
            fail('native_auth_writer_protocol_unqualified')
        ff._plain(physical.lstat())
        if digest(physical) != entry['sha256']: fail('native_auth_writer_binary_changed')


def process_evidence():
    """A synchronous writer identity, never a caller-supplied PID."""
    from .cindy_isolation_lifecycle import windows_identity
    identity = windows_identity(os.getpid())
    if identity is None:
        fail("writer_identity_unproven")
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        fail("writer_session_unproven")
    return {"pid": os.getpid(), "started_unix": identity[1], "executable": identity[0],
            "admitted_at": utc_now(),
            "machine": os.environ.get("COMPUTERNAME"), "windows_session": session.value,
            'user_sid':current_user_sid(),
            "model": "synchronous_in_process_no_delegated_writer"}


def current_user_sid():
    """Only the Windows security principal, never authentication credentials."""
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True); adv=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=(wintypes.HANDLE,)
    adv.OpenProcessToken.argtypes=(wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE))
    adv.OpenProcessToken.restype=wintypes.BOOL
    adv.GetTokenInformation.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD))
    adv.GetTokenInformation.restype=wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes=(ctypes.c_void_p,ctypes.POINTER(wintypes.LPWSTR));adv.ConvertSidToStringSidW.restype=wintypes.BOOL
    token=wintypes.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(token)):raise ctypes.WinError(ctypes.get_last_error())
    try:
        size=wintypes.DWORD();adv.GetTokenInformation(token,1,None,0,ctypes.byref(size))
        data=ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token,1,data,size,ctypes.byref(size)):raise ctypes.WinError(ctypes.get_last_error())
        sid=ctypes.cast(data,ctypes.POINTER(ctypes.c_void_p))[0];text=wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid,ctypes.byref(text)):raise ctypes.WinError(ctypes.get_last_error())
        try:return text.value
        finally:
            kernel.LocalFree.argtypes=(ctypes.c_void_p,);kernel.LocalFree(ctypes.cast(text,ctypes.c_void_p))
    finally:kernel.CloseHandle(token)


def read_shared_fd(path):
    """Open just this file for reading with FILE_SHARE_READ."""
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


class ReadLeases:
    """Read sharing only, including namespace fences; handles never inherited."""
    def __init__(self):
        self.stack, self.parents, self.files = ExitStack(), {}, {}

    def __enter__(self): return self
    def __exit__(self, *args): return self.stack.__exit__(*args)

    def acquire(self, path):
        path = Path(path)
        identity = path.stat()
        key = (identity.st_dev, identity.st_ino)
        if key in self.files:
            return
        ff._plain(path.lstat())
        for parent in reversed(path.parents):
            before = parent.lstat(); ff._plain(before, directory=True)
            name = str(parent)
            if name not in self.parents:
                fd = ff._windows_fd(parent, directory=True)
                self.stack.callback(os.close, fd); self.parents[name] = fd
            if ff._identity(os.fstat(self.parents[name])) != ff._identity(before):
                fail("source_parent_replaced")
        # ff's delete=True also requests DELETE; source needs READ only.
        fd = read_shared_fd(path)
        self.stack.callback(os.close, fd)
        if ff._identity(os.fstat(fd)) != ff._identity(identity): fail("source_file_replaced")
        self.files[key] = fd


def auth_permissions(path, *, copy_to=None):
    """Synchronous in-process owner/group/DACL preservation, no child writer.

    Raw ACE order and grants remain identical; protection prevents future
    inherited broadening. Inherited labels do not enter the effective-grant
    digest. Unknown ACE forms refuse qualification before credential copying.
    """
    from ctypes import wintypes
    adv=ctypes.WinDLL('advapi32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    ptr=ctypes.c_void_p
    adv.GetNamedSecurityInfoW.argtypes=(wintypes.LPWSTR,ctypes.c_int,wintypes.DWORD,
        ctypes.POINTER(ptr),ctypes.POINTER(ptr),ctypes.POINTER(ptr),ctypes.POINTER(ptr),ctypes.POINTER(ptr))
    adv.GetNamedSecurityInfoW.restype=wintypes.DWORD
    adv.SetNamedSecurityInfoW.argtypes=(wintypes.LPWSTR,ctypes.c_int,wintypes.DWORD,ptr,ptr,ptr,ptr)
    adv.SetNamedSecurityInfoW.restype=wintypes.DWORD
    adv.ConvertSidToStringSidW.argtypes=(ptr,ctypes.POINTER(wintypes.LPWSTR));adv.ConvertSidToStringSidW.restype=wintypes.BOOL
    adv.GetAce.argtypes=(ptr,wintypes.DWORD,ctypes.POINTER(ptr));adv.GetAce.restype=wintypes.BOOL
    adv.GetSecurityDescriptorControl.argtypes=(ptr,ctypes.POINTER(wintypes.WORD),ctypes.POINTER(wintypes.DWORD))
    adv.GetSecurityDescriptorControl.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=(ptr,)
    class ACL(ctypes.Structure):
        _fields_=[('revision',ctypes.c_ubyte),('reserved',ctypes.c_ubyte),('size',ctypes.c_ushort),('count',ctypes.c_ushort),('reserved2',ctypes.c_ushort)]
    class ACE(ctypes.Structure):
        _fields_=[('kind',ctypes.c_ubyte),('flags',ctypes.c_ubyte),('size',ctypes.c_ushort)]
    def sid_string(sid):
        value=wintypes.LPWSTR()
        if not sid or not adv.ConvertSidToStringSidW(sid,ctypes.byref(value)):fail('credential_sid_unqualified')
        try:return value.value
        finally:kernel.LocalFree(ctypes.cast(value,ptr))
    owner,group,dacl,descriptor=ptr(),ptr(),ptr(),ptr()
    code=adv.GetNamedSecurityInfoW(str(path),1,7,ctypes.byref(owner),ctypes.byref(group),ctypes.byref(dacl),None,ctypes.byref(descriptor))
    if code:raise ctypes.WinError(code)
    try:
        if not dacl:fail('credential_null_dacl_unqualified')
        entries=[];categories=[]
        for index in range(ctypes.cast(dacl,ctypes.POINTER(ACL)).contents.count):
            ace=ptr()
            if not adv.GetAce(dacl,index,ctypes.byref(ace)):fail('credential_ace_unavailable')
            header=ctypes.cast(ace,ctypes.POINTER(ACE)).contents
            if header.kind not in (0,1) or header.size<12:fail('credential_ace_format_unqualified')
            mask=ctypes.c_uint32.from_address(ace.value+4).value
            entries.append((sid_string(ace.value+8),mask,header.kind,header.flags & 0x0f))
            categories.append((2 if header.flags & 0x10 else 0)+(0 if header.kind==1 else 1))
        if categories!=sorted(categories):fail('credential_acl_noncanonical')
        control=wintypes.WORD();revision=wintypes.DWORD()
        if not adv.GetSecurityDescriptorControl(descriptor,ctypes.byref(control),ctypes.byref(revision)):
            fail('credential_descriptor_control_unavailable')
        proof={'effective_acl_sha256':fingerprint({'owner':sid_string(owner),'group':sid_string(group),'rules':sorted(entries)}),
               'protected':bool(control.value & 0x1000)}
        if copy_to is not None:
            # All pointers remain valid until SetNamedSecurityInfo returns.
            # This is a synchronous syscall: no delegated ACL process can
            # survive the writer's kernel-proven exit during recovery.
            code=adv.SetNamedSecurityInfoW(str(copy_to),1,0x80000007,owner,group,dacl,None)
            if code:raise ctypes.WinError(code)
            actual=auth_permissions(copy_to)
            if actual['effective_acl_sha256']!=proof['effective_acl_sha256'] or not actual['protected']:
                fail('credential_permission_copy_mismatch')
            return actual
        return proof
    finally:kernel.LocalFree(descriptor)


def closure_in_transaction(connection, root_ids):
    rows = list(connection.execute("select id,rollout_path,attached_thread_parent_id from threads"))
    edges = [tuple(r) for r in connection.execute("select parent_thread_id,child_thread_id from thread_spawn_edges")]
    for ident, path, parent in rows:
        if parent: edges.append((parent, ident))
        with Path(path).open(encoding="utf8") as stream: header = json.loads(stream.readline())
        payload = header.get("payload", {})
        if header.get("type") != "session_meta" or payload.get("id") != ident: fail("native_header_identity_changed")
        def parents(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"parent_thread_id", "parentThreadId", "parent_session_id", "forked_from_id"} and isinstance(child, str):
                        edges.append((child, ident))
                    elif isinstance(child, (dict, list)): parents(child)
            elif isinstance(value, list):
                for child in value: parents(child)
        parents(payload)
    return sorted({i for b in closure_batches({r: ["task"] for r in root_ids}, edges, protected_ids=set())
                   for i in b["native_ids"]})


def verify_retained_target(spec, *, packet_names=None, file_ids=None):
    """Prove frozen history remains while allowing later native continuation.

    Original payload rows and source-byte prefixes cannot disappear. Only
    enumerated native recency/usage fields and monotone projection offsets may
    evolve. This neither copies nor rolls back any current target data.
    """
    from .cindy_isolation_plan import readonly_sqlite
    from .cindy_isolation_merge import TABLES
    ids = sorted(f["native_id"] for f in spec["native_files"])
    files = {}
    for item in spec["native_files"]:
        if file_ids is not None and item["native_id"] not in file_ids: continue
        path = Path(item["target_path"]); info = path.lstat(); ff._plain(info)
        length = item["size"]
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk: fail("target_original_rollout_truncated")
                hasher.update(chunk); remaining -= len(chunk)
        if hasher.hexdigest() != item["sha256"]: fail("target_original_rollout_prefix_changed")
        files[item["native_id"]] = {"original_prefix_verified": True, "new_bytes": info.st_size - length}
    databases = {}
    mutable = {"threads": {"updated_at", "updated_at_ms", "tokens_used", "has_user_event", "preview", "recency_at", "recency_at_ms"},
               "thread_history_projection_state": {"next_rollout_byte_offset", "next_rollout_ordinal"},
               "thread_read_receipts": {"first_unread_turn", "revision", "last_published_turn"}}
    for packet in spec["database_packets"]:
        name = Path(packet["packet"]).name
        if packet_names is not None and name not in packet_names: continue
        with readonly_sqlite(Path(spec["target"]["path"]) / name) as c:
            c.execute("attach database ? as incoming", (Path(packet["packet"]).as_uri() + "?mode=ro",))
            # Run the complete schema check first. A mismatch in data is then
            # qualified below, never confused with a schema exception.
            original = compare_packet(c, ids=ids, tables=list(packet["rows_inserted"]))
            for table in original["tables"]:
                columns = [tuple(r) for r in c.execute("pragma main.table_info(" + q(table) + ")")]
                keys = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
                stable = [r[1] for r in columns if r[1] not in mutable.get(table, set())]
                if not keys: fail("target_progress_primary_key_missing")
                join = " and ".join("a." + q(k) + " is b." + q(k) for k in keys)
                mismatch = " or ".join("a." + q(k) + " is not b." + q(k) for k in stable)
                if c.execute("select count(*) from incoming." + q(table) + " a left join main." + q(table) + " b on " + join + " where " + mismatch).fetchone()[0]:
                    fail("target_original_payload_or_identity_missing:" + table)
                if table == "thread_history_projection_state":
                    for row in c.execute("select b.thread_id,a.next_rollout_byte_offset,b.next_rollout_byte_offset,a.next_rollout_ordinal,b.next_rollout_ordinal from incoming." + table + " a join main." + table + " b on a.thread_id=b.thread_id"):
                        if row[2] < row[1] or row[4] < row[3] or row[2] > Path(next(f["target_path"] for f in spec["native_files"] if f["native_id"] == row[0])).stat().st_size:
                            fail("target_projection_regressed_or_outside_rollout")
            if name == "state_5.sqlite":
                present = {r[0] for r in c.execute("select id from threads")}
                if not set(spec["controller"].get("protected_target_ids", [])) <= present:
                    fail("protected_target_thread_missing")
            databases[name] = {"original_rows_retained": True, "exact_at_freeze": original["state"] == "after"}
    return {"files": files, "databases": databases, "target_increments_retained": True}


def verify_runtime_switch(spec):
    """Product may add bindings after acceptance; frozen fields still match."""
    current = json.loads(Path(spec["registry_path"]).read_text(encoding="utf-8-sig"))
    if current["pools"]["openai"].get("legacyCodexDefault") is not True: fail("legacy_default_route_lost")
    expected_pool = dict(spec['controller']['registry_before']['pools']['openai'])
    expected_pool['legacyCodexDefault'] = True
    if current['pools']['openai'] != expected_pool: fail('existing_account_pool_configuration_changed')
    for change in spec["binding_changes"]:
        if current.get("bindings", {}).get(change["key"]) != change["after"]: fail("migrated_binding_changed")
    for key,value in spec['controller'].get('expected_bindings',{}).items():
        if current.get('bindings',{}).get(key)!=value: fail('selected_binding_semantics_changed')
    for key, value in spec["controller"].get("protected_bindings", {}).items():
        if current.get("bindings", {}).get(key) != value: fail("protected_account_binding_changed")
    for change in spec["location_changes"]:
        value = json.loads(Path(change["file"]).read_text(encoding="utf-8-sig"))
        if any(value.get(key) != wanted for key, wanted in change["after"].items()): fail("migrated_location_changed")
    source, target = (Path(spec["auth"][key]["path"]) for key in ("source", "target"))
    info = target.lstat(); ff._plain(info)
    if os.path.samefile(source, target): fail("auth_relinked_to_official")
    before = spec["controller"].get("auth_effective_acl_sha256")
    actual = auth_permissions(target)
    if before is not None and actual["effective_acl_sha256"] != before: fail("runtime_auth_permissions_changed")
    if not actual["protected"]: fail("runtime_auth_inheritance_unprotected")
    return {"bindings_and_locations": True, "independent_auth": True, "auth_permissions": True}


class WindowsImportBoundary:
    def __init__(self, spec, draft):
        self.spec, self.draft = spec, draft
        self.stack, self.active, self.sources = ExitStack(), False, {}
        self.source_stack, self.sources_active = ExitStack(), False
        self.ids = sorted(f["native_id"] for f in spec["native_files"])
        self.original = {f["native_id"]: f for f in draft["groups"]["current_shared_candidates"]["native_files"]}

    def __enter__(self):
        try:
            if os.name != "nt": fail("windows_import_boundary_required")
            no_cindy()
            for key in ("profile", "source", "target"):
                if directory_identity(Path(self.spec[key]["path"])) != self.spec[key]: fail("physical_root_changed:" + key)
            held = self.stack.enter_context(HeldFiles())
            for runtime in self.spec["controller"]["startup_files"]:
                held.acquire(Path(runtime["root"]), runtime["file"])
            no_cindy()  # after executable/package and ancestor namespace leases
            self.stack.enter_context(self.source_stack)
            self.sources_active = True
            qualify_auth_writers(self.spec)
            self.auth_locks = self.source_stack.enter_context(NativeAuthLocks([self.spec[key]['path'] for key in ('source', 'target')]))
            self.leases = self.source_stack.enter_context(ReadLeases())
            deps = self.draft["groups"]["current_shared_candidates"]["dependencies"]
            for dependency in sorted(deps, key=lambda d: d["identity"]["path"]):
                path = Path(dependency["identity"]["path"])
                self.source_stack.enter_context(ff._parent_fence(path.parent, path.name))
                ff._plain(path.lstat())
                pinned = ff._windows_fd(path)
                self.source_stack.callback(os.close, pinned)
                expected = self.spec["controller"].get("source_database_identities", {}).get(path.name)
                if expected and ff._identity(os.fstat(pinned)) != [expected["device"], expected["inode"]]:
                    fail("source_database_physical_identity_changed")
                # Pin the namespace/entity BEFORE SQLite opens the path, then
                # acquire its write reservation. A replaced leaf cannot hide
                # a connection to a different physical database.
                connection = self.source_stack.enter_context(closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)))
                connection.execute("pragma query_only=off")
                connection.execute("begin immediate")
                self.source_stack.callback(connection.rollback)
                self.sources[path.name] = connection
                # Pin the actual database file/namespace without excluding our
                # SQLite writer handle. SQLite's transaction locks serialize
                # data writers; this handle additionally excludes replacement.
            paths = [r[0] for r in self.sources["state_5.sqlite"].execute("select rollout_path from threads")]
            paths += [f["path"] for f in self.spec["native_files"]]
            paths += [p["packet"] for p in self.spec["database_packets"]]
            paths += [self.spec["auth"]["source"]["path"]]
            ensure_descriptor_capacity(min(8192, len(paths) + 512))
            for path in paths: self.leases.acquire(path)
            # Hold product config namespaces, not incompatible destination file
            # handles. Known Cindy writers cannot restart; manager mutexes are
            # already held by admitted_import or verification/recovery caller.
            for path in [self.spec["registry_path"], self.spec["auth"]["target"]["path"],
                         *(loc["file"] for loc in self.spec["location_changes"])]:
                path = Path(path)
                if str(path) != self.spec["auth"]["target"]["path"] and path.exists(): ff._plain(path.lstat())
                self.stack.enter_context(ff._parent_fence(path.parent, path.name))
            self.active = True
            self.validate_source()
            return self
        except BaseException:
            self.stack.close(); raise

    def __exit__(self, *args):
        self.active = False
        return self.stack.__exit__(*args)

    def require_held(self):
        if not self.active: fail("windows_import_boundary_not_held")

    def validate_source(self):
        self.require_held()
        if not self.sources_active: fail("source_reservations_already_released_to_native_manager")
        if not auth_state(self.spec).get('source_generation_matches_freeze'):
            fail('auth_source_generation_changed_since_freeze')
        expected_acl = self.spec['controller'].get('auth_effective_acl_sha256')
        if expected_acl and auth_permissions(self.spec['auth']['source']['path'])['effective_acl_sha256'] != expected_acl:
            fail('auth_source_permissions_changed_since_freeze')
        group = self.draft["groups"]["current_shared_candidates"]
        roots = sorted({i for b in group["batches"] for i in b["root_ids"]})
        state = self.sources["state_5.sqlite"]
        if closure_in_transaction(state, roots) != self.ids: fail("source_closure_changed")
        rewrites = {}
        for ident, path in state.execute("select id,rollout_path from threads"):
            if ident in self.original:
                original = self.original[ident]
                if not os.path.samefile(path, original["source"]["path"]): fail("source_native_physical_file_changed")
                rewrites[path] = original["target_path"]
        for item in self.spec["native_files"]:
            frozen = self.original[item["native_id"]]["source"]
            info = Path(frozen["path"]).stat()
            if any(getattr(info, attr) != frozen[key] for attr, key in (("st_dev", "device"), ("st_ino", "inode"), ("st_size", "size"), ("st_mtime_ns", "mtime_ns"))):
                fail("source_file_generation_changed")
            if digest(frozen["path"]) != item["sha256"] or digest(item["path"]) != item["sha256"]:
                fail("source_or_staged_bytes_changed")
        for packet in self.spec["database_packets"]:
            connection = self.sources[Path(packet["packet"]).name]
            if not list(connection.execute("pragma database_list"))[-1][1] == "incoming":
                connection.execute("attach database ? as incoming", (Path(packet["packet"]).as_uri() + "?mode=ro",))
            if compare_packet(connection, ids=self.ids, tables=list(packet["rows_inserted"]), source_paths=rewrites)["state"] != "after":
                fail("source_selected_full_rows_changed")
        sections_projects = state.execute("select thread_section_id,project_id from threads where id in (" + ",".join("?" for _ in self.ids) + ")", self.ids).fetchall()
        sections, projects = ({r[i] for r in sections_projects if r[i] is not None} for i in (0, 1))
        for dependency in group["dependencies"]:
            c = self.sources[Path(dependency["identity"]["path"]).name]
            for table, evidence in dependency["tables"].items():
                if evidence.get("class") != "copy_selected_rows" or evidence["selected_rows"]: continue
                values = sorted(sections if table == "thread_sections" else projects if table in {"projects", "project_roots", "project_idempotency_keys"} else self.ids)
                if values:
                    predicate = " or ".join(q(k) + " in (" + ",".join("?" for _ in values) + ")" for k in evidence["selectors"])
                    if c.execute("select count(*) from " + q(table) + " where " + predicate, values * len(evidence["selectors"])).fetchone()[0]:
                        fail("new_selected_dependency:" + table)

    def release_source(self):
        """After the formal native plan freezes this exact source, let its
        existing app-server protocol own source IO. Keep the product fence and
        frontend namespaces held through native status/verify and publication.
        """
        self.require_held(); self.validate_source()
        self.source_stack.close(); self.sources_active = False

    def verify_import(self):
        from .cindy_isolation_recovery import observe_database
        self.require_held()
        if self.spec.get("allow_target_progress"):
            verify_retained_target(self.spec)
            return
        for item in self.spec["native_files"]:
            if file_state(item["target_path"], item["sha256"]) != "after": fail("target_file_not_exact")
        for packet in self.spec["database_packets"]:
            state = observe_database(Path(self.spec["target"]["path"]) / Path(packet["packet"]).name, packet, self.ids)["state"]
            if state != "after": fail("target_database_not_exact:" + state)

    def snapshot_auth(self):
        self.require_held()
        source, target = (Path(self.spec["auth"][key]["path"]) for key in ("source", "target"))
        state = auth_state(self.spec)
        if not state.get("source_generation_matches_freeze"): fail("auth_generation_changed")
        if state["state"] == "after":
            self.verify_auth(); return
        if state["state"] != "before": fail("auth_before_unqualified")
        expected_acl = self.spec['controller'].get('auth_effective_acl_sha256')
        if expected_acl and auth_permissions(source)['effective_acl_sha256'] != expected_acl:
            fail('auth_source_permissions_changed_since_freeze')
        temporary = target.with_name(".auth-isolation-" + self.spec["plan_sha256"][:16] + ".tmp")
        if temporary.exists(): fail("unresolved_auth_stage_requires_recovery")
        # Apply the original permissions before a single credential byte enters
        # staging, including a process crash during copy.
        with temporary.open("xb") as stream: stream.flush(); os.fsync(stream.fileno())
        auth_permissions(source, copy_to=temporary)
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing); outgoing.flush(); os.fsync(outgoing.fileno())
        if digest(source) != digest(temporary): fail("auth_snapshot_bytes_differ")
        os.replace(temporary, target)
        self.verify_auth()

    def verify_auth(self):
        state = auth_state(self.spec)
        if state['state'] != 'after':
            if self.spec.get('allow_target_progress'):
                self.verify_auth_progress(state)
                return
            fail("auth_entity_or_generation_unverified")
        source, target = (self.spec["auth"][key]["path"] for key in ("source", "target"))
        a, b = auth_permissions(source), auth_permissions(target)
        if a["effective_acl_sha256"] != b["effective_acl_sha256"] or not b["protected"]:
            fail("independent_auth_permissions_changed")

    def verify_auth_progress(self, state):
        # A T3 continuation can refresh the now independent credential file.
        # Keep it; account/read with refresh disabled must prove the frozen
        # existing identity, and the protected original grants must remain.
        from .cindy_isolation_controller import supported_account
        expected = self.spec['controller'].get('account_identity_sha256')
        if not expected or not state.get('independent_regular_file') or not state.get('source_generation_matches_freeze'):
            fail('auth_progress_entity_unqualified')
        source_acl = auth_permissions(self.spec['auth']['source']['path'])
        target_acl = auth_permissions(self.spec['auth']['target']['path'])
        if source_acl['effective_acl_sha256'] != target_acl['effective_acl_sha256'] or not target_acl['protected']:
            fail('auth_progress_permissions_changed')
        if supported_account(self.spec)['identity_sha256'] != expected:
            fail('auth_progress_existing_account_changed')

    def switch_registry(self):
        self.require_held()
        registry = Path(self.spec["registry_path"])
        current = json.loads(registry.read_text(encoding="utf-8-sig"))
        before = self.spec["controller"]["registry_before"]
        after = projected_registry(before, self.spec)
        if current == after: return
        if current != before or digest(registry) != self.spec["registry_before_sha256"]: fail("registry_before_changed")
        self.atomic_config(registry, after)
        if json.loads(registry.read_text(encoding="utf-8-sig")) != after: fail("registry_after_unverified")

    def switch_location(self, change):
        self.require_held()
        path = Path(change["file"])
        current = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None
        if current == change["after"]: return
        if current != change["before"]: fail("location_before_changed")
        self.atomic_config(path, change["after"])
        if json.loads(path.read_text(encoding="utf-8-sig")) != change["after"]: fail("location_after_unverified")

    def atomic_config(self, path, value):
        from .cindy_isolation_execution import config_stage_path
        from .legacy_index import _fsync_directory
        temporary = config_stage_path(self.spec, path)
        # Its intent is durable before creation. A crash leaves a named owned
        # stage for a new remaining operation, even if only half was written.
        write_new_json(temporary, value)
        _atomic_replace_with_retry(temporary, Path(path))
        _fsync_directory(Path(path).parent)

    def verify_switch(self):
        self.require_held(); no_cindy(); self.verify_import(); self.verify_auth()
        from .cindy_isolation_recovery import registry_state
        if registry_state(json.loads(Path(self.spec["registry_path"]).read_text(encoding="utf-8-sig")), self.spec["controller"]["registry_before"], self.spec) != "after": fail("registry_switch_unverified")
        for change in self.spec["location_changes"]:
            if json.loads(Path(change["file"]).read_text(encoding="utf-8-sig")) != change["after"]: fail("location_switch_unverified")

    def observation(self):
        # Source transactions/leases remain held across the complete observation.
        from .cindy_isolation_recovery import observe
        result = observe(self.spec, self.draft, registry_before=self.spec["controller"]["registry_before"])
        result["snapshot_consistency"] = "source_write_reservations_and_file_leases_product_startup_fence"
        result['native_auth_coordination_created'] = list(self.auth_locks.created)
        result["file_batch_components"] = {"file:" + f["target_path"]: "file:" + f["native_id"] for f in self.spec["native_files"]}
        progressive_files = {i for i, state in result["files"].items() if state != "before"}
        progressive_packets = {name for name, states in result["databases"].items() if states["target"]["state"] != "before"}
        if any(state == "diverged" for state in result["files"].values()) or any(states["target"]["state"] not in ("before", "after") for states in result["databases"].values()):
            # An invalid prefix or missing original payload raises and retains
            # the entire unknown operation. Valid later messages stay in place.
            verify_retained_target(self.spec, packet_names=progressive_packets, file_ids=progressive_files)
            for ident in progressive_files:
                if result["files"][ident] != "after": result["files"][ident] = "after_with_increments"
            for name in progressive_packets:
                if result["databases"][name]["target"]["state"] != "after": result["databases"][name]["target"]["state"] = "after_with_increments"
        if result["auth"]["state"] == "after":
            self.verify_auth(); result["auth"]["permissions_verified"] = True
        elif result['auth']['state'] == 'diverged':
            self.verify_auth_progress(result['auth'])
            result['auth'].update(state='after_with_increments', permissions_verified=True,
                existing_account_identity_verified=True)
        return result
