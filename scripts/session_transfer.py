"""Read-only Codex session export, verification and import staging. Python 3.11+."""
import argparse
import collections
import datetime as dt
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tomllib
import uuid
import zipfile

FORMAT = 'codex-session-transfer-v2'
LEGACY_FORMAT = 'codex-session-transfer-v1'
LIMIT = 256 * 1024 * 1024
SECRET_FIELD = r'(?:[a-z0-9_-]*(?:password|passwd|pwd|passphrase|api[_-]?key|token|secret)|auth|authorization|cookie|set-cookie)'
SENSITIVE = re.compile(r'^(?:creator_user_id|creator_account_id|' + SECRET_FIELD + r')$', re.I)
PATTERNS = [
    re.compile(r'\b(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{16,}'),
    re.compile(r'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b'),
    re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}'),
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.S),
    # URI userinfo with a nonempty password, including database/registry URLs.
    # Braces exclude source-code template expressions such as ${user}:${password}.
    re.compile(r'''(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,63}://[^\s\x00-\x1f/@:<>'"`{}]+:[^\s\x00-\x1f/@<>'"`{}]+@'''),
]
ASSIGNMENT = re.compile(r'''(?i)(\b''' + SECRET_FIELD + r'''\b["']?\s*[:=]\s*["']?)([^\s"'`;,{ }\\]+)''')

file_spec = importlib.util.spec_from_file_location('session_files', Path(__file__).with_name('session_files.py'))
session_files = importlib.util.module_from_spec(file_spec)
file_spec.loader.exec_module(session_files)
storage_spec = importlib.util.spec_from_file_location('session_storage', Path(__file__).with_name('session_storage.py'))
session_storage = importlib.util.module_from_spec(storage_spec)
storage_spec.loader.exec_module(session_storage)
project_spec = importlib.util.spec_from_file_location('project_transfer', Path(__file__).with_name('project_transfer.py'))
project_transfer = importlib.util.module_from_spec(project_spec)
project_spec.loader.exec_module(project_transfer)
bundle_spec = importlib.util.spec_from_file_location('single_bundle', Path(__file__).with_name('single_bundle.py'))
single_bundle = importlib.util.module_from_spec(bundle_spec)
bundle_spec.loader.exec_module(single_bundle)
resolve_archive = single_bundle.resolve


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()


def use_environment_sqlite(home):
    configured = os.environ.get('CODEX_SQLITE_HOME')
    home = Path(home).resolve()
    return (not configured or home == codex_home()
            or Path(configured).expanduser().resolve().is_relative_to(home))


def candidates(home):
    result = []
    for folder in ('sessions', 'archived_sessions'):
        for path in (home / folder).rglob('*.jsonl*'):
            if not (path.name.endswith('.jsonl') or path.name.endswith('.jsonl.zst')):
                continue
            try:
                first = json.loads(session_storage.first_line(path))
                payload = first.get('payload', {})
                if first.get('type') == 'session_meta' and payload.get('id'):
                    result.append((path, payload))
            except (OSError, ValueError, AttributeError):
                if path.suffix == '.zst':
                    raise ValueError('Cannot inspect compressed history segment: ' + str(path) +
                                     '. Check the Zstandard reader; refusing to silently skip history.')
                continue
    return result


def find_session(home, thread_id):
    if not thread_id:
        raise ValueError('Current thread ID is unavailable. Run discover and ask the user to select an ID.')
    matches = [(p, m) for p, m in candidates(home) if m['id'] == thread_id]
    if len(matches) > 1:
        # Rotated segments retain the thread ID. Select the unique unreferenced tip,
        # using validated prefix boundaries, never modification time or filename order.
        parents = set()
        for path, meta in matches:
            base = meta.get('history_base')
            if isinstance(base, dict) and base.get('thread_id') == thread_id:
                parent, _ = parent_prefix(home, path, base, matches)
                parents.add(parent)
        matches = [(p, m) for p, m in matches if p not in parents]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one session_meta.id match; found {len(matches)}. No newest-file fallback.')
    return matches[0]


