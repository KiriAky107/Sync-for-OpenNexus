"""Actual role checks, password-preserving retries, quota CAS and diagnostics."""
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import pytest
from sqlalchemy.exc import OperationalError

from sync_server.app import create_app
from sync_server.database import Database, row, run
from sync_server.storage import DiskObjects


@pytest.fixture
def admin(tmp_path):
    db=Database('sqlite:///'+str(tmp_path/'db.sqlite3')); db.migrate()
    for name in ['operator','second-operator','member','admin']:
        db.add_user(name,'controlled-admin-fixture-password')
    with db.transaction() as conn:
        ids={item['username']:item['id'] for item in run(conn,'SELECT id,username FROM users').mappings()}
    store=DiskObjects(tmp_path/'objects'); now=[1000000]
    app=create_app(db,store,tmp_path/'staging',clock=lambda:now[0],operator_user_ids=[ids['operator'],ids['second-operator']])
    with TestClient(app) as client:
        def login(name,password='controlled-admin-fixture-password'):
            reply=client.post('/sync/v1/auth/sessions',json={'username':name,'password':password,'device_name':'managed-fixture'})
            assert reply.status_code == 200
            session=reply.json()
            return {'Authorization':'Bearer '+session['access_token']},session
        auth,session=login('operator')
        yield client,db,store,now,ids,auth,session,login,app
    db.engine.dispose()


def operation(**kwargs):
    return {'operation_id':uuid.uuid4().hex,**kwargs}


def test_only_configured_fixed_identity_has_administration_rights(admin):
    client,db,_,_,ids,auth,_,login,_=admin
    ordinary,_=login('admin')
    for endpoint in ['/access','/accounts','/accounts/'+ids['member'],'/diagnostics','/operations/'+uuid.uuid4().hex]:
        response=client.get('/sync/v1/admin'+endpoint,headers=ordinary)
        assert response.status_code == 403 and response.headers['cache-control'] == 'private, no-store'
    assert client.get('/sync/v1/admin/access',headers=auth).json()['user_id'] == ids['operator']
    with db.transaction() as conn: run(conn,"UPDATE users SET username='renamed' WHERE id=:id",id=ids['operator'])
    assert client.get('/sync/v1/admin/access',headers=auth).status_code == 200
    assert client.get('/sync/v1/admin/accounts?before=not-an-id',headers=auth).status_code == 422
    page=client.get('/sync/v1/admin/accounts?limit=2',headers=auth).json()
    last=client.get('/sync/v1/admin/accounts',headers=auth,params={'limit':2,'before':page['next_before']}).json()
    assert {item['id'] for item in page['items']+last['items']} == set(ids.values())


def test_create_account_replay_preserves_later_password_and_only_stores_scrypt_proof(admin):
    client,db,_,_,_,auth,_,login,_=admin
    secret='controlled-created-user-password'
    body=operation(username='new-user',password=secret,default_quota=42)
    first=client.post('/sync/v1/admin/accounts',headers=auth,json=body)
    assert first.status_code == 200 and secret not in first.text
    result=first.json()
    account_auth,_=login('new-user',secret)
    new_secret='controlled-later-user-password'
    assert client.put('/sync/v1/account/credentials',headers=account_auth,json={'current_password':secret,'username':'new-user-renamed','password':new_secret}).status_code == 200
    assert client.post('/sync/v1/admin/accounts',headers=auth,json=body).json() == result
    login('new-user-renamed',new_secret)
    assert client.post('/sync/v1/admin/accounts',headers=auth,json={**body,'password':'controlled-different-password'}).status_code == 409
    assert client.get('/sync/v1/admin/operations/'+body['operation_id'],headers=auth).json() == result
    other,_=login('second-operator')
    assert client.get('/sync/v1/admin/operations/'+body['operation_id'],headers=other).json()['state'] == 'not_found'
    with db.transaction() as conn:
        receipt=dict(row(conn,'SELECT * FROM admin_receipts WHERE id=:id',id=body['operation_id']))
        assert len(receipt['input_salt']) == 32 and len(receipt['fingerprint']) == 64
        assert secret not in json.dumps(receipt) and new_secret not in json.dumps(receipt)


