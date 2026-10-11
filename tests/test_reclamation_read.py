"""Read-only operator inspection must preserve plans, history and recovery bytes."""
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from sync_server.database import row, run
from sync_server.reclamation import _prepare, preview
from test_admin import admin
from test_protocol import upload, change
from test_reclamation import snapshot

BASE = '/sync/v1/admin/reclamation/plans'


@pytest.fixture
def planned(admin, tmp_path):
    client, db, store, now, ids, auth, session, login, app = admin
    vault = client.post('/sync/v1/vaults', headers=auth, json={'name': 'read-only plan'}).json()['vault_id']
    base = '/sync/v1/vaults/' + vault
    orphan = upload(client, base, auth, b'original orphan bytes')
    history = upload(client, base, auth, b'original history bytes')
    assert client.post(base+'/revisions', headers=auth, json=change(history, size=len(b'original history bytes'))).status_code == 200
    source = tmp_path/'backup'
    snapshot(db, store, source)
    now[0] += 8*86400
    auth, session = login('operator')
    plan = preview(db, source, vault, now=now[0])
    return client, db, store, now, ids, auth, session, login, app, plan, source, orphan, history


def test_reads_use_only_read_snapshot_and_keep_prepared_intentions(planned, monkeypatch):
    client, db, store, now, _, auth, _, _, _, plan, source, orphan, history = planned
    _prepare(db, plan['plan_id'], now[0])
    before = {str(path): path.read_bytes() for path in source.rglob('*') if path.is_file()}
    with db.read_only() as conn:
        original = {table: [dict(item) for item in run(conn, 'SELECT * FROM '+table).mappings()] for table in ['reclamation_plans', 'reclamation_objects', 'revisions', 'objects', 'vaults', 'sessions']}
    def forbidden(*args, **kwargs):
        pytest.fail('Read endpoint entered write/backup/object boundary')
    monkeypatch.setattr(db, 'transaction', forbidden)
    for name in ['get', 'put', 'delete']:
        monkeypatch.setattr(store, name, forbidden)
    import sync_server.reclamation as gc
    for name in ['preview', 'apply', '_vault', '_backup', 'status']:
        monkeypatch.setattr(gc, name, forbidden)
    queries = []
    def record(conn, cursor, statement, parameters, context, many):
        queries.append(statement.upper())
    event.listen(db.engine, 'before_cursor_execute', record)
    try:
        page = client.get(BASE, headers=auth)
        assert page.status_code == 200 and page.headers['cache-control'] == 'private, no-store'
        assert page.json()['items'][0]['state'] == 'running'
        result = client.get(BASE+'/'+plan['plan_id'], headers=auth).json()
        assert result['snapshot_only'] and result['preview'] == plan['preview']
        assert result['pending_objects'] == 1 and result['reclaimed_objects'] == 0
        assert result['items'][0]['code'] == 'GC_PREPARED'
        assert result['preview']['protected_counts'] == {'revision': 1}
        assert result['backup_manifest_sha256'] == plan['backup_manifest_sha256']
        with db.read_only() as conn:
            for table, saved in original.items():
                assert [dict(item) for item in run(conn, 'SELECT * FROM '+table).mappings()] == saved
    finally:
        event.remove(db.engine, 'before_cursor_execute', record)
    assert not any('FOR UPDATE' in sql or 'BEGIN IMMEDIATE' in sql for sql in queries)
    assert {str(path): path.read_bytes() for path in source.rglob('*') if path.is_file()} == before
    assert (store.root/plan['vault_id']/orphan).read_bytes() == b'original orphan bytes'
    assert (store.root/plan['vault_id']/history).read_bytes() == b'original history bytes'


def test_read_context_rejects_writes_and_restores_connection_on_success_and_error(admin):
    _, db, _, _, _, _, _, _, _ = admin
    for failure in [False, True]:
        if failure:
            with pytest.raises(OperationalError):
                with db.read_only() as readonly:
                    run(readonly, 'DELETE FROM users')
        else:
            with db.read_only() as conn:
                assert conn.exec_driver_sql('PRAGMA query_only').scalar() == 1
        with db.transaction() as conn:
            assert conn.exec_driver_sql('PRAGMA query_only').scalar() == 0
            run(conn, "UPDATE users SET username=username")


def test_pagination_filters_expiry_and_role_are_exact(planned):
    client, db, _, now, _, auth, session, login, _, plan, _, _, _ = planned
    with db.transaction() as conn:
        saved = dict(row(conn, 'SELECT * FROM reclamation_plans WHERE id=:id', id=plan['plan_id']))
        for i in range(23):
            values = {**saved, 'id': f'{i:032x}', 'state': 'complete' if i == 0 else 'preview'}
            run(conn, 'INSERT INTO reclamation_plans VALUES (:id,:vault_id,:actor_id,:created,:expires,:backup_hash,:snapshot,:state,:finished,:code)', **values)
    found, cursor = [], None
    while True:
        data = client.get(BASE, headers=auth, params={'limit': 5, **({'before': cursor} if cursor else {})}).json()
        found.extend(item['plan_id'] for item in data['items'])
        cursor = data['next_before']
        if cursor is None: break
    assert len(found) == len(set(found)) == 24 and found == sorted(found, reverse=True)
    assert len(client.get(BASE, headers=auth, params={'state':'complete'}).json()['items']) == 1
    assert client.get(BASE, headers=auth, params={'vault_id':'f'*32}).json()['items'] == []
    for params in [{'before':'bad'}, {'before':'9007199254740992:'+plan['plan_id']}, {'limit':101}, {'state':'deleted'}]:
        assert client.get(BASE, headers=auth, params=params).status_code == 422
    assert client.get(BASE+'/'+'f'*32, headers=auth).status_code == 404
    ordinary, _ = login('member')
    assert client.get(BASE, headers=ordinary).status_code == 403
    assert client.get(BASE+'/'+plan['plan_id'], headers=ordinary).status_code == 403
    now[0] = plan['expires'] + 1
    auth, session = login('operator')
    result = client.get(BASE+'/'+plan['plan_id'], headers=auth).json()
    assert result['expired'] and result['snapshot_only'] and result['state'] == 'preview'
    with db.transaction() as conn: run(conn, 'UPDATE devices SET revoked=1 WHERE id=:id', id=session['device_id'])
    assert client.get(BASE, headers=auth).status_code == 401


def test_provider_probe_reads_original_cli_contract_without_object_io(planned):
    from tools.operations_probe import reclamation_read_probe
    client, db, store, _, _, auth, _, login, _, plan, source, _, _ = planned
    ordinary, _ = login('member')
    proof = reclamation_read_probe(client, db, store, source, auth, ordinary, plan)
    assert proof['ledger_and_backup_unchanged'] and proof['ordinary_account_forbidden']
    assert proof['object_io_calls'] == 0 and proof['state'] == 'preview'
    assert not proof['postgres_readonly_write_denial_and_pool_restoration']
