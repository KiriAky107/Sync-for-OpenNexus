"""Real backup bytes, durable operational intents and authorized read-only reports."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from sync_server.app import create_app
from sync_server.database import Database, row
from sync_server.operation_journal import OperationJournal
from sync_server.operations import TABLES, OperationsError, verify_backup
from sync_server.storage import DiskObjects

def backup(root):
    root.mkdir()
    body = "print('must not execute') # 中文😀\r\n".encode()
    digest = hashlib.sha256(body).hexdigest()
    vault = 'a' * 32
    target = root / 'objects' / vault / digest
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    records = [dict(table='schema_version', values=[1]), dict(table='objects', values=[vault, digest, len(body), 1])]
    database = root / 'database.jsonl'
    database.write_text(''.join(json.dumps(item) + '\n' for item in records), 'utf-8')
    manifest = dict(schema=1, created_utc=datetime.now(timezone.utc).isoformat(),
                    database_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
                    database_rows={name: int(name in ('schema_version', 'objects')) for name in TABLES},
                    object_count=1, object_bytes=len(body), objects=[dict(vault_id=vault, hash=digest, size=len(body))])
    (root / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
    return target, manifest

def test_verifies_real_backup_without_database_credentials_and_keeps_every_byte(tmp_path):
    source = tmp_path / 'backup'
    _, manifest = backup(source)
    original = {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}
    result = verify_backup(source)
    assert result['status'] == 'BACKUP_VERIFIED'
    assert result['object_bytes'] == manifest['object_bytes'] and result['verified_objects'] == 1
    assert original == {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}

@pytest.mark.parametrize('failure,code', [
    ('object', 'BACKUP_OBJECT_INTEGRITY_FAILED'), ('missing', 'BACKUP_OBJECT_MISSING'),
    ('database', 'BACKUP_DATABASE_INTEGRITY_FAILED'), ('expired', 'BACKUP_AGE_INVALID'),
])
def test_standalone_check_reuses_restore_integrity_and_age_checks(tmp_path, failure, code):
    source = tmp_path / 'backup'
    target, manifest = backup(source)
    if failure == 'object':
        content = target.read_bytes()
        target.write_bytes(b'X' + content[1:])
    elif failure == 'missing':
        target.unlink()
    elif failure == 'database':
        (source / 'database.jsonl').write_text('corrupt', 'utf-8')
    else:
        manifest['created_utc'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        (source / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
    journal = OperationJournal(tmp_path / 'ops.sqlite3')
    with pytest.raises(OperationsError, match=code):
        journal.run('verify', lambda: verify_backup(source))
    receipt = OperationJournal(journal.path).records()['items'][0]
    assert receipt['state'] == 'failed' and receipt['code'] == code
    assert receipt['finished_utc'] is not None and receipt['verified_objects'] is None

def test_actual_process_death_leaves_durable_unfinished_intent(tmp_path):
    path = tmp_path / 'ops.sqlite3'
    code = "from pathlib import Path; import sys; from sync_server.operation_journal import OperationJournal; print(OperationJournal(Path(sys.argv[1])).start('restore'), flush=True); sys.stdin.read()"
    child = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        operation_id = child.stdout.readline().decode().strip()
        assert len(operation_id) == 32
        child.kill()
        child.wait(timeout=10)
        receipt = OperationJournal(path).records()['items'][0]
        assert receipt['operation_id'] == operation_id
        assert receipt['state'] == 'unfinished' and receipt['finished_utc'] is None
        assert receipt['code'] == 'OPERATION_UNFINISHED'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()

def test_receipt_pagination_and_failure_redaction_survive_reopen(tmp_path):
    journal = OperationJournal(tmp_path / 'ops.sqlite3')
    first = journal.run('backup', lambda: dict(status='BACKUP_COMPLETE', object_count=2, object_bytes=42, source='/secret', access_token='never-store'))
    with pytest.raises(RuntimeError):
        journal.run('restore', lambda: (_ for _ in ()).throw(RuntimeError('private/password/host must not leak')))
    latest = OperationJournal(journal.path).records(limit=1)
    assert latest['items'][0]['code'] == 'RESTORE_FAILED' and latest['next_before'] is not None
    previous = journal.records(limit=1, before=latest['next_before'])
    assert previous['items'][0]['operation_id'] == first['operation_id']
    assert previous['next_before'] is None
    serialized = json.dumps(latest) + json.dumps(previous)
    assert 'secret' not in serialized and 'password' not in serialized and 'never-store' not in serialized
    with sqlite3.connect(journal.path) as conn:
        assert 'never-store' not in str(conn.execute('SELECT * FROM operation_receipts').fetchall())

def test_unknown_success_receipt_never_claims_completion_or_replays_action(tmp_path, monkeypatch):
    journal = OperationJournal(tmp_path / 'ops.sqlite3')
    calls = []
    def unavailable(*args, **kwargs):
        raise OSError('controlled lost receipt')
    monkeypatch.setattr(journal, 'finish', unavailable)
    with pytest.raises(OperationsError, match='OPERATIONS_RECEIPT_UNAVAILABLE'):
        journal.run('backup', lambda: calls.append(1) or dict(status='BACKUP_COMPLETE'))
    assert calls == [1]
    assert OperationJournal(journal.path).records()['items'][0]['state'] == 'unfinished'

def test_cli_verify_and_paged_records_run_without_postgres_or_s3_settings(tmp_path):
    source = tmp_path / 'backup'
    backup(source)
    env = {key: value for key, value in os.environ.items() if not key.startswith('SYNC_')}
    env['SYNC_OPERATIONS_PATH'] = str(tmp_path / 'ops.sqlite3')
    result = subprocess.run([sys.executable, '-m', 'sync_server', 'verify-backup', '--directory', str(source)], env=env, capture_output=True, text=True, check=True)
    verified = json.loads(result.stdout)
    assert verified['status'] == 'BACKUP_VERIFIED'
    records = subprocess.run([sys.executable, '-m', 'sync_server', 'operation-records', '--limit', '1'], env=env, capture_output=True, text=True, check=True)
    assert json.loads(records.stdout)['items'][0]['operation_id'] == verified['operation_id']

def test_operation_status_requires_configured_fixed_account_and_live_device_authorization(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'db.sqlite3'))
    db.migrate()
    db.add_user('operator', 'controlled-operator-password')
    db.add_user('member', 'controlled-member-password')
    with db.transaction() as conn:
        operator = row(conn, 'SELECT id FROM users WHERE username=:name', name='operator')['id']
    path = tmp_path / 'ops.sqlite3'
    journal = OperationJournal(path)
    journal.run('backup', lambda: dict(status='BACKUP_COMPLETE', object_bytes=42, object_count=1))
    app = create_app(db, DiskObjects(tmp_path / 'objects'), tmp_path / 'staging', operations_path=path, operator_user_ids=(operator,))
    with TestClient(app) as client:
        assert client.get('/sync/v1/operations').status_code == 401
        def login(name):
            return client.post('/sync/v1/auth/sessions', json=dict(username=name, password=f'controlled-{name}-password', device_name='Console')).json()
        member = login('member')
        session = login('operator')
        assert client.get('/sync/v1/operations', headers={'Authorization': 'Bearer ' + member['access_token']}).status_code == 403
        headers = {'Authorization': 'Bearer ' + session['access_token']}
        response = client.get('/sync/v1/operations?limit=1', headers=headers)
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json()['items'][0]['state'] == 'succeeded'
        assert str(tmp_path) not in response.text and session['access_token'] not in response.text
        assert client.get('/sync/v1/operations?before=-1', headers=headers).status_code == 422
        assert client.get('/sync/v1/operations?limit=101', headers=headers).status_code == 422
        with db.transaction() as conn:
            conn.exec_driver_sql("UPDATE users SET username='renamed-operator' WHERE id=?", (operator,))
        assert client.get('/sync/v1/operations', headers=headers).status_code == 200
        assert client.delete('/sync/v1/devices/' + session['device_id'], headers=headers).status_code == 204
        assert client.get('/sync/v1/operations', headers=headers).status_code == 401
    db.engine.dispose()

def test_missing_journal_is_empty_but_corruption_and_unknown_schema_fail_closed(tmp_path):
    journal = OperationJournal(tmp_path / 'ops.sqlite3')
    assert journal.records()['items'] == [] and not journal.path.exists()
    journal.path.write_bytes(b'broken sqlite')
    with pytest.raises(OperationsError, match='OPERATIONS_STATUS_UNAVAILABLE'):
        journal.records()
    journal.path.unlink()
    with sqlite3.connect(journal.path) as conn:
        conn.execute('PRAGMA user_version=9')
    with pytest.raises(OperationsError, match='OPERATIONS_SCHEMA_INCOMPATIBLE'):
        journal.records()
    with pytest.raises(OperationsError, match='OPERATIONS_SCHEMA_INCOMPATIBLE'):
        journal.start('restore')

@pytest.mark.parametrize('field,value', [('code', 'private/password'), ('started_at', -1)])
def test_invalid_stored_record_never_exposes_raw_values(tmp_path, field, value):
    journal = OperationJournal(tmp_path / 'ops.sqlite3')
    journal.start('backup')
    with sqlite3.connect(journal.path) as conn:
        conn.execute(f'UPDATE operation_receipts SET {field}=?', (value,))
    with pytest.raises(OperationsError, match='OPERATIONS_RECORD_INVALID'):
        journal.records()
