import hashlib
import uuid
from pathlib import Path
from sync_server.database import row, run
from sync_server.maintenance import cleanup_expired_uploads
from test_protocol import env, setup, session, upload, change


def test_usage_deduplicates_current_and_historical_objects_and_separates_upload_reservations(env):
    client, db, _, _ = env
    auth, _, base = setup(client)
    first = b'first version'; second = b'second version'
    first_hash = upload(client, base, auth, first)
    body = change(first_hash, size=len(first))
    assert client.post(base+'/revisions', headers=auth, json=body).status_code == 200
    duplicate = {**body, 'file_id':uuid.uuid4().hex, 'operation_id':uuid.uuid4().hex, 'path':'duplicate.md'}
    assert client.post(base+'/revisions', headers=auth, json=duplicate).status_code == 200
    current = client.get(base+'/usage', headers=auth).json()
    assert current['current_object_bytes'] == len(first)
    assert current['logical_file_bytes'] == 2*len(first)
    second_hash = upload(client, base, auth, second)
    for source, revision in [(body,1), (duplicate,2)]:
        assert client.post(base+'/revisions', headers=auth, json={**source,'operation_id':uuid.uuid4().hex,'base_revision':revision,'content_hash':second_hash,'size':len(second)}).status_code == 200
    orphan = b'completed but not committed'
    upload(client, base, auth, orphan)
    pending = client.post(base+'/uploads', headers=auth, json={'content_hash':hashlib.sha256(b'pending').hexdigest(),'size':7}).json()['upload_id']
    assert client.put(base+'/uploads/'+pending+'?offset=0',headers=auth,content=b'pen').status_code == 200
    result = client.get(base+'/usage',headers=auth).json()
    assert result['object_bytes'] == len(first)+len(second)+len(orphan)
    assert result['charged_bytes'] == result['object_bytes']
    assert result['current_object_bytes'] == len(second)
    assert result['historical_only_bytes'] == len(first)
    assert result['unreferenced_object_bytes'] == len(orphan)
    assert result['reserved_bytes'] == 7 and result['confirmed_upload_bytes'] == 3
    assert result['history_retention'] == 'indefinite' and result['accounting_matches']
    with db.transaction() as conn: run(conn, 'UPDATE vaults SET used=used+1 WHERE id=:id',id=base.rsplit('/',1)[-1])
    assert not client.get(base+'/usage',headers=auth).json()['accounting_matches']


def test_owner_upload_paging_other_device_cancel_idempotency_and_cross_account_rejection(env):
    client, _, _, _ = env
    auth, _, base = setup(client); second, _ = session(client); foreign, _ = session(client,'bob')
    ids = []
    for index in range(4):
        item = client.post(base+'/uploads',headers=second,json={'content_hash':hashlib.sha256(str(index).encode()).hexdigest(),'size':1}).json()
        ids.append(item['upload_id'])
    first_page = client.get(base+'/uploads?limit=2',headers=auth).json()
    second_page = client.get(base+'/uploads',params={'limit':2,'before':first_page['next_before']},headers=auth).json()
    assert {item['id'] for item in first_page['items']+second_page['items']} == set(ids)
    target = ids[0]
    assert client.delete(base+'/uploads/'+target,headers=auth).status_code == 404
    for endpoint in ('/usage','/uploads','/uploads/'+target+'/result'):
        assert client.get(base+endpoint,headers=foreign).status_code == 404
    assert client.post(base+'/uploads/'+target+'/cancel',headers=foreign).status_code == 404
    receipt = client.post(base+'/uploads/'+target+'/cancel',headers=auth).json()
    assert receipt['state'] == 'cancelled'
    assert client.post(base+'/uploads/'+target+'/cancel',headers=second).json() == receipt
    assert client.get(base+'/uploads/'+target+'/result',headers=auth).json() == receipt
    assert client.get(base+'/usage',headers=auth).json()['reserved_bytes'] == 3
    assert client.get(base+'/uploads?before=invalid',headers=auth).status_code == 422