def parent_prefix(home, source, base, available=None):
    boundary = base.get('end_byte_offset')
    ordinal = base.get('end_ordinal_exclusive')
    if type(boundary) is not int or not 1 <= boundary <= LIMIT:
        raise ValueError('Unsupported history boundary; exact byte offset is required.')
    matches = []
    for path, meta in (candidates(home) if available is None else available):
        if meta['id'] != base['thread_id'] or path.resolve() == source.resolve():
            continue
        raw = session_storage.read_session(path, boundary)
        if len(raw) != boundary or not raw.endswith(b'\n'):
            continue
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not rows or rows[0].get('type') != 'session_meta':
            continue
        if ordinal is not None and any('ordinal' in r for r in rows):
            if type(ordinal) is not int or rows[-1].get('ordinal') != ordinal - 1:
                continue
            ordinals = [r.get('ordinal') for r in rows]
            if any(type(n) is not int for n in ordinals) or any(b != a + 1 for a, b in zip(ordinals, ordinals[1:])):
                continue
        if base['thread_id'] == json.loads(session_storage.first_line(source))['payload']['id']:
            child = json.loads(session_storage.first_line(source))
            if type(ordinal) is not int or child.get('ordinal') != ordinal or rows[0].get('ordinal', ordinal) >= ordinal:
                continue
        matches.append((path, raw))
    if len(matches) != 1:
        raise ValueError(f'History prefix boundary has {len(matches)} valid parent segments; '
                         'compressed segments were included. Refusing incomplete or ambiguous export.')
    return matches[0]


def referenced_export_targets(records):
    """Inspect the latest human request; a chat reference is not inherited history."""
    for row in reversed(records):
        payload = row.get('payload', {})
        if row.get('type') != 'response_item' or payload.get('role') != 'user':
            continue
        text = ''.join(p.get('text', '') for p in payload.get('content', []) if isinstance(p, dict)).lstrip()
        if text.startswith(('<environment_context>', '<external_codex_apps_open_page>', '<skill>')):
            continue
        if text.startswith('## Referenced chats with Codex:') and '## My request:' in text:
            request = text.split('## My request:', 1)[1]
            return list(dict.fromkeys(re.findall(r'thread://([0-9a-fA-F-]{36})', request)))
        return []
    return []


def materialized_history(home, source, raw, seen=None, captures=None):
    """Flatten the observed paginated history_base contract without changing source files."""
    seen, captures = set() if seen is None else seen, [] if captures is None else captures
    source = source.resolve()
    if source in seen or len(seen) >= 64:
        raise ValueError('Cyclic or excessive parent history dependencies.')
    seen.add(source)
    end = raw.rfind(b'\n') + 1
    snapshot = raw[:end]
    records = [json.loads(line) for line in snapshot.splitlines() if line.strip()]
    if not records or records[0].get('type') != 'session_meta':
        raise ValueError('History dependency has no valid session metadata.')
    captures.append((source, snapshot))
    base = records[0]['payload'].get('history_base')
    if base:
        if not isinstance(base, dict) or not base.get('thread_id'):
            raise ValueError('Unrecognized parent history reference; refusing incomplete export.')
        parent, parent_raw = parent_prefix(home, source, base)
        prior, captures = materialized_history(home, parent, parent_raw, seen, captures)
        records = [records[0]] + prior[1:] + records[1:]
        records[0]['payload'].pop('history_base', None)
        records[0]['payload'].pop('forked_from_ordinal_exclusive', None)
    if sum(len(data) for _, data in captures) > LIMIT:
        raise ValueError('Combined history exceeds 256 MiB.')
    return records, captures


