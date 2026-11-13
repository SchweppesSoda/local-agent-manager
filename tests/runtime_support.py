"""Optional integration runtimes skip only when genuinely unconfigured."""
from tests import bootstrap
import os
import unittest
from local_agent_manager import office_leveldb


def require_leveldb():
    try:
        result = office_leveldb.runtime()
    except office_leveldb.OfficeLevelDBError:
        if os.environ.get("LARJ_REQUIRE_LEVELDB_TESTS") == "1" or os.environ.get("LARJ_LEVELDB_RUNTIME") or office_leveldb._runtime_directory().exists():
            raise
        raise unittest.SkipTest("Pinned LevelDB runtime is not configured")
    os.environ["LARJ_LEVELDB_RUNTIME"] = str(result[1])
    return result
