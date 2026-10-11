"""Real cleanup boundaries, immutable observations, precise reminders and redaction."""
import json
import uuid
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from sync_server.app import create_app
from sync_server.database import Database, row, rows, run
from sync_server.storage import DiskObjects
from sync_server.maintenance import cleanup_expired_uploads, HISTORY_LIMIT
from sync_server.operation_journal import OperationJournal
from sync_server.observability import redacted


@pytest.fixture
def observed(tmp_path):
    db=Database('sqlite:///'+str(tmp_path/'db.sqlite3')); db.migrate()
    for name in ('operator','member'): db.add_user(name,'owned-observability-password')
    with db.transaction() as conn: ids={u['username']:u['id'] for u in rows(conn,'SELECT id,username FROM users')}
    objects=DiskObjects(tmp_path/'objects'); now=[1_000_000.5]; journal=tmp_path/'operations.sqlite3'
    app=create_app(db,objects,tmp_path/'staging',clock=lambda:now[0],operations_path=journal,operator_user_ids=[ids['operator']])
    with TestClient(app) as client:
        def login(name):
            response=client.post('/sync/v1/auth/sessions',json={'username':name,'password':'owned-observability-password','device_name':'owned observation'})
            assert response.status_code==200
            session=response.json(); return {'Authorization':'Bearer '+session['access_token']},session
        auth,session=login('operator')
        yield client,db,objects,now,journal,auth,session,login,tmp_path
    db.engine.dispose()


def history(conn, count, finished=99):
    for index in range(count):
        run(conn,"INSERT INTO maintenance_history VALUES (:id,'uploads',:time,:time,0,1,1,7,0,0)",id=f'{index:032x}',time=finished)


def test_cleanup_keeps_only_meaningful_recent_runs_and_preserves_idle_summary(observed,monkeypatch):
    client,db,_,now,_,auth,session,_,tmp=observed
    vault=client.post('/sync/v1/vaults',headers=auth,json={'name':'owned cleanup'}).json()['vault_id']
    upload=uuid.uuid4().hex; staging=tmp/'staging'; (staging/upload).write_bytes(b'partial')
    with db.transaction() as conn:
        history(conn,HISTORY_LIMIT)
        run(conn,'INSERT INTO uploads VALUES (:id,:v,:d,:hash,7,3,:expires)',id=upload,v=vault,d=session['device_id'],hash='a'*64,expires=int(now[0])-1)
    original=Path.unlink
    def fail_owned(path,*args,**kwargs):
        if path==staging/upload: raise OSError('owned private staging path')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail_owned)
    failed=cleanup_expired_uploads(db,staging,now=int(now[0]))
    assert failed['filesystem_failures']==1 and (staging/upload).read_bytes()==b'partial'
    with db.read_only() as conn:
        assert row(conn,'SELECT COUNT(*) AS n FROM maintenance_history')['n']==HISTORY_LIMIT
        assert row(conn,'SELECT 1 FROM uploads WHERE id=:id',id=upload)
    monkeypatch.setattr(Path,'unlink',original)
    now[0]+=1; retried=cleanup_expired_uploads(db,staging,now=int(now[0]))
    assert retried['expired_uploads_removed']==1 and retried['released_bytes']==7
    page=client.get('/sync/v1/admin/observability',headers=auth).json()
    assert page['history']['items'][0]['removed']==1 and page['history']['items'][1]['filesystem_failures']==1
    with db.read_only() as conn: before=rows(conn,'SELECT * FROM maintenance_history ORDER BY id')
    cleanup_expired_uploads(db,staging,now=int(now[0])+1)
    with db.read_only() as conn:
        assert rows(conn,'SELECT * FROM maintenance_history ORDER BY id')==before
        assert row(conn,"SELECT * FROM maintenance_summary WHERE kind='uploads'")['total_removed']==1
        assert row(conn,"SELECT * FROM maintenance_summary WHERE kind='uploads'")['total_failures']==1


def test_history_pages_are_bounded_reverse_and_new_rows_do_not_shift_old_cursor(observed):
    client,db,_,_,_,auth,*_=observed
    with db.transaction() as conn: history(conn,23)
    first=client.get('/sync/v1/admin/observability?limit=20',headers=auth).json()
    assert len(first['history']['items'])==20 and first['history']['retention_limit']==1000
    with db.transaction() as conn: run(conn,"INSERT INTO maintenance_history VALUES (:id,'uploads',101,101,0,1,1,1,0,0)",id='f'*32)
    last=client.get('/sync/v1/admin/observability',headers=auth,params={'before':first['history']['next_before']}).json()
    assert len(last['history']['items'])==3 and last['history']['next_before'] is None
    assert len({r['id'] for r in first['history']['items']+last['history']['items']})==23
    for params in ({'limit':101},{'before':'not-a-cursor'},{'before':'9999999999999999:'+'a'*32}):
        assert client.get('/sync/v1/admin/observability',headers=auth,params=params).status_code==422