class Sanitizer:
    def signature_has_secrets(self, text):
        return any(p.search(text) for p in PATTERNS) or any(v in text for v in self.identities)

    def source_has_secrets(self, text):
        # Source expressions are not credential values. Retain concrete signatures
        # and quoted credential literals without rewriting any source bytes.
        if self.signature_has_secrets(text):
            return True
        return bool(re.search(r'''(?i)\b''' + SECRET_FIELD + r'''\b["']?\s*[:=]\s*(?:(["'])([^"'\r\n]+)\1|[+-]?\d+(?:\.\d+)?\b)''', text))

    def __init__(self, values):
        self.identities = set()
        self.counts = collections.Counter()
        self.collect(values)

    def collect(self, value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ('creator_user_id', 'creator_account_id') and isinstance(v, str) and v:
                    self.identities.add(v)
                self.collect(v)
        elif isinstance(value, list):
            for v in value:
                self.collect(v)
        elif isinstance(value, str) and value.lstrip().startswith(('{', '[')):
            try:
                self.collect(json.loads(value))
            except ValueError:
                pass

    def clean(self, value):
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                if SENSITIVE.fullmatch(k):
                    self.counts['removed_fields'] += 1
                elif (k == 'encrypted_content' and value.get('type') in ('reasoning', 'compaction')
                      and isinstance(v, str)):
                    # Opaque authenticated ciphertext can resemble credentials by chance.
                    result[k] = v
                else:
                    result[k] = self.clean(v)
            return result
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value
        text = value
        if text.lstrip().startswith(('{', '[')):
            try:
                parsed = json.loads(text)
            except ValueError:
                pass
            else:
                cleaned = self.clean(parsed)
                # Each nested string was already scanned; rescanning JSON corrupts ciphertext.
                return json.dumps(cleaned, ensure_ascii=False) if parsed != cleaned else text
        for identity in self.identities:
            if identity in text:
                self.counts['identity_replacements'] += text.count(identity)
                text = text.replace(identity, '[REDACTED_IDENTITY]')
        for pattern in PATTERNS:
            text, n = pattern.subn('[REDACTED_SECRET]', text)
            self.counts['credential_replacements'] += n
        text, n = ASSIGNMENT.subn(lambda m: m[1] + '[REDACTED_SECRET]', text)
        self.counts['credential_replacements'] += n
        return text


def metadata(home, thread_id):
    values, inventory = {}, []
    index = home / 'session_index.jsonl'
    if index.is_file():
        rows = []
        for line in index.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get('id') == thread_id:
                rows.append({k: row[k] for k in ('id', 'thread_name', 'updated_at') if k in row})
        values['session-index'] = rows
    roots = project_transfer.private_data_roots(home, include_env=use_environment_sqlite(home))
    databases = set()
    for root in roots:
        databases.update(root.glob('state_*.sqlite'))
    for db in sorted(databases):
        con = None
        try:
            con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
            con.execute('PRAGMA query_only=ON')
            cols = {r[1] for r in con.execute('PRAGMA table_info(threads)')}
            wanted = [c for c in ('id', 'rollout_path', 'created_at', 'updated_at', 'cwd', 'title',
                                 'archived', 'cli_version', 'source', 'originator', 'model', 'history_mode') if c in cols]
            if 'id' not in wanted:
                inventory.append({'path': str(db), 'status': 'unrecognized threads schema; not exported'})
                continue
            rows = [dict(zip(wanted, r)) for r in con.execute('SELECT ' + ','.join(wanted) + ' FROM threads WHERE id=?', (thread_id,))]
            values[db.stem + '-thread'] = rows
            inventory.append({'path': str(db), 'table': 'threads', 'rows': len(rows), 'columns': wanted})
        except sqlite3.Error as exc:
            inventory.append({'path': str(db), 'status': 'unavailable_optional_index', 'error': str(exc)})
        finally:
            if con is not None:
                con.close()
    # These are observed secondary indexes/caches, not required raw databases.
    for path in [home / 'thread_history_1.sqlite', home / 'sqlite' / 'codex-dev.db', home / '.codex-global-state.json']:
        if path.exists():
            inventory.append({'path': str(path), 'status': 'observed; not copied (cache/global or unrelated state)'})
    return values, inventory


def export_session(home, thread_id, output, project_root=None, single_file=True, attachment_mode='all'):
    home, output = Path(home).resolve(), Path(output).resolve()
    if project_root is not None:
        project_root = Path(project_root).resolve()
        if output.is_relative_to(project_root):
            output = project_root.parent / (project_root.name + '-session-exports')
    source, meta = find_session(home, thread_id)
    raw = session_storage.read_session(source)
    if len(raw) > LIMIT:
        raise ValueError('Session exceeds 256 MiB; no partial archive produced.')
    end = raw.rfind(b'\n') + 1
    snapshot = raw[:end]
    records, captures = materialized_history(home, source, snapshot)
    if any('ordinal' in row for row in records):
        for ordinal, row in enumerate(records):
            row['ordinal'] = ordinal
    if not records or records[0].get('payload', {}).get('id') != thread_id:
        raise ValueError('Snapshot identity mismatch or no complete JSONL records.')
    extras, inventory = metadata(home, thread_id)
    sanitizer = Sanitizer([records, extras])
    archive = output / f'codex-session-{thread_id}-{uuid.uuid4().hex[:8]}.zip'
    attachments, file_report, file_payloads = session_files.collect_files(records, meta.get('cwd'), home, sanitizer, LIMIT, mode=attachment_mode)
    record_path = 'sessions/' + source.relative_to(home).as_posix().removesuffix('.zst')
    files = {record_path: b''.join((json.dumps(sanitizer.clean(r), ensure_ascii=False) + '\n').encode('utf-8') for r in records)}
    for name, value in extras.items():
        files['metadata/' + name + '.json'] = encoded(sanitizer.clean(value))
    files.update(file_payloads)
    manifest = {
        'format': FORMAT, 'thread_id': thread_id, 'exported_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'source_path': str(source), 'codex_home': str(home), 'cwd': meta.get('cwd'), 'record_path': record_path,
        'identification': 'Explicit/environment thread ID equals unique session_meta.id; never inferred from mtime',
        'versions': {'cli': meta.get('cli_version'), 'cli_source': 'session_meta.cli_version (recording version)',
                     'desktop': None, 'desktop_note': 'Not independently established; not guessed', 'os': platform.platform()},
        'snapshot': {'record_count': len(records), 'bytes_captured': end, 'discarded_tail_bytes': len(raw) - end,
                     'source_prefix_sha256': sha(snapshot), 'last_record_at': records[-1].get('timestamp'),
                     'lineage_sources': [{'path': str(p), 'bytes': len(b), 'sha256': sha(b),
                        'encoding': 'zstd' if p.suffix == '.zst' else 'plain',
                        'checksum_scope': 'decoded JSONL prefix'} for p, b in captures]},
        'metadata_sources': inventory, 'sanitization': {'copy_modified': True, 'counts': dict(sanitizer.counts),
            'note': 'Heuristic credential and creator identity redaction; original paths deliberately retained. Review before sharing.'},
        'attachments': attachments, 'file_report': file_report,
        'file_snapshot': 'Current on-disk versions of resolvable chat-linked files, not historical versions. No whole-project copy.',
        'excluded': ['auth.json', 'config.toml', 'credentials', 'full databases and WAL/SHM', 'other threads', 'unreferenced project files'],
        'limitations': ['Active snapshot excludes later messages; not a byte-identical raw export after sanitization.',
                       'Referenced ChatGPT history is only what appears in this local thread; not separately fetched.',
                       'Uploading to ChatGPT does not create or restore a native Codex thread.',
                       'Native import requires a separately controlled, version-specific procedure.'],
    }
    if project_root is not None:
        manifest['project_bundle'] = project_transfer.export_project(
            project_root, archive.with_suffix('.project.zip'), sanitizer, session_files, home=home)
    project = manifest.get('project_bundle')
    covered = set()
    if project:
        with zipfile.ZipFile(archive.with_suffix('.project.zip')) as z:
            pm = json.loads(z.read('project-manifest.json'))
        covered = {os.path.normcase(str(project_root / e['path'])) for e in pm['files']}
    for item in file_report:
        reference = re.sub(r'^/([A-Za-z]:/)', r'\1', item['reference'])
        p = session_files.resolve_reference(reference, meta.get('cwd'))
        if p is not None and os.path.normcase(str(p)) in covered and item['reason'] != 'superseded_by_later_reference':
            item['covered_by_project'] = True
    unresolved = [i for i in file_report if not i.get('covered_by_project') and i['reason'] != 'superseded_by_later_reference']
    inventory_report = session_files.file_inventory(attachments, file_report, project)
    manifest['file_inventory'] = inventory_report
    completeness = {
        'file_inventory': inventory_report,
        'history': {'status': 'verified', 'records': len(records), 'lineage_segments': len(captures),
                    'scope': 'complete captured lineage, not future messages or cloud history'},
        'attachments': {'mode': attachment_mode, 'included': len(attachments),
            'status': 'has_skipped_items' if unresolved else 'selected_references_resolved',
            'skipped': unresolved, 'project_covered_references': sum(bool(i.get('covered_by_project')) for i in file_report),
            'superseded': [i for i in file_report if i['reason'] == 'superseded_by_later_reference'],
            'latest_rule': 'last reference per parent+format+explicit version-normalized stem; ambiguous names kept'},
        'project': {'status': 'included' if project else 'not_requested',
                    'files': project['files'] if project else 0, 'skipped': project['skipped'] if project else []},
        'runtime': project['critical_checks'] if project else {'status': 'not_checked', 'launch_test': 'not_performed'},
        'limitations': ['Heuristic secret filtering is not an absolute guarantee.',
                        'Current on-disk files only; application launch and full dependency graph are not verified.']}
    manifest['completeness'] = completeness
    files['COMPLETENESS-REPORT.json'] = encoded(sanitizer.clean(completeness))
    files['COMPLETENESS-REPORT.txt'] = ('迁移完整性报告\n历史：已验证，共 ' + str(len(records)) + ' 条记录\n'
        + '附件选择：' + attachment_mode + '（all=全部，latest=每项成果最后引用版本）\n'
        + '已包含附件：' + str(len(attachments)) + '\n'
        + '未解决或排除的附件引用：' + str(len(unresolved)) + '\n'
        + '因版本筛选省略的旧附件引用：' + str(len(completeness['attachments']['superseded'])) + '\n'
        + '项目文件：' + str(completeness['project']['files']) + '\n'
        + '关键运行文件检查：' + completeness['runtime']['status'] + '\n'
        + '目标电脑启动及完整依赖图：未验证，不能仅凭校验通过保证可运行。\n'
        + '具体缺失、排除和版本筛选明细见 COMPLETENESS-REPORT.json。\n').encode('utf-8')
    files['manifest.json'] = encoded(sanitizer.clean(manifest))
    file_text = session_files.inventory_text(inventory_report)
    files['FILE-REPORT.txt'] = sanitizer.clean(file_text).encode('utf-8')
    files['COMPLETENESS-REPORT.txt'] += ('\n' + sanitizer.clean(file_text)).encode('utf-8')
    files['SHA256SUMS.json'] = encoded({n: sha(data) for n, data in files.items()})
    if sum(map(len, files.values())) > LIMIT or len(files) > 2000:
        raise ValueError('Combined session and linked files exceed the archive limit; nothing exported.')
    for captured_path, captured in captures:
        if session_storage.read_session(captured_path, len(captured)) != captured:
            raise ValueError('Source prefix changed during capture. Retry; original was not modified.')
    output.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    with archive.open('xb') as f:
        f.write(buffer.getvalue())
    verify_archive(archive)
    archive.with_suffix('.zip.sha256').write_text(sha(buffer.getvalue()) + '  ' + archive.name + '\n', encoding='utf-8')
    if project_root is not None and single_file:
        project = archive.with_suffix('.project.zip')
        bundle = single_bundle.pack(archive, project, archive.with_name(archive.stem + '-complete.zip'))
        verify_archive(bundle)
        for path in (archive, project, archive.with_suffix('.zip.sha256'), project.with_suffix('.zip.sha256')):
            path.unlink()
        return bundle
    return archive


def verify_archive(archive):
    archive = resolve_archive(archive)
    if archive.stat().st_size > LIMIT:
        raise ValueError('Archive exceeds 256 MiB.')
    data = archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries = z.infolist()
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or len(names) != len({n.casefold() for n in names}) or len(names) > 2000:
            raise ValueError('Duplicate entries or too many ZIP members.')
        if sum(e.file_size for e in entries) > LIMIT:
            raise ValueError('Expanded archive exceeds 256 MiB.')
        for entry in entries:
            p = PurePosixPath(entry.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename
                    or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)
                    or str(p) != entry.filename
                    or any(session_files.portable_name(part) != part for part in p.parts)):
                raise ValueError('Unsafe archive member path.')
        try:
            files = {n: z.read(n) for n in names}
        except (RuntimeError, NotImplementedError) as exc:
            raise ValueError('Encrypted or unsupported ZIP members cannot be imported.') from exc
    try:
        checks = json.loads(files['SHA256SUMS.json'])
        if not isinstance(checks, dict) or not all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in checks.values()):
            raise ValueError('Invalid SHA256 checksum list.')
        if set(files) != set(checks) | {'SHA256SUMS.json'}:
            raise ValueError('Unlisted or missing archive entries.')
        for name, digest in checks.items():
            if sha(files[name]) != digest:
                raise ValueError('SHA256 mismatch: ' + name)
        manifest = json.loads(files['manifest.json'])
        if not isinstance(manifest, dict):
            raise ValueError('Manifest must be a JSON object.')
        if manifest['format'] not in (FORMAT, LEGACY_FORMAT):
            raise ValueError('Unsupported archive format; do not guess or overwrite live state.')
        if (not isinstance(manifest.get('thread_id'), str) or not isinstance(manifest.get('snapshot'), dict)
                or type(manifest['snapshot'].get('record_count')) is not int
                or manifest['snapshot']['record_count'] < 1
                or not isinstance(manifest.get('record_path'), str)
                or not manifest['record_path'].startswith('sessions/')):
            raise ValueError('Invalid session metadata in manifest.')
        uuid.UUID(manifest['thread_id'])
        session_files.validate_assets(manifest.get('attachments', []), files)
        lines = [json.loads(line) for line in files[manifest['record_path']].splitlines() if line.strip()]
        if not all(isinstance(line, dict) and isinstance(line.get('type'), str) for line in lines):
            raise ValueError('Session JSONL records must be objects with a type.')
        if (not lines or lines[0].get('type') != 'session_meta' or not isinstance(lines[0].get('payload'), dict)
                or lines[0]['payload']['id'] != manifest['thread_id']
                or len(lines) != manifest['snapshot']['record_count']):
            raise ValueError('Session identity or record count mismatch.')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Malformed session archive.') from exc
    sidecar = archive.with_suffix('.zip.sha256')
    if sidecar.exists():
        tokens = sidecar.read_text(encoding='utf-8-sig').split()
        if not tokens or not re.fullmatch(r'[0-9a-fA-F]{64}', tokens[0]) or tokens[0].lower() != sha(data):
            raise ValueError('Whole archive SHA256 is missing, invalid, or mismatched.')
    if 'project_bundle' in manifest:
        project_transfer.companion_path(archive, manifest['project_bundle'], session_files)
    return {'thread_id': manifest['thread_id'], 'records': len(lines), 'entries': len(files),
            'sha256': sha(data), 'verified': True, 'manifest': manifest}


