"""Companion project archives preserve runnable directory structure without changing session ZIP limits."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
import uuid
import zipfile

LIMIT = 2 * 1024 ** 3
FILE_LIMIT = 512 * 1024 ** 2
MAX_FILES = 20000
EXCLUDE = {'.git', '.codex', '.agents', '.aws', '.ssh', '.docker', '.kube', '.azure', '__pycache__',
           'session-exports', 'session-files', 'session-projects', '.validation-temp',
           'native-test-profile', 'native-gravity-test-profile'}
BINARY = {'.exe', '.dll', '.pak', '.bin', '.dat', '.png', '.jpg', '.jpeg', '.ico', '.woff', '.woff2'}


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def private_data_roots(home, include_env=True):
    home = Path(home).expanduser().resolve()
    private_roots = {home}
    if include_env and os.environ.get('CODEX_SQLITE_HOME'):
        private_roots.add(Path(os.environ['CODEX_SQLITE_HOME']).expanduser().resolve())
    config = home / 'config.toml'
    if config.is_file():
        settings = tomllib.loads(config.read_text(encoding='utf-8-sig'))
        if settings.get('sqlite_home'):
            sqlite_root = Path(settings['sqlite_home']).expanduser()
            private_roots.add((sqlite_root if sqlite_root.is_absolute() else home / sqlite_root).resolve())
    return private_roots


def export_project(root, archive, sanitizer, files, home=None):
    root, archive = Path(root).resolve(), Path(archive).resolve()
    home = Path(home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser().resolve()
    private_roots = private_data_roots(home)
    def private(path):
        resolved = path.resolve()
        return any(resolved.is_relative_to(p) for p in private_roots)
    if not root.is_dir() or root == Path(root.anchor) or root == Path.home():
        raise ValueError('Choose a specific project folder, not a drive or user home.')
    if private(root):
        raise ValueError('Project root is inside private Codex data storage; nothing exported.')
    if archive.is_relative_to(root):
        raise ValueError('Project archive output must be outside the exported project.')
    entries, skipped = [], []
    total = 0
    temporary = archive.with_name(archive.name + '.part-' + uuid.uuid4().hex)
    archive.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for folder, dirs, names in os.walk(root, followlinks=False):
                for name in list(dirs):
                    p = Path(folder) / name
                    if name in EXCLUDE or p.is_symlink() or getattr(p, 'is_junction', lambda: False)() or private(p):
                        dirs.remove(name)
                        skipped.append({'path': p.relative_to(root).as_posix(), 'reason': 'excluded_directory'})
                for name in sorted(names):
                    p = Path(folder) / name
                    relative = p.relative_to(root).as_posix()
                    if p.is_symlink() or private(p) or files.forbidden(p, home, allow_dependencies=True) or p.suffix in {'.log', '.pyc'} or name.endswith('-pid.txt'):
                        skipped.append({'path': relative, 'reason': 'private_or_generated_file'})
                        continue
                    size = p.stat().st_size
                    if size > FILE_LIMIT or total + size > LIMIT or len(entries) >= MAX_FILES:
                        raise ValueError('Project exceeds 512 MiB per file / 2 GiB / 20000 files; no partial project delivered.')
                    if p.suffix.lower() in BINARY:
                        # Scan runtime bytes for recognizable credentials; assignment heuristics
                        # are for text and would mistake executable symbol names for secrets.
                        suspicious = False
                        tail = b''
                        with p.open('rb') as f:
                            while chunk := f.read(1024 * 1024):
                                text = (tail + chunk).decode('utf-8', errors='ignore')
                                if sanitizer.signature_has_secrets(text):
                                    suspicious = True
                                    break
                                tail = chunk[-4096:]
                    else:
                        suspicious = files.contains_secrets(p.read_bytes(), sanitizer,
                                                           source_code=p.suffix.lower() in files.SOURCE_SUFFIXES)
                    if suspicious:
                        skipped.append({'path': relative, 'reason': 'possible_credentials'})
                        continue
                    before = digest(p)
                    z.write(p, 'project/' + relative)
                    if digest(p) != before:
                        raise ValueError('Project changed during export: ' + relative)
                    entries.append({'path': relative, 'size': size, 'sha256': before})
                    total += size
            manifest = {'format': 'codex-project-transfer-v1', 'source_root': str(root),
                        'name': files.portable_name(root.name), 'files': entries, 'skipped': skipped}
            manifest['critical_checks'] = check_critical(root, entries)
            z.writestr('project-manifest.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        # Validate the actual zipped bytes, not only the source hashes.
        verify_project(temporary, None, files)
        os.rename(temporary, archive)
        sha = digest(archive)
        archive.with_suffix('.zip.sha256').write_text(sha + '  ' + archive.name + '\n', encoding='utf-8')
        return {'filename': archive.name, 'sha256': sha, 'source_root': str(root),
                'files': len(entries), 'expanded_bytes': total, 'skipped': skipped,
                'critical_checks': manifest['critical_checks']}
    finally:
        temporary.unlink(missing_ok=True)


def check_critical(root, entries):
    names = {e['path'] for e in entries}
    checks, missing = [], []
    # Check application entry points, not dependency-package metadata conventions.
    active = [n for n in names if n in ('package.json', 'app/package.json', 'resources/app/package.json')
              or n.endswith('/resources/app/package.json')]
    for name in sorted(active):
        try:
            m = json.loads((root / name).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError('Critical project files: invalid ' + name) from exc
        if not isinstance(m, dict):
            raise ValueError('Critical project files: invalid ' + name)
        main = m.get('main')
        if isinstance(main, str) and main:
            p = (root / name).parent / main
            if not p.resolve().is_relative_to(root):
                raise ValueError('Critical project files: entry outside project: ' + name)
            relative = p.resolve().relative_to(root).as_posix()
            candidates = [relative, relative + '.js', relative + '.cjs', relative + '/index.js']
            present = any(c in names for c in candidates)
            checks.append({'file': relative, 'present': present, 'kind': 'package_entry'})
            if not present:
                missing.append(relative)
    for exe in sorted(n for n in names if n.lower().endswith('.exe')):
        base = PurePosixPath(exe).parent
        if str(base / 'resources/app/package.json') in names or str(base / 'resources/app.asar') in names:
            for dependency in ('icudtl.dat', 'resources.pak', 'ffmpeg.dll'):
                n = str(base / dependency)
                present = n in names
                checks.append({'file': n, 'present': present, 'kind': 'electron_runtime'})
                if not present:
                    missing.append(n)
            present = any(n.startswith(str(base / 'locales') + '/') for n in names)
            checks.append({'file': str(base / 'locales'), 'present': present, 'kind': 'electron_locales'})
            if not present:
                missing.append(str(base / 'locales'))
    if missing:
        raise ValueError('Critical project files missing or excluded: ' + ', '.join(missing))
    return {'status': 'passed' if checks else 'no_known_entry_detected', 'checks': checks,
            'launch_test': 'not_performed', 'dependency_graph': 'not_verified'}


def verify_project(archive, expected, files):
    try:
        return _verify_project(archive, expected, files)
    except (KeyError, TypeError, AttributeError, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Invalid project manifest or unsupported ZIP: ' + str(exc)) from exc


def _verify_project(archive, expected, files):
    if expected and digest(archive) != expected:
        raise ValueError('Project companion SHA256 mismatch.')
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > MAX_FILES + 1 or sum(e.file_size for e in entries) > LIMIT + 8 * 1024 ** 2:
            raise ValueError('Project archive exceeds expansion limits.')
        names = set()
        for e in entries:
            parts = PurePosixPath(e.filename).parts
            if (not parts or e.filename.startswith('/') or '\\' in e.filename or ':' in e.filename
                    or '..' in parts or e.filename != '/'.join(parts)
                    or e.filename.casefold() in names or stat.S_ISLNK(e.external_attr >> 16)
                    or any(files.portable_name(p) != p for p in parts)):
                raise ValueError('Unsafe project ZIP member: ' + e.filename)
            names.add(e.filename.casefold())
        if z.getinfo('project-manifest.json').file_size > 8 * 1024 ** 2:
            raise ValueError('Project manifest too large.')
        m = json.loads(z.read('project-manifest.json'))
        if (not isinstance(m, dict) or m.get('format') != 'codex-project-transfer-v1'
                or not isinstance(m.get('source_root'), str) or not isinstance(m.get('files'), list)
                or not isinstance(m.get('name'), str) or files.portable_name(m['name']) != m['name']):
            raise ValueError('Invalid project manifest.')
        expected_names = {'project-manifest.json'}
        for item in m['files']:
            if not isinstance(item, dict) or not isinstance(item.get('path'), str):
                raise ValueError('Invalid project file entry.')
            name = 'project/' + item['path']
            if name in expected_names or name.casefold() not in names:
                raise ValueError('Duplicate or missing project file.')
            expected_names.add(name)
            if type(item.get('size')) is not int or not 0 <= item['size'] <= FILE_LIMIT or z.getinfo(name).file_size != item['size']:
                raise ValueError('Invalid project file size.')
            with z.open(name) as f:
                if hashlib.file_digest(f, 'sha256').hexdigest() != item.get('sha256'):
                    raise ValueError('Project file SHA256 mismatch: ' + name)
        if expected_names != {e.filename for e in entries}:
            raise ValueError('Unlisted project ZIP entries.')
    return m


def companion_path(session_archive, descriptor, files):
    if not isinstance(descriptor, dict):
        raise ValueError('Invalid project companion descriptor.')
    name, sha = descriptor.get('filename'), descriptor.get('sha256')
    if not isinstance(name, str) or files.portable_name(name) != name or not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha):
        raise ValueError('Invalid project companion filename or SHA256.')
    archive = Path(session_archive).resolve().with_name(name)
    if not archive.is_file():
        raise ValueError('Missing project companion: ' + str(archive) + '. Copy both ZIPs to the same folder.')
    if digest(archive) != sha:
        raise ValueError('Project companion SHA256 mismatch.')
    return archive


def restore_project(session_archive, descriptor, cwd, files):
    if not descriptor:
        return None
    archive = companion_path(session_archive, descriptor, files)
    sha = descriptor['sha256']
    m = verify_project(archive, sha, files)
    if m['source_root'] != descriptor.get('source_root'):
        raise ValueError('Project source identity mismatch.')
    parent = Path(cwd).resolve() / 'session-projects'
    root = parent / sha[:20] / m['name']
    def safe_mkdir(path):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Unsafe project target directory.')
        if not path.exists():
            safe_mkdir(path.parent)
            path.mkdir(exist_ok=True)
        if not path.is_dir() or path.resolve() != path:
            raise ValueError('Project target directory conflict.')
    safe_mkdir(root)
    marker = root / 'PROJECT-SOURCE-SHA256.txt'
    if marker.exists():
        if marker.is_symlink() or marker.read_text(encoding='utf-8').strip() != sha:
            raise ValueError('Project restoration marker conflict.')
    elif any(root.iterdir()):
        raise ValueError('Refusing to claim an existing project directory.')
    else:
        files.publish_file(marker, (sha + '\n').encode())
    path_map = {m['source_root']: root.as_posix()}
    with zipfile.ZipFile(archive) as z:
        for item in m['files']:
            target = root / item['path']
            safe_mkdir(target.parent)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError('Unsafe project file destination.')
            if not target.exists():
                # Reuse atomic publication; project files may exceed attachment limits.
                files.publish_file(target, z.read('project/' + item['path']))
            original = (PurePosixPath(m['source_root'].replace('\\', '/')) / item['path']).as_posix()
            path_map[original] = target.as_posix()
            path_map[original.replace('/', '\\')] = target.as_posix()
    return {'root': str(root), 'path_map': path_map, 'files': len(m['files']), 'sha256': sha,
            'skipped': m.get('skipped', [])}