def test_account_default_policy_only_affects_new_vaults_and_is_cas_bound(admin):
    client,_,_,_,ids,auth,_,login,_=admin
    member,_=login('member')
    original=client.post('/sync/v1/vaults',headers=member,json={'name':'existing'}).json()['vault_id']
    body=operation(expected_revision=0,quota=31)
    endpoint='/sync/v1/admin/accounts/'+ids['member']+'/policy'
    result=client.put(endpoint,headers=auth,json=body).json()
    assert result['policy_revision'] == 1
    assert client.put(endpoint,headers=auth,json=body).json() == result
    assert client.put(endpoint,headers=auth,json=operation(expected_revision=0,quota=99)).status_code == 409
    created=client.post('/sync/v1/vaults',headers=member,json={'name':'new'}).json()['vault_id']
    vaults=client.get('/sync/v1/vaults',headers=member).json()['items']
    assert next(item for item in vaults if item['id']==original)['quota'] == 1024**3
    assert next(item for item in vaults if item['id']==created)['quota'] == 31
    details=client.get('/sync/v1/admin/accounts/'+ids['member']+'?limit=1',headers=auth).json()
    assert details['vaults_next_before'] is not None and details['totals']['vault_count'] == 2
    assert client.get('/sync/v1/admin/accounts/'+ids['member'],headers=auth,params={'limit':1,'vault_before':details['vaults_next_before']}).json()['vaults'][0]['id'] != details['vaults'][0]['id']