def restore_session_files(archive, report, cwd):
    archive = resolve_archive(archive)
    verified = verify_archive(archive)
    if verified['sha256'] != report['sha256'] or verified['manifest'] != report['manifest']:
        raise ValueError('Archive or manifest changed before file restoration.')
    project = project_transfer.restore_project(archive, verified['manifest'].get('project_bundle'), cwd, session_files)
    result = session_files.restore_files(archive, verified, project['root'] if project else cwd)
    if project:
        result['path_map'].update(project['path_map'])
        for item in result['files']:
            if item['source_path'] in project['path_map']:
                item['path'] = project['path_map'][item['source_path']]
        result['project'] = {k: v for k, v in project.items() if k != 'path_map'}
    return result


def relink_session(data, path_map):
    return session_files.relink_records(data, path_map)


def environment_report(home, executable=None):
    """Check local prerequisites without touching accounts, sessions, or model inference."""
    python = {'path': sys.executable, 'version': platform.python_version(), 'ready': sys.version_info >= (3, 11)}
    chosen = Path(executable) if executable else None
    if chosen is None:
        found = shutil.which('codex')
        if found:
            chosen = Path(found)
        else:
            root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local') / 'OpenAI/Codex/bin'
            try:
                choices = list(root.rglob('codex.exe'))
                chosen = max(choices, key=lambda p: p.stat().st_mtime) if choices else None
            except OSError:
                chosen = None
    codex = {'path': str(chosen) if chosen else None, 'ready': False}
    if chosen is not None and chosen.is_file():
        try:
            version = subprocess.run([str(chosen), '--version'], capture_output=True, text=True,
                                     timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            codex['version'] = version.stdout.strip()
            codex['ready'] = version.returncode == 0 and codex['version'].lower().startswith('codex')
            if not codex['ready']:
                codex['message'] = 'The Codex version check failed. Open or repair the installed Codex app.'
        except (OSError, subprocess.TimeoutExpired) as exc:
            codex['message'] = 'Codex could not be started: ' + str(exc)
    else:
        codex['message'] = 'Codex engine not found. Install/open Codex or provide --codex with its executable path.'
    return {'skill_version': '1.0.3', 'python': python, 'codex': codex, 'codex_home': str(Path(home).resolve()),
            'runtime_ready': python['ready'] and codex['ready'],
            'account_check': 'Not checked. The destination account must already be able to use Codex.',
            'compatibility_check': 'Runtime checks do not replace the isolated native-import rehearsal.'}


def stage_import(archive, destination):
    archive = resolve_archive(archive)
    report = verify_archive(archive)
    data = Path(archive).read_bytes()
    if sha(data) != report['sha256']:
        raise ValueError('Archive changed after verification.')
    destination = Path(destination).resolve()
    if '.codex' in (part.casefold() for part in destination.parts) or destination.is_relative_to(codex_home()):
        raise ValueError('Staging inside live Codex home is forbidden.')
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as f:
                f.write(z.read(name))
    (destination / 'IMPORT-NOT-APPLIED.txt').write_text(
        'Verified staging only. No native session or index has been installed.\n'
        'Before native import: inspect target Codex version/schema; compare existing IDs; back up affected state; '
        'resolve paths; use a supported method; validate visibility, history, continuation and workspace.\n'
        'Same ID: identical content may be skipped; differing content must stop without overwrite or automatic renaming.\n', encoding='utf-8')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=codex_home())
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('discover')
    doctor = subs.add_parser('doctor', help='Read-only local runtime and optional archive diagnostics.')
    doctor.add_argument('--codex', type=Path)
    doctor.add_argument('--archive', type=Path)
    export = subs.add_parser('export')
    export.add_argument('--thread-id')
    export.add_argument('--attachments', choices=('all', 'latest'), default='all')
    export.add_argument('--project', type=Path, help='Explicit project root: export a bound companion ZIP with its runtime and editable tree.')
    export.add_argument('--output', type=Path, default=Path.cwd() / 'session-exports')
    verify = subs.add_parser('verify')
    verify.add_argument('archive', type=Path)
    stage = subs.add_parser('stage-import')
    stage.add_argument('archive', type=Path)
    stage.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'doctor':
            result = environment_report(args.home, args.codex)
            if args.archive:
                report = verify_archive(args.archive)
                manifest = report['manifest']
                result['archive'] = {'verified': True, 'thread_id': report['thread_id'], 'records': report['records'],
                                     'files_included': len(manifest.get('attachments', [])),
                                     'files_not_included': [i for i in manifest.get('file_report', []) if not i.get('covered_by_project')],
                                     'file_inventory': manifest.get('file_inventory'),
                                     'project_bundle': manifest.get('project_bundle'),
                                     'completeness': manifest.get('completeness'),
                                     'recording_versions': manifest.get('versions', {})}
        elif args.command == 'discover':
            rows = sorted(candidates(args.home), key=lambda pair: pair[1].get('timestamp', ''), reverse=True)
            result = {'environment_thread_id': os.environ.get('CODEX_THREAD_ID'), 'recent': [
                {'id': m['id'], 'started_at': m.get('timestamp'), 'cli_version': m.get('cli_version')} for _, m in rows[:10]]}
        elif args.command == 'export':
            if not args.thread_id:
                thread_id, session_id = os.environ.get('CODEX_THREAD_ID'), os.environ.get('CODEX_SESSION_ID')
                if thread_id and session_id and thread_id != session_id:
                    raise ValueError('Conflicting environment thread/session IDs; discover and select an explicit ID.')
                args.thread_id = thread_id or session_id
                if args.thread_id:
                    source, _ = find_session(args.home, args.thread_id)
                    rows = [json.loads(line) for line in session_storage.read_session(source).splitlines() if line.strip()]
                    targets = referenced_export_targets(rows)
                    if targets:
                        raise ValueError('This request references another chat; the current thread does not include its history. '
                                         'Select the intended source with --thread-id: ' + ', '.join(targets))
            path = export_session(args.home, args.thread_id, args.output, project_root=args.project, attachment_mode=args.attachments)
            manifest = verify_archive(path)['manifest']
            result = {'zip_path': str(path), 'sha256_path': str(path.with_suffix('.zip.sha256')), 'verified': True,
                      'files_included': len(manifest['attachments']),
                      'files_not_included': [i for i in manifest['file_report'] if not i.get('covered_by_project')],
                      'file_inventory': manifest.get('file_inventory'),
                      'project_bundle': manifest.get('project_bundle')}
            result['completeness'] = manifest.get('completeness')
        elif args.command == 'verify':
            result = verify_archive(args.archive)
        else:
            result = {'staging_path': str(stage_import(args.archive, args.destination)), 'native_import_applied': False}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        if args.command == 'doctor' and not result['runtime_ready']:
            return 2
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
