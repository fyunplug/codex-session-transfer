"""Collect chat-linked local files and rebind visible file references on import."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import unquote, urlsplit
import zipfile

FILE_LIMIT = 64 * 1024 * 1024
SOURCE_SUFFIXES = {'.js', '.cjs', '.mjs', '.ts', '.tsx', '.jsx', '.py', '.html', '.htm', '.vue', '.svelte'}
MARKDOWN = re.compile(r'!?\[[^\]\n]*\]\(\s*')
ABSOLUTE = re.compile(r'''(?<![\w/])(?:[A-Za-z]:[\\/]|/mnt/data/)[^\r\n<>"'`|]+''')
BLOCKED_NAMES = {'.env', '.npmrc', '.pypirc', '.netrc', '_netrc', '.git-credentials',
                 'auth.json', 'credentials', 'credentials.json', 'secrets.json', 'config.toml', 'cap_sid'}
BLOCKED_PARTS = {'.git', '.ssh', '.aws', '.docker', '.kube', '.azure', '.sandbox-secrets', 'node_modules'}
BLOCKED_SUFFIXES = {'.key', '.pem', '.pfx', '.p12', '.sqlite', '.db'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def message_fields(row):
    """Only editable/displayed messages and attachment URLs, never tool arguments."""
    payload = row.get('payload', {})
    if row.get('type') == 'response_item' and payload.get('role') in ('user', 'assistant'):
        for part in payload.get('content', []):
            if isinstance(part, dict):
                for key in ('text', 'image_url', 'file_path'):
                    if isinstance(part.get(key), str):
                        yield part, key
    if row.get('type') == 'event_msg' and payload.get('type') in ('user_message', 'agent_message'):
        if isinstance(payload.get('message'), str):
            yield payload, 'message'


def markdown_targets(text):
    for match in MARKDOWN.finditer(text):
        start = match.end()
        end, depth = start, 0
        angle = text[start:].startswith('<')
        while end < len(text) and text[end] != '\n':
            char = text[end]
            if angle:
                if char == '>':
                    end += 1
                    break
            elif char == '(':
                depth += 1
            elif char == ')':
                if depth == 0:
                    break
                depth -= 1
            end += 1
        raw = text[start:end].rstrip()
        if angle:
            if not raw.endswith('>'):
                continue
            left, right = start + 1, start + len(raw) - 1
        else:
            title = re.search(r'\s+["\']', raw)
            left, right = start, start + (title.start() if title else len(raw))
        yield match.start(), end, left, right


def references(records, cwd=None):
    for row in records:
        if row.get('type') in ('session_meta', 'turn_context'):
            cwd = row.get('payload', {}).get('cwd') or cwd
        for container, key in message_fields(row):
            text = container[key]
            if text.lstrip().startswith('<environment_context>'):
                continue
            if key in ('image_url', 'file_path'):
                yield text, cwd
            spans = []
            for start, end, left, right in markdown_targets(text):
                yield text[left:right], cwd
                spans.append((start, end))
            # Bare/backticked Windows paths are common in generated file replies.
            for match in ABSOLUTE.finditer(text):
                if not any(start <= match.start() < end for start, end in spans):
                    yield match[0].rstrip(' .,;，。；)'), cwd


def resolve_reference(reference, cwd):
    value = unquote(reference.strip())
    if value.startswith(('http:', 'https:', 'data:', 'plugin:', 'codex:', 'app:', '#')):
        return None
    if value.startswith('file://'):
        parsed = urlsplit(value)
        if parsed.netloc not in ('', 'localhost'):
            return None
        value = parsed.path
        if re.match(r'^/[A-Za-z]:/', value):
            value = value[1:]
    if value.startswith('sandbox:'):
        value = value[len('sandbox:'):]
    # Only a numeric line/page fragment is removed; other query strings aren't local files.
    value = re.sub(r'(?::\d+(?::\d+)?|#(?:L|page=)?\d+(?:-L?\d+)?)$', '', value)
    if not value or value.startswith(('\\\\', '//')):
        return None
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', value) and not re.match(r'^[A-Za-z]:[\\/]', value):
        return None
    path = Path(value)
    if not path.is_absolute():
        if not cwd:
            return None
        path = Path(cwd) / path
    return path


def forbidden(path, home, allow_dependencies=False):
    parts = {p.casefold() for p in path.parts}
    name = path.name.casefold()
    blocked_parts = BLOCKED_PARTS - {'node_modules'} if allow_dependencies else BLOCKED_PARTS
    if (parts & blocked_parts or name in BLOCKED_NAMES or name.startswith('.env.')
            or path.suffix.casefold() in BLOCKED_SUFFIXES or name.endswith(('.sqlite-wal', '.sqlite-shm'))):
        return True
    if path.is_relative_to(home) and not any(path.is_relative_to(home / folder) for folder in ('visualizations', 'skills')):
        return True
    return False


def contains_secrets(data, sanitizer, depth=0, budget=None, source_code=False):
    # Check common archive/document XML content too; never alter an Office/binary file.
    if zipfile.is_zipfile(io.BytesIO(data)):
        budget = [FILE_LIMIT] if budget is None else budget
        if depth >= 4:
            return True
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = z.infolist()
                expanded = sum(m.file_size for m in members)
                if len(members) > 10000 or expanded > budget[0]:
                    return True
                budget[0] -= expanded
                for m in members:
                    name = m.filename.replace('\\', '/').split('/')[-1].casefold()
                    if not m.is_dir():
                        parts = {p.casefold() for p in m.filename.replace('\\', '/').split('/')}
                        if name in BLOCKED_NAMES or name.startswith('.env.') or parts & (BLOCKED_PARTS - {'node_modules'}):
                            return True
                        if contains_secrets(z.read(m), sanitizer, depth + 1, budget,
                                            Path(m.filename).suffix.lower() in SOURCE_SUFFIXES):
                            return True
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            return True
    return contains_text_secrets(data, sanitizer, source_code)


def contains_text_secrets(data, sanitizer, source_code=False):
    def detected(text):
        return sanitizer.source_has_secrets(text) if source_code else sanitizer.clean(text) != text
    for encoding in ('utf-8', 'utf-16'):
        try:
            text = data.decode(encoding)
        except UnicodeError:
            continue
        if detected(text):
            return True
    text = data.decode('utf-8', errors='ignore')
    return detected(text)


def portable_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).rstrip(' .')[:180] or 'file'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        name = '_' + name
    return name


def collect_files(records, cwd, home, sanitizer, limit, mode='all'):
    assets, report, payloads, known = [], [], {}, {}
    if mode not in ('all', 'latest'):
        raise ValueError('Attachment mode must be all or latest.')
    refs = list(references(records, cwd))
    if mode == 'latest':
        # Conservative identity: same parent and format, with an explicit trailing
        # v-number / Chinese edition marker removed. Never merge unrelated folders.
        chosen = {}
        def identity(ref):
            p = resolve_reference(*ref)
            if p is None:
                return ref
            stem = re.sub(r'(?i)(?:[-_ .]*v\d+(?:\.\d+)*|[-_ .]*第?\d+(?:\.\d+)*版)$', '', p.stem)
            return os.path.normcase(str(p.parent / (stem + p.suffix)))
        for ref in refs:
            chosen[identity(ref)] = ref
        kept = set(chosen.values())
        for ref in dict.fromkeys(refs):
            if ref not in kept:
                report.append({'reference': ref[0], 'reason': 'superseded_by_later_reference',
                               'selected_reference': chosen[identity(ref)][0]})
        refs = list(chosen.values())
    total = 0
    for reference, reference_cwd in dict.fromkeys(refs):
        try:
            source = resolve_reference(reference, reference_cwd)
            if source is None:
                continue
            path = source.resolve()
            key = os.path.normcase(str(path))
            if key in known:
                if reference not in known[key]['references']:
                    known[key]['references'].append(reference)
                continue
            if forbidden(path, home.resolve()):
                reason = 'sensitive_path'
            elif source.is_symlink():
                reason = 'symlink_not_followed'
            elif not path.is_file():
                reason = 'not_found' if not path.exists() else 'directory_not_copied'
            elif path.stat().st_size > FILE_LIMIT or len(assets) >= 1000:
                reason = 'size_or_count_limit'
            else:
                with path.open('rb') as f:
                    content = f.read(FILE_LIMIT + 1)
                if len(content) > FILE_LIMIT or total + len(content) > limit:
                    reason = 'size_or_count_limit'
                elif contains_secrets(content, sanitizer, source_code=path.suffix.lower() in SOURCE_SUFFIXES):
                    reason = 'possible_credentials'
                else:
                    asset_id = digest(str(path).encode('utf-8'))[:20]
                    archive_path = f'files/{asset_id}/{portable_name(path.name)}'
                    entry = {'id': asset_id, 'source_path': str(path), 'archive_path': archive_path,
                             'sha256': digest(content), 'size': len(content), 'references': [reference]}
                    assets.append(entry)
                    known[key] = entry
                    payloads[archive_path] = content
                    total += len(content)
                    continue
            report.append({'reference': reference, 'reason': reason})
        except OSError:
            report.append({'reference': reference, 'reason': 'unreadable'})
    return assets, report, payloads


def file_inventory(assets, report, project=None):
    result = {'attachment_included': assets, 'project_covered': [], 'excluded': [], 'missing': [],
              'project_files_total': project['files'] if project else 0,
              'project_excluded': project.get('skipped', []) if project else []}
    for row in report:
        category = ('project_covered' if row.get('covered_by_project') else
                    'missing' if row['reason'] in ('not_found', 'unreadable') else 'excluded')
        result[category].append(row)
    return result


def inventory_text(inventory):
    lines = ['文件迁移清单（附件项按引用计数，项目文件详见 project-manifest.json）']
    for key, label in (('attachment_included', '附件已包含'), ('project_covered', '项目已覆盖'),
                       ('excluded', '主动排除或超限'), ('missing', '真正缺失或不可读'),
                       ('project_excluded', '项目内排除项')):
        lines += ['', label + '：' + str(len(inventory[key]))]
        for row in inventory[key]:
            path = row.get('reference', row.get('source_path', row.get('path', '')))
            lines.append('- ' + path + ' [' + ('included_in_project' if key == 'project_covered' else row.get('reason', 'included')) + ']')
    lines += ['', '项目已包含文件总数：' + str(inventory['project_files_total'])]
    return '\n'.join(lines) + '\n'


def publish_file(path, data):
    """Publish complete bytes exclusively; failed writes never create a final file."""
    path = Path(path)
    temporary = path.with_name(path.name + '.part-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == 'nt':
            os.rename(temporary, path)  # Windows rename fails if the destination exists.
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_assets(assets, files):
    if not isinstance(assets, list):
        raise ValueError('Invalid attachment manifest.')
    names, identifiers = set(), set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError('Invalid attachment entry.')
        path, asset_id = asset.get('archive_path', ''), asset.get('id', '')
        if (not isinstance(path, str) or not isinstance(asset_id, str)
                or not re.fullmatch(r'[0-9a-f]{20}', asset_id) or not path.startswith(f'files/{asset_id}/')
                or len(path.split('/')) != 3 or portable_name(path.split('/')[-1]) != path.split('/')[-1]
                or path not in files or path.casefold() in names or asset_id in identifiers
                or not isinstance(asset.get('source_path'), str)
                or not isinstance(asset.get('references'), list)
                or not all(isinstance(r, str) and r for r in asset['references'])):
            raise ValueError('Unsafe or duplicate attachment entry.')
        if len(files[path]) != asset.get('size') or digest(files[path]) != asset.get('sha256'):
            raise ValueError('Attachment size or SHA256 mismatch.')
        names.add(path.casefold())
        identifiers.add(asset_id)
    if {n.casefold() for n in files if n.startswith('files/')} != names:
        raise ValueError('Unlisted attachment payload.')


def restore_files(archive, report, cwd):
    assets = report['manifest'].get('attachments', [])
    result = {'files': [], 'path_map': {}}
    if not assets:
        return result
    archive_bytes = Path(archive).read_bytes()
    if digest(archive_bytes) != report['sha256']:
        raise ValueError('Archive changed before restoring attachment bytes.')
    cwd = Path(cwd).resolve()
    parent = cwd / 'session-files'
    root = parent / report['sha256'][:20]
    for path in (parent, root):
        if path.is_symlink() or (path.exists() and (not path.is_dir() or path.resolve() != path)):
            raise ValueError('Unsafe attachment restoration directory.')
    parent.mkdir(exist_ok=True)
    marker = root / 'SOURCE-SHA256.txt'
    if root.exists():
        if not marker.exists() and not any(root.iterdir()):
            publish_file(marker, (report['sha256'] + '\n').encode('utf-8'))
        if not marker.is_file() or marker.is_symlink() or marker.read_text(encoding='utf-8').strip() != report['sha256']:
            raise ValueError('Attachment directory conflict; existing files were not overwritten.')
    else:
        root.mkdir()
        publish_file(marker, (report['sha256'] + '\n').encode('utf-8'))
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
        for asset in assets:
            folder = root / asset['id']
            if folder.is_symlink() or (folder.exists() and (not folder.is_dir() or folder.resolve() != folder)):
                raise ValueError('Unsafe attachment target directory.')
            folder.mkdir(exist_ok=True)
            target = folder / Path(asset['archive_path']).name
            if target.is_symlink() or (target.exists() and (not target.is_file() or target.resolve() != target)):
                raise ValueError('Unsafe attachment target file.')
            if not target.exists():
                publish_file(target, z.read(asset['archive_path']))
            result['files'].append({'source_path': asset['source_path'], 'path': str(target), 'sha256_at_export': asset['sha256']})
            aliases = [asset['source_path'], asset['source_path'].replace('\\', '/'), *asset['references']]
            for alias in aliases:
                # Keep numeric line/page suffixes when rebinding links.
                base = re.sub(r'(?::\d+(?::\d+)?|#(?:L|page=)?\d+(?:-L?\d+)?)$', '', alias)
                result['path_map'][base] = target.as_posix()
    return result


def relink_records(data, path_map):
    if not path_map:
        return data
    absolute_map = {}
    absolute_aliases = []
    for alias, target in path_map.items():
        resolved = resolve_reference(alias, None)
        if resolved is not None and resolved.is_absolute():
            absolute_map[os.path.normcase(os.path.normpath(str(resolved)))] = target
            absolute_aliases.append(alias)
    pattern = None
    if absolute_aliases:
        choices = [''.join(r'[\\/]' if c in '\\/' else re.escape(c) for c in alias)
                   for alias in sorted(absolute_aliases, key=len, reverse=True)]
        pattern = re.compile(r'(?<![\w/\\])(?:' + '|'.join(choices) + r')(?![\w.\/\\-])', re.I)
    current_cwd = None
    def remap(value):
        base = re.sub(r'(?::\d+(?::\d+)?|#(?:L|page=)?\d+(?:-L?\d+)?)$', '', value)
        resolved = resolve_reference(base, current_cwd)
        if resolved is not None:
            absolute = os.path.normcase(os.path.normpath(str(resolved)))
            if absolute in absolute_map:
                return absolute_map[absolute] + value[len(base):]
        if value in path_map:
            return path_map[value]
        match = re.search(r'(?::\d+(?::\d+)?|#(?:L|page=)?\d+(?:-L?\d+)?)$', value)
        if match and value[:match.start()] in path_map:
            return path_map[value[:match.start()]] + match[0]
        return value

    def remap_text(text):
        edits, spans = [], []
        for start, end, left, right in markdown_targets(text):
            spans.append((start, end))
            target = text[left:right]
            if remap(target) != target:
                replacement = remap(target)
                if any(c.isspace() for c in replacement) and text[left - 1:left] != '<':
                    replacement = '<' + replacement + '>'
                edits.append((left, right, replacement))
        spans.extend((match.start(), match.end()) for match in re.finditer(r'https?://[^\s<>`]+', text))
        for match in (pattern.finditer(text) if pattern else []):
            if not any(start <= match.start() < end for start, end in spans):
                target = match[0]
                if remap(target) != target:
                    edits.append((match.start(), match.start() + len(target), remap(target)))
        for left, right, replacement in sorted(edits, reverse=True):
            text = text[:left] + replacement + text[right:]
        return text
    records = []
    for line in data.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('type') in ('session_meta', 'turn_context'):
            current_cwd = row.get('payload', {}).get('cwd') or current_cwd
        for container, key in message_fields(row):
            container[key] = remap(container[key]) if key in ('image_url', 'file_path') else remap_text(container[key])
        records.append((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8'))
    return b''.join(records)