def test_reads_export_permissions_and_expiry_leave_activity_rows_and_object_io_unchanged(observed,monkeypatch):
    client,db,objects,_,_,auth,session,login,_=observed
    ordinary,_=login('member')
    def forbidden_io(*args,**kwargs): raise AssertionError('Observation called object IO')
    monkeypatch.setattr(objects,'get',forbidden_io);monkeypatch.setattr(objects,'put',forbidden_io)
    with db.read_only() as conn: previous=rows(conn,'SELECT * FROM device_activity ORDER BY device_id')
    statements=[]
    def executed(conn,cursor,statement,params,context,many): statements.append(statement.strip().upper())
    event.listen(db.engine,'before_cursor_execute',executed)
    try:
        for route in ('/sync/v1/admin/observability','/sync/v1/admin/observability/export'):
            ok=client.get(route,headers=auth)
            assert ok.status_code==200 and ok.headers['cache-control']=='private, no-store'
            assert client.get(route,headers=ordinary).status_code==403
        assert not any(s.startswith(('INSERT','UPDATE','DELETE','BEGIN IMMEDIATE')) or 'FOR UPDATE' in s for s in statements)
    finally: event.remove(db.engine,'before_cursor_execute',executed)
    with db.read_only() as conn: assert rows(conn,'SELECT * FROM device_activity ORDER BY device_id')==previous
    with db.transaction() as conn: run(conn,'UPDATE sessions SET expires=0 WHERE device_id=:id',id=session['device_id'])
    assert client.get('/sync/v1/admin/observability',headers=auth).status_code==401


def test_capacity_includes_active_reservations_ignores_expired_and_handles_zero_quota_exactly(observed):
    client,db,_,now,_,auth,session,_,_=observed
    with db.transaction() as conn:
        user=row(conn,'SELECT user_id FROM devices WHERE id=:id',id=session['device_id'])['user_id']
        for index,(quota,used,reserved,expires) in enumerate([(100,79,1,int(now[0])+1),(100,60,40,int(now[0])),(10,10,0,0),(0,0,0,0),(100,150,0,0)]):
            vault=f'{index:032x}'
            run(conn,'INSERT INTO vaults VALUES (:id,:user,:name,0,:quota,:used)',id=vault,user=user,name='private fixture vault',quota=quota,used=used)
            if reserved: run(conn,'INSERT INTO uploads VALUES (:id,:vault,:device,:hash,:size,0,:expires)',id=uuid.uuid4().hex,vault=vault,device=session['device_id'],hash='a'*64,size=reserved,expires=expires)
    capacity=client.get('/sync/v1/admin/observability',headers=auth).json()['capacity']
    assert capacity=={'vaults':5,'near_quota':1,'blocked':3,'warning_percent':80,'basis':'charged_objects_and_active_upload_reservations'}


def test_backup_reminders_are_separate_receipts_not_archive_verification(observed,monkeypatch):
    client,_,_,now,path,auth,*_=observed
    clock=[int((now[0]-86401)*1000)]
    monkeypatch.setattr('sync_server.operation_journal.time.time_ns',lambda:clock[0]*1_000_000)
    journal=OperationJournal(path)
    journal.run('backup',lambda:{'status':'BACKUP_COMPLETE','object_count':1,'object_bytes':7})
    clock[0]=int((now[0]-.1)*1000)
    journal.run('verify',lambda:{'status':'BACKUP_VERIFIED','verified_objects':1,'backup_age_seconds':90000})
    journal.start('restore')
    page=client.get('/sync/v1/admin/observability',headers=auth).json()
    receipts=page['backup_receipts']
    assert receipts['backup']['state']=='stale_receipt' and receipts['verify']['state']=='recent_receipt'
    assert receipts['verify']['recorded_backup_age_seconds']==90000
    assert receipts['unfinished_operations']==1 and receipts['archive_association_known'] is False
    now[0]-=90001
    assert client.get('/sync/v1/admin/observability',headers=auth).json()['backup_receipts']['backup']['state']=='clock_ahead'


def test_missing_journal_is_unknown_without_creation_and_corruption_is_redacted(observed):
    client,_,_,_,path,auth,*_=observed
    assert client.get('/sync/v1/admin/observability',headers=auth).json()['backup_receipts']['backup'] is None
    assert not path.exists()
    path.write_bytes(b'owned invalid journal includes private local path')
    result=client.get('/sync/v1/admin/observability/export',headers=auth)
    assert result.status_code==503 and 'private local path' not in result.text and str(path) not in result.text


def test_export_whitelist_excludes_identifiers_and_extra_nested_data(observed,monkeypatch):
    client,db,_,now,path,auth,session,*_=observed
    monkeypatch.setattr('sync_server.operation_journal.time.time_ns',lambda:int(now[0]*1_000_000_000))
    OperationJournal(path).run('backup',lambda:{'status':'BACKUP_COMPLETE','object_count':2,'object_bytes':7})
    with db.transaction() as conn: history(conn,1)
    report=client.get('/sync/v1/admin/observability',headers=auth).json()
    report['private_path']='C:/private-owned-fixture'
    report['backup_receipts']['backup']['credential']='owned-secret'
    report['history']['items'][0]['content']='owned-content'
    result=redacted(report); encoded=json.dumps(result)
    assert result['redacted'] and result['cleanup_page'][0]['removed']==1
    assert not any(value in encoded for value in ('owned-secret','owned-content','private-owned-fixture',session['device_id'],report['history']['items'][0]['id']))
    actual=client.get('/sync/v1/admin/observability/export',headers=auth)
    assert actual.json()==result and actual.headers['cache-control']=='private, no-store'
