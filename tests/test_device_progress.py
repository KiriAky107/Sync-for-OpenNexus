"""Authentication samples, transaction commits and generated pages are distinct."""
import uuid

import pytest

from sync_server.database import row, rows, run
from sync_server.operations import _load_manifest, _load_database_snapshot, verify_backup
from tools.operations_probe import legacy_progress_snapshot
from test_admin import admin
from test_protocol import change, upload
from test_reclamation import snapshot


def read(client, base, auth):
    response = client.get(base+'/devices/progress', headers=auth)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    return response.json()


def test_activity_is_sampled_authentication_and_unknown_login_is_not_sync(admin):
    client, db, _, now, _, auth, session, login, _ = admin
    fresh, fresh_session = login('member')
    with db.read_only() as conn:
        assert row(conn,'SELECT * FROM device_activity WHERE device_id=:id',id=fresh_session['device_id']) is None
    first=client.get('/sync/v1/devices',headers=fresh).json()
    assert first['items'][0]['authenticated_at'] == now[0] and first['activity_sample_seconds'] == 60
    recorded=now[0]; now[0]+=59
    assert client.get('/sync/v1/devices',headers=fresh).json()['items'][0]['authenticated_at']==recorded
    now[0]+=1
    assert client.get('/sync/v1/devices',headers=fresh).json()['items'][0]['authenticated_at']==now[0]
    with db.read_only() as conn: before=[dict(item) for item in rows(conn,'SELECT * FROM device_activity')]
    assert client.get('/sync/v1/devices',headers={'Authorization':'Bearer invalid'}).status_code == 401
    with db.read_only() as conn: assert [dict(item) for item in rows(conn,'SELECT * FROM device_activity')]==before