def test_quota_changes_preserve_charged_objects_and_full_upload_reservations(admin):
    client,db,_,_,ids,auth,_,login,_=admin
    member,_=login('member')
    vault=client.post('/sync/v1/vaults',headers=member,json={'name':'quota'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    content=b'1234567890'; digest=hashlib.sha256(content).hexdigest()
    uploaded=client.post(base+'/uploads',headers=member,json={'content_hash':digest,'size':10}).json()['upload_id']
    assert client.put(base+'/uploads/'+uploaded+'?offset=0',headers=member,content=content).status_code == 200
    assert client.post(base+'/uploads/'+uploaded+'/complete',headers=member).status_code == 200
    pending=client.post(base+'/uploads',headers=member,json={'content_hash':'f'*64,'size':14}).json()['upload_id']
    overview=client.get('/sync/v1/admin/accounts/'+ids['member'],headers=auth).json()
    resource=next(item for item in overview['vaults'] if item['id']==vault)
    assert resource['used'] == 10 and resource['reserved_bytes'] == 14
    endpoint='/sync/v1/admin/accounts/'+ids['member']+'/vaults/'+vault+'/quota'
    refused=client.put(endpoint,headers=auth,json=operation(expected_quota=1024**3,quota=23))
    assert refused.json()['error']['code'] == 'QUOTA_IN_USE'
    assert refused.json()['error']['details'] == {'charged_bytes':10,'reserved_bytes':14}
    body=operation(expected_quota=1024**3,quota=24)
    reply=client.put(endpoint,headers=auth,json=body).json()
    assert reply['quota'] == 24 and client.put(endpoint,headers=auth,json=body).json() == reply
    assert client.put(endpoint,headers=auth,json=operation(expected_quota=1024**3,quota=100)).json()['error']['code'] == 'QUOTA_CHANGED'
    assert client.post(base+'/uploads',headers=member,json={'content_hash':'a'*64,'size':1}).status_code == 413
    assert client.get(base+'/uploads/'+pending,headers=member).status_code == 200
    assert client.get(base+'/objects/'+digest,headers=member).content == content
    wrong=endpoint.replace(ids['member'],ids['operator'])
    assert client.put(wrong,headers=auth,json=operation(expected_quota=24,quota=100)).status_code == 404


def test_device_revoke_binds_owner_and_unknown_outcome_has_readonly_receipt(admin):
    client,_,_,_,ids,auth,_,login,_=admin
    member,session=login('member')
    wrong='/sync/v1/admin/accounts/'+ids['admin']+'/devices/'+session['device_id']+'/revoke'
    assert client.post(wrong,headers=auth,json=operation()).status_code == 404
    endpoint='/sync/v1/admin/accounts/'+ids['member']+'/devices/'+session['device_id']+'/revoke'
    body=operation(); result=client.post(endpoint,headers=auth,json=body).json()
    assert result['revoked'] and client.post(endpoint,headers=auth,json=body).json() == result
    assert client.get('/sync/v1/admin/operations/'+body['operation_id'],headers=auth).json() == result
    assert client.get('/sync/v1/vaults',headers=member).status_code == 401
    assert client.post('/sync/v1/auth/refresh',json={'refresh_token':session['refresh_token']}).status_code == 401


def test_concurrent_account_creation_is_unique_without_duplicate_receipts(admin):
    client,db,_,_,_,auth,_,_,_=admin
    def create(_):
        return client.post('/sync/v1/admin/accounts',headers=auth,json=operation(username='only-one',password='controlled-unique-password',default_quota=20)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create,range(2))) == [200,409]
    with db.transaction() as conn:
        assert row(conn,"SELECT COUNT(*) AS n FROM users WHERE username='only-one'")['n'] == 1
        assert row(conn,"SELECT COUNT(*) AS n FROM admin_receipts WHERE kind='create_account'")['n'] == 1


@pytest.mark.parametrize('mode,code',[('integrity','OBJECT_STORAGE_INTEGRITY'),('storage','OBJECT_STORAGE_UNAVAILABLE'),('staging','STAGING_UNAVAILABLE')])
def test_diagnostics_classifies_failures_and_identifies_cached_observation(admin,monkeypatch,mode,code):
    client,_,store,_,_,auth,_,_,_=admin
    def fault(*args,**kwargs): raise OSError('/private/password/host never expose')
    if mode=='integrity': monkeypatch.setattr(store,'get',lambda key:b'wrong bytes')
    elif mode=='storage': monkeypatch.setattr(store,'put',fault)
    else:
        import sync_server.app as module
        monkeypatch.setattr(module.tempfile,'TemporaryFile',fault)
    first=client.get('/sync/v1/admin/diagnostics',headers=auth)
    data=first.json()['dependencies']
    assert data['code']==code and not data['cached'] and not data['ready']
    cached=client.get('/sync/v1/admin/diagnostics',headers=auth).json()['dependencies']
    assert cached['cached'] and cached['checked_at']==data['checked_at']
    assert '/private' not in first.text and 'password' not in first.text


def test_timeout_keeps_the_original_probe_live_and_does_not_report_previous_success(admin,monkeypatch):
    client,_,store,_,_,auth,_,_,app=admin
    entered=threading.Event(); release=threading.Event(); calls=[]
    real_put=store.put
    def slow(key,data):
        calls.append(key); entered.set(); release.wait(5); real_put(key,data)
    app.state.readiness.timeout=.03
    app.state.readiness.cache_seconds=0
    monkeypatch.setattr(store,'put',slow)
    try:
        first=client.get('/sync/v1/admin/diagnostics',headers=auth).json()['dependencies']
        assert entered.is_set() and first['code']=='DEPENDENCY_TIMEOUT' and first['probe_pending']
        second=client.get('/sync/v1/admin/diagnostics',headers=auth).json()['dependencies']
        assert not second['ready'] and len(calls)==1 and second['started_at']==first['started_at']
    finally:
        release.set()


def test_database_failure_is_sanitized_even_before_administration_authorization(admin,monkeypatch):
    client,db,_,_,_,auth,_,_,_=admin
    @contextmanager
    def unavailable():
        raise OperationalError('private-sql-password',{},OSError('private connection'))
        yield
    monkeypatch.setattr(db,'transaction',unavailable)
    response=client.get('/sync/v1/admin/access',headers=auth)
    assert response.status_code==503 and response.json()['error']['code']=='DATABASE_UNAVAILABLE'
    assert 'private' not in response.text


def test_lost_http_reply_is_reconciled_without_repeating_account_creation(admin):
    _,db,store,now,ids,auth,_,login,_=admin
    app=create_app(db,store,store.root.parent/'lost-stage',clock=lambda:now[0],operator_user_ids=[ids['operator']])
    writes=[]
    @app.middleware('http')
    async def lose_reply(request,call_next):
        response=await call_next(request)
        if request.method=='POST' and request.url.path=='/sync/v1/admin/accounts':
            writes.append(1)
            return JSONResponse({'error':{'code':'CONTROLLED_LOST_REPLY'}},status_code=503)
        return response
    body=operation(username='lost-reply-user',password='controlled-lost-reply-password',default_quota=20)
    with TestClient(app) as client:
        assert client.post('/sync/v1/admin/accounts',headers=auth,json=body).status_code==503
        result=client.get('/sync/v1/admin/operations/'+body['operation_id'],headers=auth).json()
        assert result['state']=='completed' and result['username']=='lost-reply-user'
        assert writes==[1]
    login(body['username'],body['password'])


def test_ordinary_account_cannot_provision_change_policy_or_revoke_another_device(admin):
    client,db,_,_,ids,auth,operator,login,_=admin
    ordinary,_=login('member')
    operations=[('POST','/accounts',operation(username='forbidden',password='controlled-forbidden-password',default_quota=1)),
                ('PUT','/accounts/'+ids['operator']+'/policy',operation(expected_revision=0,quota=1)),
                ('POST','/accounts/'+ids['operator']+'/devices/'+operator['device_id']+'/revoke',operation())]
    for method,path,body in operations:
        assert client.request(method,'/sync/v1/admin'+path,headers=ordinary,json=body).status_code==403
    with db.transaction() as conn: assert row(conn,'SELECT COUNT(*) AS n FROM admin_receipts')['n']==0
    assert client.get('/sync/v1/admin/access',headers=auth).status_code==200


def test_older_backups_without_management_or_reclamation_tables_remain_verifiable(tmp_path):
    from test_operations import backup
    from sync_server.operations import verify_backup
    source=tmp_path/'old-backup'; _,manifest=backup(source)
    for name in ['account_policy','admin_receipts','reclamation_plans','reclamation_objects']:
        del manifest['database_rows'][name]
    (source/'manifest.json').write_text(json.dumps(manifest),'utf-8')
    assert verify_backup(source)['verified_objects']==1
