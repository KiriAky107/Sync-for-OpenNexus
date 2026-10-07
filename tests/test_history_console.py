"""Console preflight and read-only outcome checks exercise real stored history."""
from test_protocol import env, setup, session
from test_history import put, restore_request


def test_restore_target_checks_exact_parent_child_and_casefold_without_wildcards(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    occupied = put(client, base, auth, path='笔记_%/Existing.md')
    target = lambda path: client.get(base+'/restore-target', headers=auth, params={'file_id': first['file_id'], 'path': path})
    for path in ('笔记_%/existing.MD', '笔记_%', '笔记_%/Existing.md/child.md'):
        checked = target(path)
        assert checked.status_code == 200
        assert checked.json()['occupied']['file_id'] == occupied['file_id']
        assert checked.json()['current_revision'] == first['sequence']
    for path in ('笔记XX/Existing.md', first['path'], 'recover/original.md'):
        assert target(path).json()['occupied'] is None
    for path in ('../outside.md', 'con.md', '.git/config', '/absolute.md', 'ends-in-space '):
        assert target(path).status_code == 422
    missing = client.get(base+'/restore-target', headers=auth, params={'file_id': 'nonexistent-identity', 'path': 'safe.md'})
    assert missing.status_code == 404


def test_result_lookup_is_authorized_readonly_and_survives_later_edits(env):
    client, _, _, _ = env
    auth, _, base = setup(client)
    first = put(client, base, auth)
    second = put(client, base, auth, b'second', file_id=first['file_id'], revision=first['sequence'])
    request = restore_request(first, second)
    result = client.post(base+'/files/'+first['file_id']+'/restore', headers=auth, json=request).json()
    other_device, _ = session(client)
    later = put(client, base, other_device, b'later', file_id=first['file_id'], revision=result['sequence'])
    lookup = base+'/files/'+first['file_id']+'/restore-results/'+request['operation_id']
    for headers in (auth, other_device):
        looked_up = client.get(lookup, headers=headers)
        assert looked_up.status_code == 200
        assert looked_up.json()['sequence'] == result['sequence']
        assert looked_up.json()['restored_from'] == first['sequence']
    assert client.get(base+'/files/'+first['file_id'], headers=auth).json()['sequence'] == later['sequence']
    normal = base+'/files/'+first['file_id']+'/restore-results/'+first['operation_id']
    assert client.get(normal, headers=auth).status_code == 404
    foreign, _ = session(client, 'bob')
    assert client.get(lookup, headers=foreign).status_code == 404
    assert client.get(base+'/restore-target', headers=foreign, params={'file_id': first['file_id'], 'path': 'safe.md'}).status_code == 404
    assert client.get(base+'/files/nonexistent-identity/restore-results/'+request['operation_id'], headers=auth).status_code == 404
    assert client.get('/sync/v1/vaults', headers=auth).json()['items'][0]['sequence'] == later['sequence']


def test_preflight_and_result_lookup_recheck_revoked_device(env):
    client, _, _, _ = env
    auth, device, base = setup(client)
    first = put(client, base, auth)
    other, _ = session(client)
    assert client.delete('/sync/v1/devices/'+device['device_id'], headers=other).status_code == 204
    assert client.get(base+'/restore-target', headers=auth, params={'file_id': first['file_id'], 'path': first['path']}).status_code == 401
    assert client.get(base+'/files/'+first['file_id']+'/restore-results/unknown-operation', headers=auth).status_code == 401
