"""Real recovery bytes, durable intentions and protocol guards for reviewed GC."""
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys
import uuid

import pytest

from sync_server.database import row, run
from sync_server.operations import (OperationsError, _manifest_objects,
                                    _write_database_snapshot, sha256_file, verify_backup)
from sync_server.reclamation import apply, preview, status
from test_protocol import env, setup, session, upload, change


def snapshot(db, store, source):
    source.mkdir()
    with db.transaction() as conn:
        catalog = _manifest_objects(conn)
        counts = _write_database_snapshot(conn, source/'database.jsonl')
    for item in catalog:
        target = source/'objects'/item['vault_id']/item['hash']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(store.get(item['vault_id']+'/'+item['hash']))
    manifest = dict(schema=1, created_utc=datetime.now(timezone.utc).isoformat(),
                    database_sha256=sha256_file(source/'database.jsonl'), database_rows=counts,
                    object_count=len(catalog), object_bytes=sum(item['size'] for item in catalog), objects=catalog)
    (source/'manifest.json').write_text(json.dumps(manifest), 'utf-8')
    assert verify_backup(source)['verified_objects'] == len(catalog)
    return manifest


def aged(env, tmp_path, *, commit=False):
    client, db, store, now = env
    auth, original_session, base = setup(client)
    content = b'old uncommitted upload bytes \xe4\xb8\xad\xe6\x96\x87'
    sha = upload(client, base, auth, content)
    if commit:
        assert client.post(base+'/revisions', headers=auth, json=change(sha,size=len(content))).status_code == 200
    source = tmp_path/'backup'
    snapshot(db, store, source)
    now[0] += 8*86400
    renewed = client.post('/sync/v1/auth/refresh',json={'refresh_token':original_session['refresh_token']})
    assert renewed.status_code == 200
    auth = {'Authorization':'Bearer '+renewed.json()['access_token']}
    return client, db, store, now, auth, base, content, sha, source


def test_reviewed_reclamation_preserves_indefinite_history_and_old_receipts(env, tmp_path):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path)
    vault = base.rsplit('/',1)[-1]
    original_backup = (source/'objects'/vault/sha).read_bytes()
    with db.transaction() as conn:
        receipt = dict(row(conn,'SELECT * FROM upload_receipts WHERE hash=:h',h=sha))
    plan = preview(db,source,vault,now=now[0])
    assert plan['state'] == 'preview' and len(plan['preview']['candidates']) == 1
    assert store.get(vault+'/'+sha) == content
    with pytest.raises(OperationsError,match='GC_CONFIRMATION_REQUIRED'):
        apply(db,store,source,plan['plan_id'],confirm_plan='wrong',now=now[0])
    result = apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert result['state'] == 'complete' and result['reclaimed_bytes'] == len(content)
    assert not (store.root/vault/sha).exists()
    assert apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id']) == result
    assert client.get(base+'/usage',headers=auth).json()['charged_bytes'] == 0
    assert client.get(base+'/uploads/'+receipt['id']+'/result',headers=auth).json()['state'] == 'reclaimed'
    assert client.post(base+'/uploads/'+receipt['id']+'/complete',headers=auth).json()['error']['code'] == 'OBJECT_RECLAIMED'
    # Completion receipts keep original device binding, as before GC.
    with db.transaction() as conn:
        device = row(conn,'SELECT token FROM sessions WHERE device_id=:d',d=receipt['device_id'])
        assert device is not None
        original = row(conn,'SELECT * FROM upload_receipts WHERE id=:id',id=receipt['id'])
        assert dict(original) == receipt
    assert (source/'objects'/vault/sha).read_bytes() == original_backup
    assert verify_backup(source)['verified_objects'] == 1
    assert client.get(base+'/retention',headers=auth).json()['history_retention'] == 'indefinite'


def test_all_history_including_deleted_files_is_protected(env,tmp_path):
    client, db, store, now = env
    auth, _, base = setup(client)
    first = b'history'; sha = upload(client,base,auth,first)
    body = change(sha,size=len(first))
    assert client.post(base+'/revisions',headers=auth,json=body).status_code == 200
    second = b'current'; current_hash = upload(client,base,auth,second)
    assert client.post(base+'/revisions',headers=auth,json={**body,'operation_id':uuid.uuid4().hex,'base_revision':1,'content_hash':current_hash,'size':len(second)}).status_code == 200
    assert client.post(base+'/revisions',headers=auth,json={**body,'operation_id':uuid.uuid4().hex,'base_revision':2,'operation':'delete','content_hash':None,'size':0}).status_code == 200
    source = tmp_path/'backup'; snapshot(db,store,source); now[0] += 8*86400
    vault = base.rsplit('/',1)[-1]
    plan = preview(db,source,vault,now=now[0])
    assert plan['preview']['candidates'] == [] and plan['preview']['protected_counts'] == {'revision':2}
    result = apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert result['reclaimed_bytes'] == 0
    auth, _ = session(client)
    assert client.get(base+'/changes',headers=auth).json()['cursor'] == 3
    assert client.get(base+'/objects/'+sha,headers=auth).content == first
    assert client.get(base+'/objects/'+current_hash,headers=auth).content == second


