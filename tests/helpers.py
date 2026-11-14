import atexit
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="agent-cost-tests-"))
atexit.register(shutil.rmtree, ROOT, True)

HOME = ROOT / "home"
STATE = ROOT / "state"
HOME.mkdir()
STATE.mkdir()

os.environ["HOME"] = str(HOME)
os.environ["XDG_STATE_HOME"] = str(STATE)
os.environ["AGENT_COST_STATE_DIR"] = str(STATE / "agent-cost")
os.environ["AGENT_COST_TMP_DIRS"] = str(ROOT / "tmp")
(ROOT / "tmp").mkdir()
