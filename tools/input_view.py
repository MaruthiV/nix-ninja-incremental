from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import secrets
import shutil
import stat
import tempfile


class InputView:
    def __init__(self, marker):
        self.marker = Path(marker).resolve(strict=True)
        self.control = self.marker.parent
        self.data = json.loads(self.marker.read_text())
        if self.data["schema_version"] != 1:
            raise ValueError("unsupported input-view version")
        self.root = Path(self.data["root"]).resolve(strict=True)
        if self.root != self.control / "inputs":
            raise ValueError("input-view root does not match its controller")
        if stat.S_IMODE(self.control.stat().st_mode) & 0o077:
            raise ValueError("input-view controller must be private")
        lock = self.control / "view.lock"
        if lock.is_symlink() or not lock.is_file():
            raise ValueError("input-view lock must be a regular file")

    @classmethod
    def create(cls, parent="/tmp", provenance=None):
        control = Path(tempfile.mkdtemp(prefix="nni-input-view-", dir=parent)).resolve()
        inputs = control / "inputs"
        inputs.mkdir()
        (control / "view.lock").touch(mode=0o600)
        marker = control / "view.json"
        data = {"schema_version": 1, "id": secrets.token_hex(16), "root": str(inputs),
                "provenance": provenance or {}}
        with marker.open("x") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
        marker.chmod(0o600)
        return cls(marker)

    @contextmanager
    def lease(self):
        with (self.control / "view.lock").open("r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("input view is busy") from error
            try:
                yield self.root
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def close(self):
        with self.lease():
            shutil.rmtree(self.control)

    def validate_links(self):
        for path in self.root.rglob("*"):
            if path.is_symlink():
                target = path.resolve()
                if not target.is_relative_to(self.root):
                    parts = target.parts
                    if len(parts) < 4 or parts[:3] != ("/", "nix", "store"):
                        raise ValueError(f"input-view symlink escapes stable roots: {path}")
                    if not Path(*parts[:4]).exists():
                        raise ValueError("input-view symlink targets an absent store root")
            elif not path.is_file() and not path.is_dir():
                raise ValueError(f"unsupported input-view file type: {path}")


if __name__ == "__main__":
    raise SystemExit("input_view is used by workload and oracle controllers")