@pytest.mark.parametrize('reason',['upload','completion_receipt','grace_period','absent_from_backup'])
def test_each_protection_excludes_eligible_looking_objects(env,tmp_path,reason):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path)
    vault = base.rsplit('/',1)[-1]
    with db.transaction() as conn:
        device = row(conn,'SELECT device_id FROM upload_receipts WHERE hash=:h',h=sha)['device_id']
        if reason == 'upload':
            run(conn,'INSERT INTO uploads VALUES (:id,:v,:d,:h,:size,0,:expires)',id=uuid.uuid4().hex,v=vault,d=device,h=sha,size=len(content),expires=now[0]-1)
        elif reason == 'completion_receipt':
            run(conn,'UPDATE upload_receipts SET completed=:now WHERE hash=:h',now=now[0],h=sha)
        elif reason == 'grace_period':
            run(conn,'UPDATE objects SET created=:now WHERE hash=:h',now=now[0],h=sha)
    if reason == 'absent_from_backup':
        new_content=b'not in snapshot'; sha=upload(client,base,auth,new_content)
        with db.transaction() as conn:
            run(conn,'UPDATE objects SET created=0 WHERE hash=:h',h=sha)
            run(conn,'UPDATE upload_receipts SET completed=0 WHERE hash=:h',h=sha)
    plan = preview(db,source,vault,now=now[0])
    assert sha not in {item['hash'] for item in plan['preview']['candidates']}
    assert plan['preview']['protected_counts'][reason] == 1
    assert (store.root/vault/sha).exists()


@pytest.mark.parametrize('mutation',['revision','upload','ledger','expired'])
def test_preview_is_fixed_and_revalidates_before_any_delete(env,tmp_path,mutation):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path)
    vault = base.rsplit('/',1)[-1]; plan=preview(db,source,vault,now=now[0])
    expected='GC_PLAN_STALE'
    if mutation == 'revision':
        assert client.post(base+'/revisions',headers=auth,json=change(sha,size=len(content))).status_code == 200
    elif mutation == 'upload':
        with db.transaction() as conn:
            device=row(conn,'SELECT device_id FROM upload_receipts WHERE hash=:h',h=sha)['device_id']
            run(conn,'INSERT INTO uploads VALUES (:id,:v,:d,:h,:size,0,:expires)',id=uuid.uuid4().hex,v=vault,d=device,h=sha,size=len(content),expires=now[0]+100)
    elif mutation == 'ledger':
        with db.transaction() as conn: run(conn,'UPDATE vaults SET used=used+1 WHERE id=:v',v=vault)
        expected='GC_ACCOUNTING_MISMATCH'
    else:
        now[0] += 3600; expected='GC_PLAN_EXPIRED'
    with pytest.raises(OperationsError,match=expected):
        apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert store.get(vault+'/'+sha) == content
    assert status(db,plan['plan_id'])['items'] == []


@pytest.mark.parametrize('damage',['object','database','manifest'])
def test_changed_or_damaged_backup_cannot_authorize_deletion(env,tmp_path,damage):
    _, db, store, now, _, base, content, sha, source = aged(env,tmp_path)
    vault=base.rsplit('/',1)[-1]; plan=preview(db,source,vault,now=now[0])
    if damage == 'object': (source/'objects'/vault/sha).write_bytes(b'bad')
    elif damage == 'database': (source/'database.jsonl').write_bytes(b'bad')
    else: (source/'manifest.json').write_text('{}','utf-8')
    with pytest.raises(OperationsError):
        apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert store.get(vault+'/'+sha) == content and status(db,plan['plan_id'])['items'] == []


def test_lost_delete_reply_keeps_durable_guard_and_resumes_same_plan_once(env,tmp_path,monkeypatch):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path)
    vault=base.rsplit('/',1)[-1]; plan=preview(db,source,vault,now=now[0])
    real_delete=store.delete
    def lost_reply(key):
        real_delete(key)
        raise OSError('private/path/credential must never be persisted')
    monkeypatch.setattr(store,'delete',lost_reply)
    receipt=apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert receipt['state'] == 'running' and receipt['pending_objects'] == 1
    assert 'credential' not in json.dumps(receipt)
    assert not (store.root/vault/sha).exists()
    assert client.post(base+'/revisions',headers=auth,json=change(sha,size=len(content))).json()['error']['code'] == 'OBJECT_RECLAMATION_PENDING'
    assert client.get(base+'/objects/'+sha,headers=auth).status_code == 503
    assert client.post(base+'/uploads',headers=auth,json={'content_hash':sha,'size':len(content)}).status_code == 503
    with db.transaction() as conn:
        upload_id=row(conn,'SELECT id FROM upload_receipts WHERE hash=:h',h=sha)['id']
        assert row(conn,'SELECT used FROM vaults WHERE id=:v',v=vault)['used'] == len(content)
    assert client.get(base+'/uploads/'+upload_id+'/result',headers=auth).json()['state'] == 'reclamation_pending'
    assert client.post(base+'/uploads/'+upload_id+'/complete',headers=auth).status_code == 503
    usage=client.get(base+'/usage',headers=auth).json()
    assert usage['reclamation_pending_objects'] == 1 and usage['reclamation_pending_bytes'] == len(content)
    monkeypatch.setattr(store,'delete',real_delete)
    completed=apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert completed['reclaimed_bytes'] == len(content)
    assert client.get(base+'/usage',headers=auth).json()['charged_bytes'] == 0
    assert upload(client,base,auth,content) == sha
    assert client.get(base+'/uploads/'+upload_id+'/result',headers=auth).json()['state'] == 'completed'
    assert client.post(base+'/uploads/'+upload_id+'/complete',headers=auth).status_code == 200
    assert client.get(base+'/usage',headers=auth).json()['charged_bytes'] == len(content)


