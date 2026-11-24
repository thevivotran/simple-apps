import io
import stat
import tempfile
import zipfile
from pathlib import Path

from simple_apps.config import Config

TOKEN = "t" * 32


def make_config(tmp: Path, **overrides) -> Config:
    return Config(admin_token=TOKEN, data_dir=tmp, port=0, **overrides)


def make_zip(files: dict[str, bytes | str], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


class TempDirMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