def test_completion_wins_cancel_race_and_expiry_preserves_readonly_disposition(env):
    client, db, store, now = env
    auth, _, base = setup(client)
    content = b'kept object'; content_hash = hashlib.sha256(content).hexdigest()
    identity = client.post(base+'/uploads',headers=auth,json={'content_hash':content_hash,'size':len(content)}).json()['upload_id']
    endpoint = base+'/uploads/'+identity
    assert client.put(endpoint+'?offset=0',headers=auth,content=content).status_code == 200
    assert client.post(endpoint+'/complete',headers=auth).status_code == 200
    assert client.post(endpoint+'/cancel',headers=auth).json()['state'] == 'completed'
    assert client.get(base+'/objects/'+content_hash,headers=auth).content == content
    expired = client.post(base+'/uploads',headers=auth,json={'content_hash':'f'*64,'size':10}).json()['upload_id']
    now[0] += 3600
    fresh, _ = session(client)
    assert client.get(base+'/usage',headers=fresh).json()['reserved_bytes'] == 0
    assert client.get(base+'/usage',headers=fresh).json()['expired_uploads'] == 1
    result = cleanup_expired_uploads(db, store.root.parent/'staging',now=now[0])
    assert result['expired_uploads_removed'] == 1
    assert client.get(base+'/uploads/'+expired+'/result',headers=fresh).json()['state'] == 'expired'
    with db.transaction() as conn:
        summary = row(conn,"SELECT * FROM maintenance_summary WHERE kind='uploads'")
        assert summary['removed'] == 1 and summary['released_bytes'] == 10


def test_failed_filesystem_removal_is_retryable_and_classified_without_raw_paths(env, monkeypatch):
    client, db, store, now = env
    auth, _, base = setup(client)
    identity = client.post(base+'/uploads',headers=auth,json={'content_hash':'e'*64,'size':12}).json()['upload_id']
    real_unlink = Path.unlink
    def fail(self,*args,**kwargs):
        if self.name == identity: raise OSError('sensitive-private-path')
        return real_unlink(self,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail)
    cancel = client.post(base+'/uploads/'+identity+'/cancel',headers=auth)
    assert cancel.status_code == 503 and 'sensitive' not in cancel.text
    assert client.get(base+'/uploads/'+identity+'/result',headers=auth).json()['state'] == 'active'
    now[0] += 3600
    first = cleanup_expired_uploads(db,store.root.parent/'staging',now=now[0])
    assert first['filesystem_failures'] == 1 and first['expired_uploads_removed'] == 0
    monkeypatch.setattr(Path,'unlink',real_unlink)
    second = cleanup_expired_uploads(db,store.root.parent/'staging',now=now[0])
    assert second['expired_uploads_removed'] == 1
    with db.transaction() as conn:
        summary = row(conn,"SELECT * FROM maintenance_summary WHERE kind='uploads'")
        assert summary['total_failures'] == 1 and summary['total_removed'] == 1
    assert client.get('/sync/v1/admin/maintenance',headers=auth).status_code == 401
    fresh, _ = session(client)
    assert client.get('/sync/v1/admin/maintenance',headers=fresh).status_code == 403


def test_damaged_upload_result_and_operator_metrics_are_bound_to_actual_role(env):
    client, db, store, _ = env
    auth, _, base = setup(client)
    identity = client.post(base+'/uploads',headers=auth,json={'content_hash':'d'*64,'size':4}).json()['upload_id']
    assert client.put(base+'/uploads/'+identity+'?offset=0',headers=auth,content=b'test').status_code == 200
    (store.root.parent/'staging'/identity).unlink()
    assert client.get(base+'/uploads/'+identity,headers=auth).status_code == 409
    assert client.get(base+'/uploads/'+identity+'/result',headers=auth).json()['state'] == 'damaged'
    assert client.get(base+'/usage',headers=auth).json()['reserved_bytes'] == 0
    from fastapi.testclient import TestClient
    from sync_server.app import create_app
    with db.transaction() as conn:
        operator = row(conn,"SELECT id FROM users WHERE username='alice'")['id']
    with TestClient(create_app(db,store,store.root.parent/'staging',operator_user_ids=[operator])) as operator_client:
        alice, _ = session(operator_client)
        bob, _ = session(operator_client,'bob')
        assert operator_client.get('/sync/v1/admin/maintenance',headers=alice).status_code == 200
        assert operator_client.get('/sync/v1/admin/maintenance',headers=bob).status_code == 403