def test_process_death_after_physical_delete_keeps_original_intention(env,tmp_path):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path)
    vault=base.rsplit('/',1)[-1]; plan=preview(db,source,vault,now=now[0])
    script = '''import os, sys
from pathlib import Path
from sync_server.database import Database
from sync_server.storage import DiskObjects
from sync_server.reclamation import apply
db=Database(sys.argv[1]); store=DiskObjects(Path(sys.argv[2]))
real=store.delete
def die(key):
    real(key)
    os._exit(7)
store.delete=die
apply(db,store,Path(sys.argv[3]),sys.argv[4],confirm_plan=sys.argv[4],now=int(sys.argv[5]))
'''
    child=subprocess.run([sys.executable,'-c',script,str(db.engine.url),str(store.root),str(source),plan['plan_id'],str(now[0])],capture_output=True,timeout=30)
    assert child.returncode == 7
    receipt=status(db,plan['plan_id'])
    assert receipt['pending_objects'] == 1 and receipt['state'] == 'running'
    assert client.post(base+'/revisions',headers=auth,json=change(sha,size=len(content))).status_code == 503
    assert apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])['state'] == 'complete'
    assert client.get(base+'/usage',headers=auth).json()['charged_bytes'] == 0


def test_database_failure_after_delete_is_retryable_and_does_not_double_charge(env,tmp_path,monkeypatch):
    _, db, store, now, _, base, content, sha, source = aged(env,tmp_path)
    vault=base.rsplit('/',1)[-1]; plan=preview(db,source,vault,now=now[0])
    import sync_server.reclamation as gc
    real_run=gc.run
    def fail(conn,sql,**params):
        if sql.startswith('DELETE FROM objects'): raise RuntimeError('private database connection string')
        return real_run(conn,sql,**params)
    monkeypatch.setattr(gc,'run',fail)
    result=apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert result['pending_objects'] == 1 and result['reclaimed_bytes'] == 0
    monkeypatch.setattr(gc,'run',real_run)
    result=apply(db,store,source,plan['plan_id'],confirm_plan=plan['plan_id'],now=now[0])
    assert result['state'] == 'complete' and result['reclaimed_bytes'] == len(content)
    with db.transaction() as conn: assert row(conn,'SELECT used FROM vaults WHERE id=:v',v=vault)['used'] == 0


def test_preview_limits_and_competing_operator_plans_use_fixed_candidates(env,tmp_path):
    client, db, store, now, auth, base, _, _, source = aged(env,tmp_path)
    new_body=b'other orphan'; upload(client,base,auth,new_body)
    with db.transaction() as conn:
        run(conn,'UPDATE objects SET created=0')
        run(conn,'UPDATE upload_receipts SET completed=0')
    other=tmp_path/'second-backup'; snapshot(db,store,other)
    vault=base.rsplit('/',1)[-1]
    first=preview(db,other,vault,limit=1,now=now[0]); second=preview(db,other,vault,limit=1,now=now[0])
    assert first['preview']['eligible_objects'] == 2 and first['preview']['has_more']
    assert apply(db,store,other,first['plan_id'],confirm_plan=first['plan_id'],now=now[0])['reclaimed_objects'] == 1
    with pytest.raises(OperationsError,match='GC_PLAN_STALE'):
        apply(db,store,other,second['plan_id'],confirm_plan=second['plan_id'],now=now[0])
    with pytest.raises(OperationsError,match='GC_GRACE_INVALID'): preview(db,other,vault,grace_hours=0)
    with pytest.raises(OperationsError,match='GC_LIMIT_INVALID'): preview(db,other,vault,limit=101)


def test_retention_endpoint_is_owner_scoped_and_dangling_references_fail_closed(env,tmp_path):
    client, db, store, now, auth, base, content, sha, source = aged(env,tmp_path,commit=True)
    foreign,_=session(client,'bob')
    assert client.get(base+'/retention',headers=foreign).status_code == 404
    policy=client.get(base+'/retention',headers=auth).json()
    assert not policy['automatic_reclamation'] and policy['minimum_grace_hours'] == 24
    vault=base.rsplit('/',1)[-1]
    with db.transaction() as conn: run(conn,'DELETE FROM revisions WHERE vault_id=:v',v=vault)
    with pytest.raises(OperationsError,match='GC_REFERENCES_INCONSISTENT'):
        preview(db,source,vault,now=now[0])
