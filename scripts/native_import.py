"""Restore an exported snapshot as a new native Codex thread, without sending a chat message."""
import argparse
import collections
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile

spec = importlib.util.spec_from_file_location('session_transfer', Path(__file__).with_name('session_transfer.py'))
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


class AppServer:
    """Use the installed engine's protocol; never edit its database schema."""
    def __init__(self, executable, home, isolated=False):
        env = os.environ.copy()
        env['CODEX_HOME'] = str(home)
        if isolated:
            env['CODEX_SQLITE_HOME'] = str(home)
        self.process = subprocess.Popen(
            [str(executable), 'app-server', '--listen', 'stdio://'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.messages = queue.Queue()
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.info = self.call('initialize', {'clientInfo': {'name': 'codex_session_transfer',
                'title': 'Codex Session Transfer', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=True) + '\n')
        self.process.stdin.flush()

    def call(self, method, params):
        # Import is a history operation, never an inference or message operation.
        allowed = {'initialize', 'thread/list', 'thread/fork', 'thread/read', 'thread/resume',
                   'thread/turns/list', 'thread/items/list', 'thread/name/set', 'thread/archive', 'thread/goal/get'}
        if method not in allowed:
            raise ValueError('Method is forbidden during import: ' + method)
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + 45
        while True:
            try:
                item = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise ValueError('App-server timed out; no chat message was sent.') from exc
            if item is None:
                raise ValueError('App-server closed before answering ' + method)
            if 'id' in item and 'method' in item:
                self.send({'id': item['id'], 'error': {'code': -32601,
                    'message': 'Interactive requests and tool execution are not supported during session import.'}})
                raise ValueError('Unexpected interactive server request; import stopped without prompting.')
            if item.get('id') == request_id:
                if 'error' in item:
                    raise ValueError(method + ': ' + str(item['error'].get('message', 'RPC error')))
                return item['result']
            if time.monotonic() >= deadline:
                raise ValueError('App-server timed out.')

    def close(self):
        with contextlib.suppress(OSError):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def visible_messages(data):
    messages = []
    for line in data.splitlines():
        row = json.loads(line)
        payload = row.get('payload', {})
        if row.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') in ('user', 'assistant'):
            messages.append(json.dumps([payload['role'], payload.get('content')], sort_keys=True, ensure_ascii=True))
    return collections.Counter(messages)


def turn_count(server, thread_id):
    total, cursor = 0, None
    for _ in range(1000):
        params = {'threadId': thread_id, 'limit': 100}
        if cursor:
            params['cursor'] = cursor
        page = server.call('thread/turns/list', params)
        total += len(page['data'])
        following = page.get('nextCursor')
        if not following:
            return total
        if following == cursor:
            raise ValueError('Repeated pagination cursor.')
        cursor = following
    raise ValueError('Too many history pages.')


def listed_thread(server, thread_id):
    cursor, seen = None, set()
    for _ in range(1000):
        params = {'limit': 100, 'sortKey': 'created_at'}
        if cursor:
            params['cursor'] = cursor
        page = server.call('thread/list', params)
        if any(thread['id'] == thread_id for thread in page['data']):
            return True
        cursor = page.get('nextCursor')
        if not cursor:
            return False
        if cursor in seen:
            raise ValueError('Repeated thread listing cursor.')
        seen.add(cursor)
    raise ValueError('Too many native session listing pages.')


def history_signature(server, thread_id):
    items, cursor = [], None
    for _ in range(1000):
        params = {'threadId': thread_id, 'limit': 100, 'sortDirection': 'asc'}
        if cursor:
            params['cursor'] = cursor
        page = server.call('thread/items/list', params)
        for entry in page['data']:
            item = dict(entry['item'])
            for key in ('id', 'clientId', 'status', 'durationMs'):
                item.pop(key, None)
            items.append(item)
        following = page.get('nextCursor')
        if not following:
            return transfer.sha(transfer.encoded(items))
        if following == cursor:
            raise ValueError('Repeated item pagination cursor.')
        cursor = following
    raise ValueError('Too many item history pages.')


def resume_without_goal(server, thread_id, **overrides):
    if server.call('thread/goal/get', {'threadId': thread_id}).get('goal') is not None:
        raise ValueError('Automatic goal state exists; refusing to resume it during import.')
    return server.call('thread/resume', {'threadId': thread_id, 'excludeTurns': True, **overrides})['thread']


def save_receipt(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(transfer.encoded(value))
    temporary.replace(path)


def backup_indexes(home, destination):
    destination.mkdir(parents=True, exist_ok=False)
    roots = transfer.project_transfer.private_data_roots(home, include_env=transfer.use_environment_sqlite(home))
    for i, root in enumerate(sorted(roots)):
        for database in root.glob('state_*.sqlite'):
            source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
            target = sqlite3.connect(destination / f'{i}-{database.name}')
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
    index = home / 'session_index.jsonl'
    if index.exists():
        (destination / 'session_index.jsonl').write_bytes(index.read_bytes())
    save_receipt(destination / 'BACKUP-COMPLETE.json', {
        path.name: transfer.sha(path.read_bytes()) for path in destination.iterdir() if path.is_file()})


def backup_complete(destination):
    try:
        checks = json.loads((destination / 'BACKUP-COMPLETE.json').read_text(encoding='utf-8'))
        return (isinstance(checks, dict)
                and set(checks) == {path.name for path in destination.iterdir()
                                    if path.is_file() and path.name != 'BACKUP-COMPLETE.json'}
                and all(Path(name).name == name
                        and transfer.sha((destination / name).read_bytes()) == digest
                        for name, digest in checks.items()))
    except (OSError, ValueError, TypeError):
        return False


def restore_native(archive, home, cwd, output, executable=None):
    archive = transfer.resolve_archive(archive)
    executable = Path(executable or shutil.which('codex') or '')
    if not executable.is_file():
        raise FileNotFoundError('Codex executable not found. Supply --codex with the installed CLI path.')
    report = transfer.verify_archive(archive)
    manifest = report['manifest']
    home, cwd, output = Path(home).resolve(), Path(cwd).resolve(), Path(output).resolve()
    if not cwd.is_dir() or cwd.is_relative_to(home):
        raise ValueError('Choose an existing project directory outside the target Codex home.')
    restored_files = transfer.restore_session_files(archive, report, cwd)
    if restored_files.get('project'):
        cwd = Path(restored_files['project']['root']).resolve()
    key = transfer.sha(transfer.encoded([report['sha256'], str(home), str(cwd), 'path-rewrite-v2']))[:20]
    work = output / key
    work.mkdir(parents=True, exist_ok=True)
    receipt_path = work / 'receipt.json'
    receipt = json.loads(receipt_path.read_text(encoding='utf-8')) if receipt_path.exists() else {}
    if not receipt:
        data = Path(archive).read_bytes()
        if transfer.sha(data) != report['sha256']:
            raise ValueError('Archive changed after verification.')
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            original = z.read(manifest['record_path'])
            original = transfer.relink_session(original, restored_files['path_map'])
            if json.loads(original.splitlines()[0])['payload'].get('history_base'):
                raise ValueError('Archive has unresolved parent history. Re-export with the updated skill before native import.')
            title = '导入的 Codex 会话'
            for name in z.namelist():
                if name.startswith('metadata/') and name.endswith('.json'):
                    rows = json.loads(z.read(name))
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict) and (row.get('thread_name') or row.get('title')):
                                title = '导入 · ' + str(row.get('thread_name') or row['title'])[:100]
                                break
        source_id = str(transfer.uuid.UUID(manifest['thread_id']))
        isolated = work / 'engine-home'
        imported_source = isolated / 'sessions' / '2000' / '01' / '01' / f'rollout-2000-01-01T00-00-00-{source_id}.jsonl'
        imported_source.parent.mkdir(parents=True, exist_ok=True)
        imported_source.write_bytes(original)
        # Path-based fork alone does not register paginated histories in this engine.
        # Scan only the isolated export, then let Codex assign its own new thread ID.
        with AppServer(executable, isolated, isolated=True) as server:
            listed = server.call('thread/list', {'limit': 100})
            if not any(t['id'] == source_id for t in listed['data']):
                raise ValueError('Installed Codex did not recognize the exported session.')
            resume_without_goal(server, source_id, cwd=str(cwd), runtimeWorkspaceRoots=[str(cwd)])
            before_turns = turn_count(server, source_id)
            expected_history = history_signature(server, source_id)
            if visible_messages(original) and expected_history == transfer.sha(transfer.encoded([])):
                raise ValueError('Installed engine cannot project the exported messages; refusing an empty native restoration.')
            fork = server.call('thread/fork', {'threadId': source_id, 'cwd': str(cwd),
                'runtimeWorkspaceRoots': [str(cwd)], 'excludeTurns': True, 'deferGoalContinuation': True})['thread']
            new_id = str(transfer.uuid.UUID(fork['id']))
            if new_id == source_id or fork.get('ephemeral'):
                raise ValueError('Engine did not create a new persistent thread.')
            generated = Path(fork['path']).resolve()
            if not generated.is_relative_to(isolated.resolve() / 'sessions'):
                raise ValueError('Unexpected engine output path.')
            if turn_count(server, new_id) != before_turns:
                raise ValueError('Engine fork changed the number of historical turns.')
        generated_records = [json.loads(line) for line in generated.read_bytes().splitlines() if line.strip()]
        # Paginated forks store a history_base pointer rather than duplicate messages.
        # Keep the engine-assigned header and materialize its verified source snapshot.
        if generated_records[0]['payload'].get('history_base'):
            generated_records[0]['payload'].pop('history_base')
            generated_records[0]['payload'].pop('forked_from_ordinal_exclusive', None)
            original_records = [json.loads(line) for line in original.splitlines() if line.strip()]
            generated_records = [generated_records[0]] + original_records[1:] + generated_records[1:]
        portable = work / 'portable' / generated.relative_to(isolated.resolve())
        portable.parent.mkdir(parents=True, exist_ok=True)
        # Ordinals are physical record positions. A paginated fork's header starts
        # at the parent's cutoff; a standalone materialization must start at zero.
        for ordinal, row in enumerate(generated_records):
            row['ordinal'] = ordinal
        fork_bytes = b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode('utf-8') for r in generated_records)
        portable.write_bytes(fork_bytes)
        if visible_messages(original) - visible_messages(fork_bytes):
            raise ValueError('Engine fork did not preserve all exported user/assistant messages.')
        rehearsal = work / 'rehearsal-home'
        rehearsal_rollout = rehearsal / generated.relative_to(isolated.resolve())
        rehearsal_rollout.parent.mkdir(parents=True, exist_ok=True)
        rehearsal_rollout.write_bytes(fork_bytes)
        with AppServer(executable, rehearsal, isolated=True) as server:
            server.call('thread/list', {'limit': 100})
            resume_without_goal(server, new_id)
            if turn_count(server, new_id) != before_turns or history_signature(server, new_id) != expected_history:
                raise ValueError('Independent restoration rehearsal did not preserve ordered native history.')
        destination = home / generated.relative_to(isolated.resolve())
        receipt = {'thread_id': new_id, 'source_thread_id': source_id, 'title': title,
            'cwd': str(cwd), 'rollout_path': str(destination), 'engine_rollout': str(portable),
            'source_archive_sha256': report['sha256'], 'source_turn_count': before_turns,
            'source_items_digest': expected_history,
            'restored_files': restored_files['files'], 'file_report': manifest.get('file_report', []),
            'restored_project': restored_files.get('project'),
            'completeness_at_export': manifest.get('completeness'),
            'path_rewrite_version': 2,
            'chat_messages_sent': 0, 'history_verified': True, 'status': 'prepared',
            'native_import_applied': False, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        save_receipt(receipt_path, receipt)
    destination, generated = Path(receipt['rollout_path']), Path(receipt['engine_rollout'])
    if (not destination.resolve().is_relative_to(home / 'sessions')
            or not generated.resolve().is_relative_to(work.resolve() / 'portable')
            or receipt.get('source_archive_sha256') != report['sha256']
            or Path(receipt.get('cwd', '')).resolve() != cwd):
        raise ValueError('Invalid or mismatched import receipt; target state was not changed.')
    # Persist a receipt before touching the target, so retries reuse the same new ID.
    if receipt['status'] == 'prepared':
        backup = home / 'session-transfer-backups' / key
        if backup.is_symlink() or not backup.resolve().is_relative_to(home / 'session-transfer-backups'):
            raise ValueError('Backup directory must stay inside the target Codex home.')
        if not backup_complete(backup):
            home.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                incomplete = backup.with_name(backup.name + '.incomplete-' + transfer.uuid.uuid4().hex[:8])
                if not incomplete.resolve().is_relative_to(home / 'session-transfer-backups'):
                    raise ValueError('Invalid incomplete-backup destination.')
                backup.rename(incomplete)
            backup_indexes(home, backup)
        receipt['backup_path'] = str(backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = generated.read_bytes()
        if destination.exists():
            if destination.read_bytes() != payload:
                raise ValueError('Generated ID conflicts with a different target file; nothing overwritten.')
        else:
            transfer.session_files.publish_file(destination, payload)
        receipt['status'] = 'installed'
        save_receipt(receipt_path, receipt)
    with AppServer(executable, home, not transfer.use_environment_sqlite(home)) as server:
        present = listed_thread(server, receipt['thread_id'])
        # A successful direct read is also required: listing alone is insufficient.
        read = server.call('thread/read', {'threadId': receipt['thread_id']})['thread']
        if Path(read['cwd']).resolve() != cwd:
            raise ValueError('Imported native thread points at the wrong workspace.')
        if receipt['status'] != 'complete':
            resumed = resume_without_goal(server, receipt['thread_id'])
            if resumed['id'] != receipt['thread_id'] or not resumed.get('canAcceptDirectInput', True):
                raise ValueError('Imported thread cannot accept direct input.')
        restored_turns = turn_count(server, receipt['thread_id'])
        if restored_turns != receipt['source_turn_count']:
            raise ValueError(f"Imported native history projection differs from the source: {restored_turns} vs {receipt['source_turn_count']} turns.")
        if history_signature(server, receipt['thread_id']) != receipt['source_items_digest']:
            raise ValueError('Ordered native history items differ from the source snapshot; existing conversation was not overwritten.')
        if receipt['status'] != 'complete':
            server.call('thread/name/set', {'threadId': receipt['thread_id'], 'name': receipt['title']})
        if not present:
            raise ValueError('Imported thread was not present in the native session listing.')
    receipt.update(status='complete', native_import_applied=True, resume_verified=True, listed_in_app_server=True)
    save_receipt(receipt_path, receipt)
    return {**receipt, 'receipt_path': str(receipt_path)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, nargs='?')
    parser.add_argument('--home', type=Path, default=transfer.codex_home())
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--codex', type=Path)
    parser.add_argument('--pick', action='store_true', help='Choose a ZIP with the Windows file picker; no chat prompt needed.')
    args = parser.parse_args()
    gui = None
    try:
        if args.pick:
            import tkinter as tk
            from tkinter import filedialog
            gui = tk.Tk()
            gui.withdraw()
            if not args.archive:
                selected = filedialog.askopenfilename(title='选择要恢复到 Codex 的会话 ZIP', filetypes=[('Codex session ZIP', '*.zip')])
                if not selected:
                    return 0
                args.archive = Path(selected)
        if not args.archive:
            raise ValueError('A session ZIP is required.')
        if args.cwd is None and args.pick:
            manifest = transfer.verify_archive(args.archive)['manifest']
            previous = Path(manifest.get('cwd') or args.archive.parent)
            args.cwd = previous if previous.is_dir() else args.archive.parent
        cwd = args.cwd or Path.cwd()
        output = args.output or args.home.resolve() / 'session-transfer-imports'
        legacy_output = args.archive.resolve().parent / 'session-imports'
        if args.output is None and legacy_output.is_dir():
            archive_sha = transfer.verify_archive(args.archive)['sha256']
            legacy_key = transfer.sha(transfer.encoded([
                archive_sha, str(args.home.resolve()), str(cwd.resolve()), 'path-rewrite-v2']))[:20]
            if (legacy_output / legacy_key / 'receipt.json').is_file():
                output = legacy_output
        report = restore_native(args.archive, args.home, cwd, output, args.codex)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        if gui:
            from tkinter import messagebox
            messagebox.showinfo('Codex 会话已恢复', f"已恢复为原生会话：{report['title']}\n项目：{report['cwd']}\n未发送任何聊天消息。")
        return 0
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        if gui:
            from tkinter import messagebox
            messagebox.showerror('导入未完成', str(exc))
        return 1
    finally:
        if gui:
            gui.destroy()


if __name__ == '__main__':
    sys.exit(main())
