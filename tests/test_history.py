"""Real historical bytes, stable paging, concurrent restore and legacy backups."""
import base64
import hashlib
from io import BytesIO
import json
import struct
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, PngImagePlugin
from botocore.exceptions import ClientError

from sync_server.database import row
from sync_server.operations import verify_backup
from test_protocol import env, setup, session, upload, change
from test_operations import backup


def put(client, base, auth, data=b'first version', path='notes/original.md', file_id=None, revision=0):
    if len(data) <= 1024*1024:
        sha = upload(client, base, auth, data)
    else:
        sha = hashlib.sha256(data).hexdigest()
        response = client.post(base+'/uploads', headers=auth, json={'content_hash': sha, 'size': len(data)})
        assert response.status_code == 200
        started = response.json()
        if not started['complete']:
            upload_path = base+'/uploads/'+started['upload_id']
            for offset in range(0, len(data), 512*1024):
                assert client.put(upload_path+f'?offset={offset}', headers=auth, content=data[offset:offset+512*1024]).status_code == 200
            assert client.post(upload_path+'/complete', headers=auth).status_code == 200
    values = change(sha, path=path, size=len(data), base_revision=revision)
    if file_id:
        values['file_id'] = file_id
    response = client.post(base+'/revisions', headers=auth, json=values)
    assert response.status_code == 200, response.text
    return response.json()


def restore_request(source, head, **extra):
    return dict(operation_id=uuid.uuid4().hex, source_revision=source['sequence'], base_revision=head['sequence'], **extra)


def test_restore_after_rename_preserves_identity_history_and_original_object(env):
    client, db, store, now = env
    auth, _, base = setup(client)
    first = put(client, base, auth, b'old content')
    second, _ = session(client)
    now[0] += 7
    renamed = put(client, base, second, b'new content', 'renamed.md', first['file_id'], first['sequence'])
    request = restore_request(first, renamed)
    response = client.post(base+'/files/'+first['file_id']+'/restore', headers=auth, json=request)
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored['file_id'] == first['file_id'] and restored['path'] == 'renamed.md'
    assert restored['hash'] == first['hash'] and restored['base_revision'] == renamed['sequence']
    history = client.get(base+'/history/'+first['file_id']+'?limit=2', headers=auth).json()
    assert history['current']['sequence'] == restored['sequence']
    assert history['items'][0]['restored_from'] == first['sequence']
    assert history['items'][0]['created_at'] == now[0] and history['items'][0]['device_name'] == '测试设备'
    assert history['has_more'] and history['next_before'] == renamed['sequence']
    assert client.get(base+'/objects/'+first['hash'], headers=auth).content == b'old content'
    assert client.get(base+'/objects/'+renamed['hash'], headers=second).content == b'new content'
    with db.transaction() as conn:
        assert row(conn, 'SELECT used FROM vaults WHERE id=:v', v=base.split('/')[-1])['used'] == len(b'old contentnew content')


def test_restore_lost_response_can_be_queried_after_another_edit_without_reverting_it(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    second = put(client, base, auth, b'second', 'new-name.md', first['file_id'], first['sequence'])
    request = restore_request(first, second)
    path = base+'/files/'+first['file_id']+'/restore'
    restored = client.post(path, headers=auth, json=request).json()
    edited = put(client, base, auth, b'later edit', 'later-name.md', first['file_id'], restored['sequence'])
    assert client.post(path, headers=auth, json=request).json() == restored
    assert client.get(base+'/files/'+first['file_id'], headers=auth).json()['sequence'] == edited['sequence']
    assert client.post(path, headers=auth, json={**request, 'source_revision': second['sequence']}).json()['error']['code'] == 'IDEMPOTENCY_REUSED'
    other, _ = session(client)
    assert client.post(path, headers=other, json=request).json()['error']['code'] == 'IDEMPOTENCY_REUSED'


def test_concurrent_restore_has_one_winner_and_does_not_overwrite_new_head(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    head = put(client, base, auth, b'second', file_id=first['file_id'], revision=first['sequence'])
    path = base+'/files/'+first['file_id']+'/restore'
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path, headers=auth, json=restore_request(first, head)), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409).json()['error']
    assert conflict['code'] == 'REVISION_CONFLICT'
    assert conflict['details']['current']['sequence'] == head['sequence']+1


