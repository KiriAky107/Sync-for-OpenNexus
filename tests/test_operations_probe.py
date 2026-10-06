"""The production probe must refuse remote or preexisting resources before IO."""
import pytest

from tools.operations_probe import ProbeFailure, configuration, probe, served_protocol


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
