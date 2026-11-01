"""Single-file transport for existing verified session and project ZIP formats."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import zipfile

LIMIT = 2304 * 1024 ** 2


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def pack(session, project, output):
    session, project, output = map(Path, (session, project, output))
    m = {'format': 'codex-single-transfer-v1', 'session': session.name,
         'files': {p.name: {'sha256': digest(p), 'size': p.stat().st_size} for p in (session, project)}}
    temporary = output.with_name(output.name + '.part-' + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED) as z:
            z.write(session, session.name)
            z.write(project, project.name)
            z.writestr('bundle-manifest.json', json.dumps(m).encode())
        os.rename(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    output.with_suffix('.zip.sha256').write_text(digest(output) + '  ' + output.name + '\n', encoding='utf-8')
    return output


def resolve(archive):
    try:
        return _resolve(archive)
    except (KeyError, TypeError, AttributeError, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Invalid single-file transfer manifest or unsupported ZIP: ' + str(exc)) from exc


def _resolve(archive):
    archive = Path(archive).resolve()
    if archive.stat().st_size > LIMIT:
        raise ValueError('Transfer ZIP exceeds 2.25 GiB.')
    with zipfile.ZipFile(archive) as z:
        if 'bundle-manifest.json' not in z.namelist():
            return archive
        sidecar = archive.with_suffix('.zip.sha256')
        if sidecar.exists():
            tokens = sidecar.read_text(encoding='utf-8-sig').split()
            if not tokens or tokens[0].lower() != digest(archive):
                raise ValueError('Single-file transfer outer SHA256 mismatch.')
        entries = z.infolist()
        if len(entries) != 3 or len({e.filename.casefold() for e in entries}) != 3:
            raise ValueError('Invalid single-file transfer member list.')
        if z.getinfo('bundle-manifest.json').file_size > 65536 or sum(e.file_size for e in entries) > LIMIT:
            raise ValueError('Single-file transfer expansion limit exceeded.')
        m = json.loads(z.read('bundle-manifest.json'))
        if (not isinstance(m, dict) or m.get('format') != 'codex-single-transfer-v1'
                or not isinstance(m.get('files'), dict) or len(m['files']) != 2
                or not isinstance(m.get('session'), str)
                or m.get('session') not in m['files']):
            raise ValueError('Invalid single-file transfer manifest.')
        if set(m['files']) | {'bundle-manifest.json'} != set(z.namelist()):
            raise ValueError('Unlisted single-file transfer members.')
        for e in entries:
            if (not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.project)?\.zip|bundle-manifest\.json', e.filename)
                    or stat.S_ISLNK(e.external_attr >> 16)):
                raise ValueError('Unsafe single-file transfer member.')
        root = Path(tempfile.gettempdir()).resolve() / 'codex-transfer-cache'
        cache = root / digest(archive)
        for folder in (root, cache):
            if folder.is_symlink() or (folder.exists() and folder.resolve() != folder):
                raise ValueError('Unsafe single-file transfer cache.')
            folder.mkdir(exist_ok=True)
        for name, item in m['files'].items():
            if (not isinstance(item, dict) or type(item.get('size')) is not int
                    or not 0 <= item['size'] <= LIMIT or z.getinfo(name).file_size != item['size']
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
                raise ValueError('Invalid single-file transfer checksum or size.')
            target = cache / name
            if target.is_symlink():
                raise ValueError('Unsafe cached transfer member.')
            if target.exists():
                if digest(target) != item['sha256']:
                    raise ValueError('Cached transfer member changed; remove its cache directory and retry.')
                continue
            temporary = target.with_name(target.name + '.part-' + uuid.uuid4().hex)
            try:
                h = hashlib.sha256()
                with z.open(name) as src, temporary.open('xb') as dst:
                    while chunk := src.read(1024 ** 2):
                        h.update(chunk)
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if h.hexdigest() != item['sha256']:
                    raise ValueError('Single-file transfer SHA256 mismatch: ' + name)
                if os.name == 'nt':
                    os.rename(temporary, target)
                else:
                    os.link(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return cache / m['session']