def test_confirmed_commit_and_offered_cursor_do_not_prove_client_application(admin):
    client, _, _, now, _, auth, session, login, _ = admin
    vault=client.post('/sync/v1/vaults',headers=auth,json={'name':'progress'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    second, second_session=login('operator')
    no_activity=read(client,base,second)
    own=next(item for item in no_activity['items'] if item['device_id']==second_session['device_id'])
    assert own['authenticated_at'] is None and own['committed_sequence'] is None and own['offered_sequence'] is None
    sha=upload(client,base,auth,b'confirmed data')
    body=change(sha,size=len(b'confirmed data'))
    assert client.post(base+'/revisions',headers=auth,json=body).status_code == 200
    original=next(item for item in read(client,base,auth)['items'] if item['device_id']==session['device_id'])
    assert original['committed_sequence']==1 and original['committed_at']==now[0]
    now[0]+=61
    # Ignoring a successful response doesn't undo the actual server transaction.
    assert client.post(base+'/revisions',headers=auth,json=body).json()['sequence']==1
    replay=next(item for item in read(client,base,auth)['items'] if item['device_id']==session['device_id'])
    assert replay['committed_at']==original['committed_at']
    newer={**body,'file_id':uuid.uuid4().hex,'path':'new.md','operation_id':uuid.uuid4().hex}
    assert client.post(base+'/revisions',headers=auth,json=newer).json()['sequence']==2
    assert client.get(base+'/changes?limit=1',headers=second).json()['cursor']==1
    first_page=next(item for item in read(client,base,auth)['items'] if item['device_id']==second_session['device_id'])
    assert first_page['offered_sequence']==1 and first_page['committed_sequence'] is None
    assert client.get(base+'/changes',headers=second).json()['cursor']==2
    latest=read(client,base,auth)
    assert not latest['client_application_confirmed'] and latest['offered_semantics']=='server_generated_page'
    now[0]+=61
    assert client.get(base+'/changes?limit=1',headers=second).json()['cursor']==1
    actual=next(item for item in read(client,base,auth)['items'] if item['device_id']==second_session['device_id'])
    assert actual['offered_sequence']==2
    assert actual['offered_at']==first_page['offered_at']
    assert client.post(base+'/revisions',headers=auth,json={**body,'operation_id':uuid.uuid4().hex}).status_code==409
    assert next(item for item in read(client,base,auth)['items'] if item['device_id']==session['device_id'])['committed_sequence']==2


def test_progress_reads_are_read_only_and_scoped_to_owner_or_operator(admin):
    client, db, _, now, ids, auth, _, login, _ = admin
    member, member_session=login('member')
    vault=client.post('/sync/v1/vaults',headers=member,json={'name':'owned'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    with db.transaction() as conn:
        for i in range(25): run(conn,'INSERT INTO devices VALUES (:id,:user,:name,0)',id=f'{i:032x}',user=ids['member'],name='old device')
    now[0]+=61
    with db.read_only() as conn:
        before=[dict(item) for item in rows(conn,'SELECT * FROM device_activity')]
    page=read(client,base,member)
    second=client.get(base+'/devices/progress',headers=member,params={'before':page['next_before']}).json()
    assert len(page['items'])==20 and len(second['items'])==6
    assert len({item['device_id'] for item in page['items']+second['items']})==26
    assert all(item['authenticated_at'] is None for item in page['items']+second['items'] if item['name']=='old device')
    admin_path='/sync/v1/admin/accounts/'+ids['member']+'/vaults/'+vault+'/devices/progress'
    assert client.get(admin_path,headers=auth).status_code==200
    assert client.get(admin_path,headers=member).status_code==403
    assert client.get(base+'/devices/progress',headers=auth).status_code==404
    assert client.get(admin_path,headers=auth,params={'limit':101}).status_code==422
    with db.read_only() as conn: assert [dict(item) for item in rows(conn,'SELECT * FROM device_activity')]==before
    with db.transaction() as conn: run(conn,'UPDATE devices SET revoked=1 WHERE id=:id',id=member_session['device_id'])
    assert client.get(base+'/devices/progress',headers=member).status_code==401


def test_progress_failure_rolls_back_revision_and_authentication_together(admin, monkeypatch):
    client, db, _, now, _, auth, _, _, _ = admin
    vault=client.post('/sync/v1/vaults',headers=auth,json={'name':'atomic'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    sha=upload(client,base,auth,b'original bytes')
    with db.read_only() as conn:
        before=[dict(item) for item in rows(conn,'SELECT * FROM device_activity')]
    now[0]+=61
    import sync_server.device_progress as progress
    original=progress.progress
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('controlled transaction failure')
    monkeypatch.setattr(progress,'progress',fail)
    with pytest.raises(RuntimeError,match='controlled transaction failure'):
        client.post(base+'/revisions',headers=auth,json=change(sha,size=len(b'original bytes')))
    with db.read_only() as conn:
        assert row(conn,'SELECT sequence FROM vaults WHERE id=:id',id=vault)['sequence']==0
        assert not rows(conn,'SELECT * FROM revisions') and not rows(conn,'SELECT * FROM device_progress')
        assert [dict(item) for item in rows(conn,'SELECT * FROM device_activity')]==before


def test_snapshot_keeps_observations_and_old_snapshot_loads_unknown(admin,tmp_path):
    client, db, store, _, _, auth, _, _, _ = admin
    vault=client.post('/sync/v1/vaults',headers=auth,json={'name':'snapshot'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    sha=upload(client,base,auth,b'backup data')
    client.post(base+'/revisions',headers=auth,json=change(sha,size=len(b'backup data')))
    client.get(base+'/changes',headers=auth)
    directory=tmp_path/'snapshot'; manifest=snapshot(db,store,directory)
    saved=_load_database_snapshot(directory,manifest)
    assert saved['device_activity'] and saved['device_progress'][0][2:]==[1,1000000,1,1000000]
    original={str(path.relative_to(directory)):path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    legacy=tmp_path/'legacy-snapshot'
    legacy_progress_snapshot(directory,legacy)
    assert original=={str(path.relative_to(directory)):path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    assert verify_backup(legacy)['verified_objects']==1
    recovered=_load_database_snapshot(legacy,_load_manifest(legacy,24))
    assert recovered['device_activity']==[] and recovered['device_progress']==[]
