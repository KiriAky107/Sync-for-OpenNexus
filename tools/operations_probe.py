"""Exercise real PostgreSQL/S3 recovery using newly owned loopback resources only."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
from urllib.parse import urlsplit
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from sync_server.app import create_app
from sync_server.database import Database
from sync_server.operation_journal import OperationJournal
from sync_server.storage import S3Objects


class ProbeFailure(RuntimeError):
    pass


def configuration(environ):
    """Require dedicated local services; never accept ordinary production settings."""
    if environ.get('SYNC_OPERATIONS_PROBE') != '1':
        raise ProbeFailure('EXPLICIT_PROBE_REQUIRED')
    url = make_url(environ.get('SYNC_OPERATIONS_PROBE_DATABASE_URL', ''))
    endpoint = environ.get('SYNC_OPERATIONS_PROBE_S3_ENDPOINT', '')
    parsed = urlsplit(endpoint)
    loopback = {'127.0.0.1', 'localhost', '::1'}
    if url.drivername != 'postgresql+psycopg' or url.host not in loopback or url.database != 'postgres' or url.query:
        raise ProbeFailure('DEDICATED_LOOPBACK_DATABASE_REQUIRED')
    if parsed.scheme != 'http' or parsed.hostname not in loopback or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        raise ProbeFailure('DEDICATED_LOOPBACK_OBJECT_SERVICE_REQUIRED')
    if not environ.get('AWS_ACCESS_KEY_ID') or not environ.get('AWS_SECRET_ACCESS_KEY'):
        raise ProbeFailure('PROBE_OBJECT_CREDENTIALS_REQUIRED')
    return url, endpoint


def checked(client, method, path, *, status=200, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code != status:
        raise ProbeFailure('UNEXPECTED_HTTP_STATUS')
    return response.json() if status != 204 else None


def cli(repo, work, journal, url, endpoint, bucket, command, *args, expected=None):
    env = os.environ.copy()
    env.update(SYNC_DATABASE_URL=url.render_as_string(hide_password=False),
               SYNC_S3_ENDPOINT=endpoint, SYNC_S3_BUCKET=bucket,
               SYNC_STAGING_DIR=str(work/'staging'), SYNC_OPERATIONS_PATH=str(journal))
    response = subprocess.run([sys.executable, '-m', 'sync_server', command, *args],
                              cwd=repo, env=env, capture_output=True, timeout=120)
    if expected is not None:
        records = OperationJournal(journal).records()['items']
        if response.returncode == 0 or not records or records[0]['state'] != 'failed' or records[0]['code'] != expected:
            raise ProbeFailure('EXPECTED_FAILURE_RECEIPT_MISSING')
        return records[0]
    if response.returncode != 0:
        # Captured CLI stderr may contain connection details. Never emit it.
        raise ProbeFailure('CLI_OPERATION_FAILED')
    return json.loads(response.stdout)


def served_protocol(repo, work, journal, url, endpoint, bucket, base, auth, expected_ids):
    """Run only this probe's production entrypoint and restored database/bucket."""
    staging = work/'served-staging'
    staging.mkdir(exist_ok=False)
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.update(SYNC_DATABASE_URL=url.render_as_string(hide_password=False), SYNC_S3_ENDPOINT=endpoint,
               SYNC_S3_BUCKET=bucket, SYNC_STAGING_DIR=str(staging), SYNC_OPERATIONS_PATH=str(journal), SYNC_HOST='127.0.0.1')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env['SYNC_PORT'] = str(port)
    child = subprocess.Popen([sys.executable, '-m', 'sync_server', 'serve', '--workers', '1'], cwd=repo, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    def request(path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        try:
            connection.request('GET', path, headers=headers or {})
            response = connection.getresponse()
            value = json.loads(response.read(1024*1024))
            return response.status, value
        finally:
            connection.close()
    try:
        until = time.monotonic()+30
        while True:
            if child.poll() is not None:
                raise ProbeFailure('OWNED_PRODUCTION_SERVER_EXITED')
            try:
                status, value = request('/ready')
                if status == 200 and value.get('schema') == 1:
                    break
            except OSError:
                pass
            if time.monotonic() >= until:
                raise ProbeFailure('OWNED_PRODUCTION_SERVER_NOT_READY')
            time.sleep(.2)
        status, changes = request(base+'/changes', auth)
        if status != 200 or changes['cursor'] != 3 or [item['file_id'] for item in changes['items']] != expected_ids:
            raise ProbeFailure('SERVED_RESTORED_IDENTITY_MISMATCH')
        status, handshake = request('/sync/v1/handshake')
        if status != 200 or handshake['protocol'] != 1 or handshake['encryption'] != 'transport-only' or handshake['features']['execution'] is not False:
            raise ProbeFailure('SERVED_PROTOCOL_MISMATCH')
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
    return {'owned_server_stopped': child.poll() is not None, 'exit_code': child.returncode}


def probe(work: Path):
    url, endpoint = configuration(os.environ)
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    nonce = uuid.uuid4().hex
    (work/'owner.json').write_text(json.dumps({'schema': 1, 'owner': nonce})+'\n', 'utf-8')
    repo = Path(__file__).resolve().parents[1]
    journal = work/'operations.sqlite3'
    admin = create_engine(url, isolation_level='AUTOCOMMIT', connect_args={'connect_timeout': 2})
    databases, buckets, engines = [], [], []
    report = {'schema': 1, 'passed': False, 'providers': ['PostgreSQL', 'S3'], 'checks': [], 'cleanup_complete': False}
    stage = 'dependency_readiness'
    try:
        deadline = time.monotonic()+60
        while True:
            try:
                with admin.connect() as conn:
                    conn.execute(text('SELECT 1'))
                S3Objects(endpoint, 'onx-probe-'+nonce).client.list_buckets()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise ProbeFailure('PROBE_DEPENDENCIES_UNAVAILABLE') from None
                time.sleep(1)

        def database(role):
            name = 'onx_ops_'+nonce+'_'+role
            with admin.connect() as conn:
                conn.exec_driver_sql('CREATE DATABASE "'+name+'"')
            databases.append(name)
            target_url = url.set(database=name)
            target = Database(target_url.render_as_string(hide_password=False))
            engines.append(target.engine)
            return target, target_url

        def bucket(role):
            target = S3Objects(endpoint, 'onx-ops-'+nonce+'-'+role)
            if not target.ensure_bucket():
                raise ProbeFailure('PROBE_BUCKET_ALREADY_EXISTS')
            buckets.append(target)
            return target

        source, source_url = database('source')
        restored, restored_url = database('restored')
        blocked, blocked_url = database('blocked')
        damaged, damaged_url = database('damaged')
        original_objects, restored_objects = bucket('source'), bucket('restored')
        blocked_objects, damaged_objects = bucket('blocked'), bucket('damaged')
        source.migrate()
        source.add_user('probe-owner', 'controlled-probe-password')
        source.add_user('probe-other', 'controlled-probe-password')
        files = [
            ('experiments/课程#1%2F.py', 'print("中文")\r\n'.encode()),
            ('experiments/输入.json', '{"课程":"数据","值":7}\r\n'.encode()),
            ('experiments/结果.csv', '名称,数量\r\n中文,7\r\n'.encode()),
        ]
        stage = 'real_provider_protocol'
        with TestClient(create_app(source, original_objects, work/'source-staging')) as client:
            if checked(client, 'GET', '/ready')['schema'] != 1:
                raise ProbeFailure('DEPENDENCIES_NOT_READY')
            sessions = [checked(client, 'POST', '/sync/v1/auth/sessions', json={
                'username': 'probe-owner', 'password': 'controlled-probe-password', 'device_name': 'probe-'+str(i)
            }) for i in range(2)]
            auth = {'Authorization': 'Bearer '+sessions[0]['access_token']}
            vault_id = checked(client, 'POST', '/sync/v1/vaults', headers=auth, json={'name': '实验校验'})['vault_id']
            base = '/sync/v1/vaults/'+vault_id
            ids = []
            for name, content in files:
                sha = hashlib.sha256(content).hexdigest()
                upload = checked(client, 'POST', base+'/uploads', headers=auth, json={'content_hash': sha, 'size': len(content)})
                path = base+'/uploads/'+upload['upload_id']
                # Confirmed offsets and completion receipts are persisted by PostgreSQL.
                middle = len(content)//2
                checked(client, 'PUT', path+'?offset=0', headers=auth, content=content[:middle])
                if checked(client, 'GET', path, headers=auth)['offset'] != middle:
                    raise ProbeFailure('CONFIRMED_OFFSET_CHANGED')
                checked(client, 'PUT', path+'?offset='+str(middle), headers=auth, content=content[middle:])
                completed = checked(client, 'POST', path+'/complete', headers=auth)
                if checked(client, 'POST', path+'/complete', headers=auth) != completed:
                    raise ProbeFailure('COMPLETION_RECEIPT_CHANGED')
                file_id = uuid.uuid4().hex
                ids.append(file_id)
                body = dict(operation_id=uuid.uuid4().hex, file_id=file_id, base_revision=0,
                            path=name, operation='put', content_hash=sha, size=len(content))
                committed = checked(client, 'POST', base+'/revisions', headers=auth, json=body)
                if checked(client, 'POST', base+'/revisions', headers=auth, json=body) != committed:
                    raise ProbeFailure('REVISION_RECEIPT_CHANGED')
            checked(client, 'DELETE', '/sync/v1/devices/'+sessions[1]['device_id'], headers=auth, status=204)
        report['checks'].append('confirmed_upload_and_revision_receipts')

        stage = 'backup_verify_restore'
        snapshot = work/'snapshot'
        common = (repo, work, journal)
        source_settings = (source_url, endpoint, original_objects.bucket)
        first = cli(*common, *source_settings, 'backup', '--directory', str(snapshot), '--io-workers', '2')
        verified = cli(*common, *source_settings, 'verify-backup', '--directory', str(snapshot), '--io-workers', '2')
        result = cli(*common, restored_url, endpoint, restored_objects.bucket, 'restore', '--directory', str(snapshot), '--io-workers', '2')
        if first['object_count'] != 3 or verified['verified_objects'] != 3 or result['verified_objects'] != 3:
            raise ProbeFailure('RESTORED_OBJECT_COUNT_CHANGED')
        second_snapshot = work/'restored-snapshot'
        cli(*common, restored_url, endpoint, restored_objects.bucket, 'backup', '--directory', str(second_snapshot))
        if (snapshot/'database.jsonl').read_bytes() != (second_snapshot/'database.jsonl').read_bytes():
            raise ProbeFailure('RESTORED_DATABASE_ROWS_CHANGED')
        report['checks'].append('all_database_tables_and_object_hashes_restored')

        stage = 'restored_protocol_and_authority'
        with TestClient(create_app(restored, restored_objects, work/'restored-staging')) as client:
            changes = checked(client, 'GET', base+'/changes', headers=auth)
            if changes['cursor'] != 3 or [x['file_id'] for x in changes['items']] != ids:
                raise ProbeFailure('RESTORED_CURSOR_OR_IDENTITY_CHANGED')
            for name, content in files:
                sha = hashlib.sha256(content).hexdigest()
                response = client.get(base+'/objects/'+sha, headers=auth)
                if response.status_code != 200 or response.content != content:
                    raise ProbeFailure('RESTORED_DOWNLOAD_CHANGED')
            if client.get('/sync/v1/vaults', headers={'Authorization': 'Bearer '+sessions[1]['access_token']}).status_code != 401:
                raise ProbeFailure('RESTORED_DEVICE_REVOCATION_LOST')
            other = checked(client, 'POST', '/sync/v1/auth/sessions', json={
                'username': 'probe-other', 'password': 'controlled-probe-password', 'device_name': 'other'})
            if client.get(base+'/changes', headers={'Authorization': 'Bearer '+other['access_token']}).status_code != 404:
                raise ProbeFailure('RESTORED_VAULT_AUTHORITY_LOST')
        report['checks'].append('restored_ids_cursors_bytes_revocation_and_vault_isolation')

        stage = 'actual_production_http_entrypoint'
        report['served_protocol'] = served_protocol(repo, work, journal, restored_url, endpoint, restored_objects.bucket, base, auth, ids)
        report['checks'].append('actual_production_serve_readiness_handshake_and_restored_identity')

        stage = 'protected_reclamation_with_real_providers'
        gc_backup = protected_reclamation(repo, work, journal, source, original_objects,
                                          source_url, endpoint, vault_id, base, auth, files)
        report['checks'].append('reviewed_gc_preserves_history_and_recovers_lost_s3_delete_reply')

        stage = 'empty_target_and_corruption_gates'
        cli(*common, *source_settings, 'restore', '--directory', str(snapshot), expected='RESTORE_DATABASE_NOT_EMPTY')
        sentinel = b'owned-blocked-target'
        blocked_objects.put('sentinel', sentinel)
        cli(*common, blocked_url, endpoint, blocked_objects.bucket, 'restore', '--directory', str(snapshot), expected='RESTORE_BUCKET_NOT_EMPTY')
        if blocked_objects.get('sentinel') != sentinel:
            raise ProbeFailure('NONEMPTY_BUCKET_CHANGED')
        with blocked.engine.connect() as conn:
            if conn.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")).scalar_one() != 0:
                raise ProbeFailure('REJECTED_RESTORE_CHANGED_DATABASE')
        sha = hashlib.sha256(files[0][1]).hexdigest()
        target = snapshot/'objects'/vault_id/sha
        original = target.read_bytes()
        target.write_bytes(b'corrupt-owned-backup')
        try:
            cli(*common, *source_settings, 'verify-backup', '--directory', str(snapshot), expected='BACKUP_OBJECT_INTEGRITY_FAILED')
            cli(*common, damaged_url, endpoint, damaged_objects.bucket, 'restore', '--directory', str(snapshot), expected='BACKUP_OBJECT_INTEGRITY_FAILED')
            if not damaged_objects.is_empty():
                raise ProbeFailure('CORRUPT_RESTORE_WROTE_OBJECTS')
            with damaged.engine.connect() as conn:
                if conn.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")).scalar_one() != 0:
                    raise ProbeFailure('CORRUPT_RESTORE_WROTE_DATABASE')
        finally:
            target.write_bytes(original)
        report['checks'].append('nonempty_targets_and_corruption_rejected_without_writes')
        from sync_server.operations import restore_backup
        recovered = restore_backup(damaged, damaged_objects, gc_backup, workers=2)
        if recovered['verified_objects'] != 4:
            raise ProbeFailure('GC_BACKUP_RECOVERY_COUNT_CHANGED')
        report['checks'].append('pre_gc_backup_recovers_all_four_objects_into_empty_providers')
        records = cli(*common, *source_settings, 'operation-records')['items']
        if len(records) != 8 or sum(item['state'] == 'failed' for item in records) != 4:
            raise ProbeFailure('DURABLE_CLI_RECEIPTS_CHANGED')
        report.update(passed=True, object_count=3, object_bytes=sum(len(x[1]) for x in files),
                      operation_receipts=len(records), failed_receipts=4, execution=False)
    except Exception as error:
        report.update(failure_stage=stage, failure_type=type(error).__name__)
        if isinstance(error, ProbeFailure):
            report['failure_code'] = str(error)
    finally:
        cleanup_errors = 0
        for engine in engines:
            engine.dispose()
        for store in buckets:
            try:
                pages = store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket)
                for page in pages:
                    store.delete_many([item['Key'] for item in page.get('Contents', [])])
                store.delete_bucket()
            except Exception:
                cleanup_errors += 1
        for name in databases:
            try:
                with admin.connect() as conn:
                    conn.exec_driver_sql('DROP DATABASE "'+name+'"')
            except Exception:
                cleanup_errors += 1
        admin.dispose()
        report['cleanup_complete'] = cleanup_errors == 0
        report['owned_database_count'] = len(databases)
        report['owned_bucket_count'] = len(buckets)
        if cleanup_errors:
            report['passed'] = False
        (work/'result.json').write_text(json.dumps(report, indent=2)+'\n', 'utf-8')
    return report


def protected_reclamation(repo, work, journal, db, objects, url, endpoint, vault_id, base, auth, files):
    """Only caller-owned database/bucket; real S3 delete with a lost local reply."""
    from sync_server.operations import create_backup
    from sync_server.reclamation import apply
    content = 'unreferenced recovery bytes 中文\r\n'.encode()
    sha = hashlib.sha256(content).hexdigest()
    with TestClient(create_app(db, objects, work/'gc-staging')) as client:
        info = checked(client, 'POST', base+'/uploads', headers=auth, json={'content_hash':sha,'size':len(content)})
        path = base+'/uploads/'+info['upload_id']
        checked(client,'PUT',path+'?offset=0',headers=auth,content=content)
        checked(client,'POST',path+'/complete',headers=auth)
        old = int(time.time())-8*86400
        with db.transaction() as conn:
            conn.execute(text('UPDATE objects SET created=:old WHERE vault_id=:v AND hash=:h'),{'old':old,'v':vault_id,'h':sha})
            conn.execute(text('UPDATE upload_receipts SET completed=:old WHERE vault_id=:v AND hash=:h'),{'old':old,'v':vault_id,'h':sha})
        backup = work/'pre-gc-snapshot'
        create_backup(db,objects,backup,workers=2)
        settings = (repo,work,journal,url,endpoint,objects.bucket)
        plan = cli(*settings,'gc-preview','--vault-id',vault_id,'--directory',str(backup))
        if [item['hash'] for item in plan['preview']['candidates']] != [sha] or plan['preview']['protected_counts'].get('revision') != 3:
            raise ProbeFailure('GC_REFERENCE_PROTECTION_CHANGED')
        real_delete=objects.delete
        def lose_delete_reply(key):
            real_delete(key)
            raise OSError('controlled lost S3 delete reply')
        objects.delete=lose_delete_reply
        try:
            pending=apply(db,objects,backup,plan['plan_id'],confirm_plan=plan['plan_id'])
        finally:
            objects.delete=real_delete
        if pending['state'] != 'running' or pending['pending_objects'] != 1:
            raise ProbeFailure('GC_DURABLE_INTENTION_MISSING')
        if client.get(base+'/objects/'+sha,headers=auth).status_code != 503:
            raise ProbeFailure('GC_PENDING_OBJECT_NOT_GUARDED')
        original=cli(*settings,'gc-status','--plan-id',plan['plan_id'])
        if original['pending_objects'] != 1:
            raise ProbeFailure('GC_READONLY_RECONCILIATION_CHANGED')
        result=cli(*settings,'gc-apply','--directory',str(backup),'--plan-id',plan['plan_id'],'--confirm-plan',plan['plan_id'])
        if result['state'] != 'complete' or result['reclaimed_objects'] != 1 or result['reclaimed_bytes'] != len(content):
            raise ProbeFailure('GC_RESUME_RESULT_CHANGED')
        if checked(client,'GET',base+'/uploads/'+info['upload_id']+'/result',headers=auth)['state'] != 'reclaimed':
            raise ProbeFailure('GC_COMPLETION_RESULT_MISLEADING')
        if checked(client,'GET',base+'/usage',headers=auth)['charged_bytes'] != sum(len(body) for _,body in files):
            raise ProbeFailure('GC_QUOTA_LEDGER_CHANGED')
        for _,body in files:
            digest=hashlib.sha256(body).hexdigest()
            if objects.get(vault_id+'/'+digest) != body:
                raise ProbeFailure('GC_REMOVED_REVISION_OBJECT')
        if (backup/'objects'/vault_id/sha).read_bytes() != content:
            raise ProbeFailure('GC_RECOVERY_BYTES_CHANGED')
        if checked(client,'GET',base+'/changes',headers=auth)['cursor'] != 3:
            raise ProbeFailure('GC_PRUNED_REVISION_CURSOR')
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = probe(args.work_dir)
    except Exception as error:
        print('PRODUCTION_OPERATIONS_PROBE_REJECTED '+type(error).__name__)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
