"""Exercise real PostgreSQL/S3 recovery using newly owned loopback resources only."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import shutil
import socket
import sys
import time
from urllib.parse import urlsplit
import uuid

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
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


def administration_seed(db, objects, work):
    """Provision using the actual operator API before the verified snapshot."""
    with db.engine.connect() as conn:
        operator = conn.execute(text("SELECT id FROM users WHERE username='probe-owner'")).scalar_one()
    app = create_app(db, objects, work/'admin-staging', operator_user_ids=[operator])
    writes = []
    @app.middleware('http')
    async def lose_creation_reply(request, call_next):
        response = await call_next(request)
        if request.method == 'POST' and request.url.path == '/sync/v1/admin/accounts':
            writes.append(request.url.path)
            if response.status_code == 200:
                return JSONResponse({'error':{'code':'CONTROLLED_LOST_REPLY'}}, status_code=503)
        return response
    with TestClient(app) as client:
        session = checked(client, 'POST', '/sync/v1/auth/sessions', json={
            'username':'probe-owner', 'password':'controlled-probe-password', 'device_name':'provisioning'})
        auth = {'Authorization':'Bearer '+session['access_token']}
        operation = uuid.uuid4().hex
        checked(client, 'POST', '/sync/v1/admin/accounts', headers=auth, status=503, json={
            'operation_id':operation, 'username':'provisioned-member',
            'password':'controlled-provisioned-password', 'default_quota':64})
        created = checked(client, 'GET', '/sync/v1/admin/operations/'+operation, headers=auth)
        if created['state'] != 'completed' or created['default_quota'] != 64 or len(writes) != 1:
            raise ProbeFailure('PROVISIONING_RECONCILIATION_FAILED')
        member = created['user_id']
        sessions = [checked(client, 'POST', '/sync/v1/auth/sessions', json={
            'username':'provisioned-member', 'password':'controlled-provisioned-password', 'device_name':'member-'+str(i)
        }) for i in range(2)]
        access = {'Authorization':'Bearer '+sessions[0]['access_token']}
        checked(client, 'GET', '/sync/v1/admin/accounts', headers=access, status=403)
        vault = checked(client, 'POST', '/sync/v1/vaults', headers=access, json={'name':'Provisioned vault'})['vault_id']
        current = checked(client, 'GET', '/sync/v1/admin/accounts/'+member, headers=auth)
        policy = checked(client, 'PUT', '/sync/v1/admin/accounts/'+member+'/policy', headers=auth,
                         json={'operation_id':uuid.uuid4().hex,'expected_revision':current['policy']['revision'],'quota':96})
        second = checked(client, 'POST', '/sync/v1/vaults', headers=access, json={'name':'Future policy vault'})['vault_id']
        base = '/sync/v1/vaults/'+vault
        pending = checked(client, 'POST', base+'/uploads', headers=access,
                          json={'content_hash':'f'*64,'size':20})['upload_id']
        target = '/sync/v1/admin/accounts/'+member+'/vaults/'+vault+'/quota'
        refusal = checked(client, 'PUT', target, headers=auth, status=409,
                          json={'operation_id':uuid.uuid4().hex,'expected_quota':64,'quota':19})
        if refusal['error']['code'] != 'QUOTA_IN_USE' or refusal['error']['details']['reserved_bytes'] != 20:
            raise ProbeFailure('PROVISIONED_RESERVATION_NOT_PROTECTED')
        quota = checked(client, 'PUT', target, headers=auth,
                        json={'operation_id':uuid.uuid4().hex,'expected_quota':64,'quota':20})
        checked(client, 'POST', base+'/uploads/'+pending+'/cancel', headers=access)
        revoked = checked(client, 'POST', '/sync/v1/admin/accounts/'+member+'/devices/'+sessions[1]['device_id']+'/revoke',
                          headers=auth, json={'operation_id':uuid.uuid4().hex})
        first = checked(client, 'GET', '/sync/v1/admin/diagnostics', headers=auth)['dependencies']
        cache = checked(client, 'GET', '/sync/v1/admin/diagnostics', headers=auth)['dependencies']
        if not first['ready'] or cache['code'] != 'READY' or not cache['cached'] or cache['checked_at'] != first['checked_at']:
            raise ProbeFailure('DIAGNOSTIC_OBSERVATION_CHANGED')
    return {'operator':operator, 'member':member, 'sessions':sessions, 'vault':vault, 'future_vault':second,
            'receipts':[created,policy,quota,revoked]}


def administration_restored(db, objects, work, original):
    """Check restored policies/receipts/revocation, then sync fresh actual bytes."""
    app = create_app(db, objects, work/'restored-admin-staging', operator_user_ids=[original['operator']])
    with TestClient(app) as client:
        operator = checked(client, 'POST', '/sync/v1/auth/sessions', json={
            'username':'probe-owner', 'password':'controlled-probe-password', 'device_name':'restored-provisioning'})
        auth = {'Authorization':'Bearer '+operator['access_token']}
        for receipt in original['receipts']:
            if checked(client, 'GET', '/sync/v1/admin/operations/'+receipt['operation_id'], headers=auth) != receipt:
                raise ProbeFailure('RESTORED_ADMIN_RECEIPT_CHANGED')
        details = checked(client, 'GET', '/sync/v1/admin/accounts/'+original['member'], headers=auth)
        quotas = {item['id']:item['quota'] for item in details['vaults']}
        if details['policy']['default_quota'] != 96 or quotas != {original['vault']:20,original['future_vault']:96}:
            raise ProbeFailure('RESTORED_QUOTA_POLICY_CHANGED')
        revoked = original['sessions'][1]
        checked(client, 'GET', '/sync/v1/vaults', headers={'Authorization':'Bearer '+revoked['access_token']}, status=401)
        checked(client, 'POST', '/sync/v1/auth/refresh', json={'refresh_token':revoked['refresh_token']}, status=401)
        active = {'Authorization':'Bearer '+original['sessions'][0]['access_token']}
        checked(client, 'GET', '/sync/v1/admin/diagnostics', headers=active, status=403)
        base = '/sync/v1/vaults/'+original['vault']; content = b'restored sync bytes'
        sha = hashlib.sha256(content).hexdigest()
        upload = checked(client, 'POST', base+'/uploads', headers=active, json={'content_hash':sha,'size':len(content)})['upload_id']
        checked(client, 'PUT', base+'/uploads/'+upload+'?offset=0', headers=active, content=content)
        checked(client, 'POST', base+'/uploads/'+upload+'/complete', headers=active)
        file = uuid.uuid4().hex
        revision = checked(client, 'POST', base+'/revisions', headers=active, json={
            'operation_id':uuid.uuid4().hex,'file_id':file,'base_revision':0,'path':'restored.md','operation':'put','content_hash':sha,'size':len(content)})
        other = checked(client, 'POST', '/sync/v1/auth/sessions', json={
            'username':'provisioned-member','password':'controlled-provisioned-password','device_name':'new-restored-device'})
        second = {'Authorization':'Bearer '+other['access_token']}
        changes = checked(client, 'GET', base+'/changes', headers=second)
        downloaded = client.get(base+'/objects/'+sha, headers=second)
        if revision['sequence'] != 1 or changes['items'][0]['file_id'] != file or downloaded.status_code != 200 or downloaded.content != content:
            raise ProbeFailure('PROVISIONED_POST_RESTORE_SYNC_FAILED')
    return {'restored_receipts':len(original['receipts']),'restored_default_quota':96,
            'restored_vault_quota':20,'post_restore_bytes':len(content),'post_restore_sha256':sha,
            'two_device_sync':True,'revoked_access_and_refresh_rejected':True,'ordinary_admin_access_rejected':True}


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
        stage = 'operator_provisioning'
        administration = administration_seed(source, original_objects, work)
        report['checks'].append('operator_provisioning_quota_reservations_and_readonly_reconciliation')
        files = [
            ('experiments/课程#1%2F.py', 'print("中文")\r\n'.encode()),
            ('experiments/输入.json', '{"课程":"数据","值":7}\r\n'.encode()),
            ('experiments/结果.csv', '名称,数量\r\n中文,7\r\n'.encode()),
        ]
        stage = 'real_provider_protocol'
        with TestClient(create_app(source, original_objects, work/'source-staging',
                                   operator_user_ids=[administration['operator']], operations_path=journal)) as client:
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
            stage = 'device_commit_and_offered_progress'
            second_auth = {'Authorization': 'Bearer '+sessions[1]['access_token']}
            offered = checked(client, 'GET', base+'/changes', headers=second_auth, params={'limit':2})
            if offered['cursor'] != 2 or len(offered['items']) != 2:
                raise ProbeFailure('DEVICE_OFFERED_PAGE_CHANGED')
            device_proof = device_progress_probe(client, source, base, auth, sessions)
            report['device_progress'] = device_proof
            checked(client, 'DELETE', '/sync/v1/devices/'+sessions[1]['device_id'], headers=auth, status=204)
            checked(client, 'GET', base+'/devices/progress', headers=second_auth, status=401)
            progress_snapshot = checked(client, 'GET', base+'/devices/progress', headers=auth)['items']
            stage = 'actual_upload_cleanup_history'
            from sync_server.database import rows, run
            from sync_server.maintenance import cleanup_expired_uploads
            expired = checked(client,'POST',base+'/uploads',headers=auth,
                              json={'content_hash':hashlib.sha256(b'owned-expired-upload').hexdigest(),'size':7})['upload_id']
            checked(client,'PUT',base+'/uploads/'+expired+'?offset=0',headers=auth,content=b'partial')
            with source.transaction() as conn:
                run(conn,'UPDATE uploads SET expires=0 WHERE id=:id',id=expired)
            cleanup = cleanup_expired_uploads(source,work/'source-staging')
            if cleanup['expired_uploads_removed'] != 1 or cleanup['released_bytes'] != 7:
                raise ProbeFailure('CLEANUP_HISTORY_ACTUAL_RESULT_CHANGED')
            with source.read_only() as conn:
                cleanup_history = [dict(r) for r in rows(conn,'SELECT * FROM maintenance_history ORDER BY id')]
            if len(cleanup_history)!=1 or cleanup_history[0]['removed']!=1:
                raise ProbeFailure('CLEANUP_HISTORY_NOT_RECORDED')
        report['checks'].append('confirmed_upload_and_revision_receipts')
        report['checks'].append('sampled_authentication_confirmed_commit_and_generated_page_are_distinct')

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

        stage = 'legacy_device_observation_backup'
        legacy_snapshot = work/'legacy-snapshot'
        legacy_progress_snapshot(snapshot, legacy_snapshot)
        legacy, legacy_url = database('legacy')
        legacy_objects = bucket('legacy')
        legacy_journal = work/'legacy-operations.sqlite3'
        cli(repo, work, legacy_journal, legacy_url, endpoint, legacy_objects.bucket,
            'restore', '--directory', str(legacy_snapshot), '--io-workers', '2')
        with TestClient(create_app(legacy, legacy_objects, work/'legacy-staging')) as client:
            legacy_page = checked(client, 'GET', base+'/devices/progress', headers=auth)
            if (legacy_page['vault_sequence'] != 3 or len(legacy_page['items']) != len(progress_snapshot)
                or any(item[name] is not None for item in legacy_page['items']
                       for name in ['authenticated_at','committed_sequence','committed_at','offered_sequence','offered_at'])):
                raise ProbeFailure('LEGACY_BACKUP_OBSERVATIONS_NOT_UNKNOWN')
        report['device_progress']['legacy_backup_restores_unknown'] = True
        with legacy.read_only() as conn:
            if rows(conn,'SELECT * FROM maintenance_history'):
                raise ProbeFailure('LEGACY_CLEANUP_HISTORY_NOT_UNKNOWN')
        report['checks'].append('legacy_backup_without_observation_tables_restores_into_empty_providers')

        stage = 'restored_protocol_and_authority'
        with TestClient(create_app(restored, restored_objects, work/'restored-staging')) as client:
            # This read does not change the restored authentication or progress rows.
            if checked(client, 'GET', base+'/devices/progress', headers=auth)['items'] != progress_snapshot:
                raise ProbeFailure('RESTORED_DEVICE_OBSERVATIONS_CHANGED')
            report['device_progress']['backup_and_restored_observations_match'] = True
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

        stage = 'restored_management_and_fresh_sync'
        report['administration'] = administration_restored(restored, restored_objects, work, administration)
        report['checks'].append('restored_account_policies_receipts_revocation_and_fresh_two_device_sync')

        stage = 'bounded_operations_observations'
        with restored.read_only() as conn:
            if [dict(r) for r in rows(conn,'SELECT * FROM maintenance_history ORDER BY id')] != cleanup_history:
                raise ProbeFailure('RESTORED_CLEANUP_HISTORY_CHANGED')
        with TestClient(create_app(source,original_objects,work/'source-observation-staging',
                                  operations_path=journal,operator_user_ids=[administration['operator']])) as client:
            report['observability'] = observability_probe(client,source,original_objects,auth)
        if not report['observability']['backup_and_verify_receipts_present']:
            raise ProbeFailure('ACTUAL_CLI_OBSERVATION_RECEIPTS_MISSING')
        report['observability'].update(cleanup_history_restored=True,legacy_backup_history_empty=True,
                                        actual_cli_and_providers=True)
        report['checks'].append('bounded_cleanup_history_separate_backup_receipts_redacted_readonly_export')

        stage = 'protected_reclamation_with_real_providers'
        gc_backup, gc_readonly = protected_reclamation(repo, work, journal, source, original_objects,
                                          source_url, endpoint, vault_id, base, auth, files)
        report['reclamation_readonly'] = gc_readonly
        report['checks'].append('reviewed_gc_preserves_history_and_recovers_lost_s3_delete_reply')
        report['checks'].append('operator_readonly_gc_snapshot_and_pending_receipts_preserve_real_providers')

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
    with db.read_only() as conn:
        operator = conn.execute(text("SELECT id FROM users WHERE username='probe-owner'")).scalar_one()
    readonly_proofs = []
    with TestClient(create_app(db, objects, work/'gc-staging', operator_user_ids=[operator])) as client:
        member = checked(client, 'POST', '/sync/v1/auth/sessions', json={
            'username':'provisioned-member', 'password':'controlled-provisioned-password', 'device_name':'gc-read-only'})
        ordinary = {'Authorization':'Bearer '+member['access_token']}
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
        readonly_proofs.append(reclamation_read_probe(client, db, objects, backup, auth, ordinary, plan))
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
        readonly_proofs.append(reclamation_read_probe(client, db, objects, backup, auth, ordinary, original))
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
    return backup, {'passed':True, 'provider':'PostgreSQL/S3', 'snapshots':readonly_proofs,
                    'permanent_history_preserved':True, 'new_http_mutations':0}


def legacy_progress_snapshot(source, destination):
    """Create a separate older-format snapshot, keeping the original bytes."""
    from sync_server.operations import sha256_file
    shutil.copytree(source, destination)
    database = destination/'database.jsonl'
    records = [json.loads(line) for line in database.read_text('utf-8').splitlines()]
    records = [item for item in records if item['table'] not in {'device_activity','device_progress','maintenance_history'}]
    database.write_text(''.join(json.dumps(item,ensure_ascii=False)+'\n' for item in records),'utf-8')
    manifest = json.loads((destination/'manifest.json').read_text('utf-8'))
    manifest['database_sha256'] = sha256_file(database)
    for table in ['device_activity','device_progress','maintenance_history']:
        manifest['database_rows'].pop(table, None)
    (destination/'manifest.json').write_text(json.dumps(manifest)+'\n','utf-8')


def device_progress_probe(client, db, base, auth, sessions):
    """Verify actual server observations without claiming client acknowledgement."""
    from sync_server.database import rows
    def observations():
        with db.read_only() as conn:
            return {table:[dict(item) for item in rows(conn, 'SELECT * FROM '+table+' ORDER BY 1,2')]
                    for table in ['device_activity','device_progress']}
    before = observations()
    observed = checked(client, 'GET', base+'/devices/progress', headers=auth)
    if observations() != before:
        raise ProbeFailure('DEVICE_PROGRESS_READ_CHANGED_OBSERVATIONS')
    devices = {item['device_id']:item for item in observed['items']}
    writer, reader = devices[sessions[0]['device_id']], devices[sessions[1]['device_id']]
    if (observed['client_application_confirmed'] is not False
        or observed['offered_semantics'] != 'server_generated_page'
        or observed['activity_sample_seconds'] != 60
        or observed['vault_sequence'] != 3 or writer['committed_sequence'] != 3
        or writer['offered_sequence'] is not None or reader['committed_sequence'] is not None
        or reader['offered_sequence'] != 2
        or writer['authenticated_at'] is None or reader['authenticated_at'] is None):
        raise ProbeFailure('DEVICE_PROGRESS_SEMANTICS_CHANGED')
    # A rejected cursor must not advance the reader's persisted observation.
    second = {'Authorization':'Bearer '+sessions[1]['access_token']}
    checked(client, 'GET', base+'/changes', headers=second, params={'cursor':4}, status=409)
    if observations() != before:
        raise ProbeFailure('REJECTED_PAGE_CHANGED_DEVICE_PROGRESS')
    return {'writer_committed_sequence':3, 'reader_offered_sequence':2,
            'reader_committed_sequence':None, 'client_application_confirmed':False,
            'activity_sample_seconds':60, 'readonly_observations_unchanged':True,
            'rejected_cursor_unchanged':True}


def reclamation_read_probe(client, db, objects, backup, auth, ordinary, original):
    """Read a real CLI-created plan while protecting the actual recovery objects."""
    from sqlalchemy.exc import DBAPIError
    from sync_server.database import rows
    base = '/sync/v1/admin/reclamation/plans'
    def ledger():
        with db.read_only() as conn:
            return {table:[dict(item) for item in rows(conn, 'SELECT * FROM '+table+' ORDER BY 1')]
                    for table in ['reclamation_plans','reclamation_objects','objects','revisions','vaults','sessions','device_activity','device_progress']}
    before = ledger()
    backup_bytes = {str(path.relative_to(backup)):hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in backup.rglob('*') if path.is_file()}
    io_methods = {name:getattr(objects,name) for name in ['get','put','delete']}
    def forbid_io(*args, **kwargs): raise ProbeFailure('READONLY_GC_PERFORMED_OBJECT_IO')
    try:
        for name in io_methods: setattr(objects,name,forbid_io)
        page = checked(client,'GET',base,headers=auth,params={'vault_id':original['vault_id'],'state':original['state']})
        detail = checked(client,'GET',base+'/'+original['plan_id'],headers=auth)
        checked(client,'GET',base,headers=ordinary,status=403)
        checked(client,'GET',base+'/'+original['plan_id'],headers=ordinary,status=403)
    finally:
        for name, method in io_methods.items(): setattr(objects,name,method)
    if not any(item['plan_id']==original['plan_id'] for item in page['items']):
        raise ProbeFailure('READONLY_GC_PLAN_MISSING')
    for field in ['preview','items','state','code','backup_manifest_sha256','reclaimed_bytes','reclaimed_objects','protected_objects','pending_objects']:
        if detail[field] != original[field]: raise ProbeFailure('READONLY_GC_ORIGINAL_RECEIPT_CHANGED')
    if not detail['snapshot_only'] or ledger()!=before:
        raise ProbeFailure('READONLY_GC_CHANGED_LEDGER')
    if {str(path.relative_to(backup)):hashlib.sha256(path.read_bytes()).hexdigest() for path in backup.rglob('*') if path.is_file()} != backup_bytes:
        raise ProbeFailure('READONLY_GC_CHANGED_BACKUP')
    if not db.sqlite:
        try:
            with db.read_only() as conn:
                if conn.exec_driver_sql('SHOW transaction_read_only').scalar() != 'on' or conn.exec_driver_sql('SHOW transaction_isolation').scalar() != 'repeatable read':
                    raise ProbeFailure('READONLY_TRANSACTION_NOT_ISOLATED')
                conn.exec_driver_sql('UPDATE reclamation_plans SET code=code WHERE false')
            raise ProbeFailure('READONLY_TRANSACTION_ACCEPTED_WRITE')
        except DBAPIError as error:
            if getattr(error.orig,'sqlstate',None) != '25006': raise ProbeFailure('READONLY_TRANSACTION_WRONG_FAILURE') from None
        with db.transaction() as conn:
            if conn.exec_driver_sql('SHOW transaction_read_only').scalar() != 'off':
                raise ProbeFailure('READONLY_TRANSACTION_LEAKED_POOL_STATE')
    return {'plan_id':original['plan_id'], 'state':detail['state'], 'pending_objects':detail['pending_objects'],
            'original_snapshot_and_receipts':True, 'ledger_and_backup_unchanged':True,
            'object_io_calls':0, 'ordinary_account_forbidden':True,
            'postgres_readonly_write_denial_and_pool_restoration':not db.sqlite}


def observability_probe(client, db, objects, auth):
    """Verify the new read surface, not a second backup or archive validation."""
    from sync_server.database import rows
    def original():
        with db.read_only() as conn:
            return {table:[dict(r) for r in rows(conn,'SELECT * FROM '+table+' ORDER BY 1')]
                    for table in ('maintenance_history','objects','revisions','vaults','device_activity','device_progress')}
    before=original()
    methods={name:getattr(objects,name) for name in ('get','put')}
    if hasattr(objects,'delete'): methods['delete']=objects.delete
    def reject_io(*args,**kwargs): raise ProbeFailure('OBSERVABILITY_CALLED_OBJECT_IO')
    try:
        for name in methods: setattr(objects,name,reject_io)
        observed=checked(client,'GET','/sync/v1/admin/observability',headers=auth)
        exported=checked(client,'GET','/sync/v1/admin/observability/export',headers=auth)
    finally:
        for name,method in methods.items(): setattr(objects,name,method)
    if before!=original() or observed['history']['retention_limit']!=1000 or observed['cache_seconds']!=0:
        raise ProbeFailure('OBSERVABILITY_CHANGED_DATABASE')
    if not exported['redacted'] or exported['backup_receipts']['archive_association_known']:
        raise ProbeFailure('OBSERVABILITY_EXPORT_SCOPE_CHANGED')
    serialized=json.dumps(exported)
    if any(item['id'] in serialized for item in observed['history']['items']):
        raise ProbeFailure('OBSERVABILITY_EXPORTED_IDENTIFIERS')
    receipts=observed['backup_receipts']
    return {'passed':True,'readonly_rows_unchanged':True,'object_io_calls':0,'history_retention_limit':1000,
            'capacity_basis':observed['capacity']['basis'],'redacted_export':True,
            'independent_archive_association_unknown':True,
            'backup_and_verify_receipts_present':receipts['backup'] is not None and receipts['verify'] is not None}


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
