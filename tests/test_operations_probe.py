"""The production probe must refuse remote or preexisting resources before IO."""
import pytest

from tools.operations_probe import ProbeFailure, administration_seed, administration_restored, configuration, probe, served_protocol, device_progress_probe, observability_probe
from test_admin import admin


def environment():
    return dict(SYNC_OPERATIONS_PROBE='1',
                SYNC_OPERATIONS_PROBE_DATABASE_URL='postgresql+psycopg://probe:synthetic@127.0.0.1:15432/postgres',
                SYNC_OPERATIONS_PROBE_S3_ENDPOINT='http://127.0.0.1:19000',
                AWS_ACCESS_KEY_ID='synthetic-probe', AWS_SECRET_ACCESS_KEY='synthetic-probe-password')


@pytest.mark.parametrize('field,value', [
    ('SYNC_OPERATIONS_PROBE', '0'),
    ('SYNC_OPERATIONS_PROBE_DATABASE_URL', 'postgresql+psycopg://probe:synthetic@example.invalid/postgres'),
    ('SYNC_OPERATIONS_PROBE_DATABASE_URL', 'postgresql+psycopg://probe:synthetic@127.0.0.1/existing'),
    ('SYNC_OPERATIONS_PROBE_DATABASE_URL', 'postgresql+psycopg://probe:synthetic@127.0.0.1/postgres?host=remote'),
    ('SYNC_OPERATIONS_PROBE_S3_ENDPOINT', 'https://example.invalid'),
    ('SYNC_OPERATIONS_PROBE_S3_ENDPOINT', 'http://user:password@127.0.0.1:19000'),
    ('SYNC_OPERATIONS_PROBE_S3_ENDPOINT', 'http://127.0.0.1:19000/existing-bucket'),
])
def test_refuses_ordinary_or_remote_services_before_creating_any_resource(field, value):
    env = environment()
    env[field] = value
    with pytest.raises(ProbeFailure):
        configuration(env)


def test_existing_work_directory_is_never_reused_or_removed(tmp_path, monkeypatch):
    for name, value in environment().items():
        monkeypatch.setenv(name, value)
    work = tmp_path/'existing'
    work.mkdir()
    original = work/'original.bin'
    original.write_bytes(b'preserve-original')
    with pytest.raises(FileExistsError):
        probe(work)
    assert original.read_bytes() == b'preserve-original'
    assert list(work.iterdir()) == [original]


def test_existing_serve_staging_is_preserved_before_any_process_is_started(tmp_path):
    staging = tmp_path/'served-staging'
    staging.mkdir()
    original = staging/'original.bin'
    original.write_bytes(b'preserve-served-staging')
    with pytest.raises(FileExistsError):
        served_protocol(tmp_path, tmp_path, tmp_path/'journal.sqlite3', None, '', '', '', {}, [])
    assert list(staging.iterdir()) == [original]
    assert original.read_bytes() == b'preserve-served-staging'


def test_operator_probe_actions_preserve_policy_and_sync_across_fixture_snapshot(tmp_path):
    import sqlite3
    from sync_server.database import Database
    from sync_server.storage import DiskObjects
    source=Database('sqlite:///'+str(tmp_path/'source.db')); source.migrate()
    source.add_user('probe-owner','controlled-probe-password')
    source_objects=DiskObjects(tmp_path/'source-objects')
    original=administration_seed(source,source_objects,tmp_path)
    # Local contract exercise only. The production probe above runs actual
    # PostgreSQL/S3 CLI backup, verification and empty-target restoration in CI.
    with sqlite3.connect(tmp_path/'source.db') as original_db, sqlite3.connect(tmp_path/'restored.db') as recovered_db:
        original_db.backup(recovered_db)
    restored=Database('sqlite:///'+str(tmp_path/'restored.db'))
    objects=DiskObjects(tmp_path/'restored-objects')
    proof=administration_restored(restored,objects,tmp_path,original)
    assert proof['restored_receipts'] == 4 and proof['two_device_sync']
    assert proof['post_restore_bytes'] == len(b'restored sync bytes')
    assert proof['ordinary_admin_access_rejected'] and proof['revoked_access_and_refresh_rejected']
    source.engine.dispose(); restored.engine.dispose()


def test_progress_probe_reads_two_real_device_observations_without_updating_them(admin):
    from test_protocol import change, upload
    import uuid
    client,db,_,_,_,auth,session,login,_=admin
    second,second_session=login('operator')
    vault=client.post('/sync/v1/vaults',headers=auth,json={'name':'observations'}).json()['vault_id']
    base='/sync/v1/vaults/'+vault
    content=b'actual device observation'
    sha=upload(client,base,auth,content)
    for index in range(3):
        body={**change(sha,size=len(content)), 'file_id':uuid.uuid4().hex, 'path':f'{index}.md'}
        assert client.post(base+'/revisions',headers=auth,json=body).status_code==200
    assert client.get(base+'/changes?limit=2',headers=second).json()['cursor']==2
    # SQLite exercises the probe contract locally; actual PostgreSQL/S3,
    # packaged CLI and both backup formats run only in production CI.
    proof=device_progress_probe(client,db,base,auth,[session,second_session])
    assert proof['readonly_observations_unchanged'] and proof['rejected_cursor_unchanged']
    assert proof['writer_committed_sequence']==3 and proof['reader_offered_sequence']==2
    assert proof['reader_committed_sequence'] is None and not proof['client_application_confirmed']


def test_operations_observation_probe_has_no_writes_or_object_io(admin):
    client,db,store,_,_,auth,*_=admin
    proof=observability_probe(client,db,store,auth)
    assert proof['readonly_rows_unchanged'] and proof['object_io_calls']==0
    assert proof['history_retention_limit']==1000 and proof['redacted_export']
    assert proof['independent_archive_association_unknown']
    assert not proof['backup_and_verify_receipts_present']