def test_deleted_file_restore_checks_path_occupancy_and_accepts_reviewed_new_target(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    deletion = change(None, file_id=first['file_id'], path=first['path'], base_revision=first['sequence'], operation='delete', size=0)
    deleted = client.post(base+'/revisions', headers=auth, json=deletion).json()
    occupied = put(client, base, auth, b'another file', path=first['path'])
    path = base+'/files/'+first['file_id']+'/restore'
    assert client.post(path, headers=auth, json=restore_request(first, deleted)).json()['error']['code'] == 'PATH_CONFLICT'
    assert client.post(path, headers=auth, json=restore_request(deleted, deleted)).json()['error']['code'] == 'RESTORE_SOURCE_DELETED'
    request = restore_request(first, deleted, path='recovered.md')
    restored = client.post(path, headers=auth, json=request).json()
    assert restored['path'] == 'recovered.md' and restored['file_id'] == first['file_id']
    assert client.get(base+'/files/'+occupied['file_id'], headers=auth).json()['hash'] == occupied['hash']


def test_file_pages_pin_a_snapshot_across_rename_delete_and_new_files(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth, path='中文/a.md')
    second = put(client, base, auth, path='b.md')
    page = client.get(base+'/files?limit=1', headers=auth).json()
    assert page['items'][0]['file_id'] == second['file_id'] and page['has_more']
    moved = put(client, base, auth, path='changed.md', file_id=first['file_id'], revision=first['sequence'])
    put(client, base, auth, path='new.md')
    next_page = client.get(base+f'/files?limit=1&before={page["next_before"]}&boundary={page["boundary"]}', headers=auth).json()
    assert next_page['items'][0]['path'] == first['path'] and not next_page['has_more']
    assert client.get(base+'/files?q=中文', headers=auth).json()['items'] == []
    assert client.get(base+f'/files?q=中文&boundary={page["boundary"]}', headers=auth).json()['items'][0]['file_id'] == first['file_id']
    assert client.get(base+'/files?q=%25', headers=auth).json()['items'] == []
    assert client.get(base+'/files?boundary=999', headers=auth).json()['error']['code'] == 'CURSOR_INVALID'


def test_history_and_restore_cannot_cross_accounts_or_file_identities(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    other_file = put(client, base, auth, path='another.md')
    bob, _ = session(client, 'bob')
    assert client.get(base+'/files', headers=bob).status_code == 404
    assert client.get(base+'/history/'+first['file_id'], headers=bob).status_code == 404
    assert client.get(base+f'/history/{first["file_id"]}/{first["sequence"]}/preview', headers=bob).status_code == 404
    path = base+'/files/'+other_file['file_id']+'/restore'
    assert client.post(path, headers=auth, json=restore_request(first, other_file)).json()['error']['code'] == 'REVISION_NOT_FOUND'
    assert client.post(base+'/files/'+first['file_id']+'/restore', headers=auth, json=restore_request(first, first, path='../bad.md')).status_code == 422


def test_all_new_endpoints_recheck_device_revocation(env):
    client, _, _, _ = env
    auth, token, base = setup(client)
    first = put(client, base, auth)
    other, _ = session(client)
    assert client.delete('/sync/v1/devices/'+token['device_id'], headers=other).status_code == 204
    for suffix in ['/files', '/files/'+first['file_id'], '/history/'+first['file_id'], f'/history/{first["file_id"]}/{first["sequence"]}/preview']:
        assert client.get(base+suffix, headers=auth).status_code == 401
    assert client.post(base+'/files/'+first['file_id']+'/restore', headers=auth, json=restore_request(first, first)).status_code == 401


def test_preview_preserves_text_and_checks_real_object_integrity(env):
    client, _, store, _ = env
    auth, _, base = setup(client)
    data = '你好\r\n<script>never execute</script>\n'.encode()
    first = put(client, base, auth, data)
    path = base+f'/history/{first["file_id"]}/{first["sequence"]}/preview'
    preview = client.get(path, headers=auth).json()
    assert preview['kind'] == 'text' and preview['text'].encode() == data
    with store.open(base.split('/')[-1]+'/'+first['hash']) as stream:
        assert stream.read() == data
    object_path = store.root/base.split('/')[-1]/first['hash']
    object_path.write_bytes(b'corrupt bytes')
    assert client.get(path, headers=auth).json()['error']['code'] == 'STORAGE_INTEGRITY'


def test_preview_storage_failure_is_classified_and_releases_its_slot(env, monkeypatch):
    client, _, store, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    path = base+f'/history/{first["file_id"]}/{first["sequence"]}/preview'
    def unavailable(*args):
        raise ClientError({'Error': {'Code': 'NoSuchKey', 'Message': 'private storage diagnostic'}}, 'GetObject')
    with monkeypatch.context() as patch:
        patch.setattr(store, 'open', unavailable)
        for _ in range(3):
            response = client.get(path, headers=auth)
            assert response.status_code == 503 and response.json()['error']['code'] == 'STORAGE_UNAVAILABLE'
            assert 'private storage diagnostic' not in response.text
    assert client.get(path, headers=auth).json()['kind'] == 'text'


def test_preview_limits_large_objects_before_reading_and_does_not_render_svg(env, monkeypatch):
    client, _, store, _ = env
    auth, _, base = setup(client)
    large = put(client, base, auth, b'x'*(1024*1024+1))
    svg = put(client, base, auth, b'<svg onload="alert(1)"/>', path='unsafe.svg')
    assert client.get(base+f'/history/{svg["file_id"]}/{svg["sequence"]}/preview', headers=auth).json()['reason'] == 'UNSUPPORTED_TYPE'
    def unexpected_read(*args):
        raise AssertionError('Oversize preview opened an object')
    monkeypatch.setattr(store, 'open', unexpected_read)
    assert client.get(base+f'/history/{large["file_id"]}/{large["sequence"]}/preview', headers=auth).json()['reason'] == 'PREVIEW_TOO_LARGE'


def test_image_preview_reencodes_pixels_and_discards_source_metadata(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    buffer = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('secret', 'must not propagate')
    Image.new('RGB', (8, 4), 'red').save(buffer, format='PNG', pnginfo=metadata)
    original = buffer.getvalue()
    first = put(client, base, auth, original, 'image.png')
    preview = client.get(base+f'/history/{first["file_id"]}/{first["sequence"]}/preview', headers=auth).json()
    assert preview['kind'] == 'image' and preview['mime'] == 'image/png'
    rendered = base64.b64decode(preview['data'])
    assert b'must not propagate' not in rendered
    with Image.open(BytesIO(rendered)) as image:
        assert image.size == (8, 4) and image.getpixel((0, 0)) == (255, 0, 0, 255)
    assert client.get(base+'/objects/'+first['hash'], headers=auth).content == original


def test_image_preview_rejects_excessive_pixel_dimensions_before_decoding(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    buffer = BytesIO()
    Image.new('RGB', (1, 1)).save(buffer, format='PNG')
    data = bytearray(buffer.getvalue())
    data[16:24] = struct.pack('>II', 8192, 8193)
    data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]))
    first = put(client, base, auth, bytes(data), 'oversize.png')
    result = client.get(base+f'/history/{first["file_id"]}/{first["sequence"]}/preview', headers=auth).json()
    assert result['kind'] == 'unsupported' and result['reason'] == 'IMAGE_TOO_LARGE'


def test_upgrade_keeps_legacy_revisions_and_does_not_invent_timestamps(env):
    client, db, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    with db.transaction() as conn:
        conn.exec_driver_sql('DROP TABLE revision_annotations')
    db.migrate()
    history = client.get(base+'/history/'+first['file_id'], headers=auth).json()
    assert history['items'][0]['created_at'] is None and history['items'][0]['hash'] == first['hash']
    restored = client.post(base+'/files/'+first['file_id']+'/restore', headers=auth, json=restore_request(first, first))
    assert restored.status_code == 200


def test_old_backup_manifest_without_annotations_remains_valid_without_rewriting_it(tmp_path):
    source = tmp_path/'legacy-backup'
    _, manifest = backup(source)
    manifest['database_rows'].pop('revision_annotations')
    (source/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    original = {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}
    assert verify_backup(source)['status'] == 'BACKUP_VERIFIED'
    assert original == {path.relative_to(source): path.read_bytes() for path in source.rglob('*') if path.is_file()}
