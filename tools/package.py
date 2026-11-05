"""Build portable source artifacts from an explicit non-sensitive allowlist."""
from datetime import datetime, timezone
from pathlib import Path
import tarfile
import zipfile

root = Path(__file__).resolve().parents[1]
output = root / 'dist'
output.mkdir(exist_ok=True)
files = [root / name for name in ('README.md', 'PROTOCOL.md', 'VALIDATION.md', 'pyproject.toml', 'xianniu')]
files += sorted((root / 'xianniu_linux').glob('*.py'))
files += sorted((root / 'tests').glob('*.py'))
files += sorted((root / 'examples').glob('*.example.json'))
files += [Path(__file__).resolve()]
stamp = datetime.now(timezone.utc).timetuple()[:6]
with zipfile.ZipFile(output / 'xianniu-linux-0.1.0.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        relative = path.relative_to(root).as_posix()
        entry = zipfile.ZipInfo('xianniu-linux/' + relative, stamp)
        entry.create_system = 3
        entry.external_attr = (0o100755 if relative == 'xianniu' else 0o100644) << 16
        entry.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(entry, path.read_bytes())
with tarfile.open(output / 'xianniu-linux-0.1.0.tar.gz', 'w:gz') as archive:
    for path in files:
        relative = path.relative_to(root).as_posix()
        info = archive.gettarinfo(str(path), 'xianniu-linux/' + relative)
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        info.mode = 0o755 if relative == 'xianniu' else 0o644
        with path.open('rb') as stream:
            archive.addfile(info, stream)
print('\n'.join(str(path) for path in output.iterdir()))
