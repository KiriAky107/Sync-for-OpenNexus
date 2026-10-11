"""Sample authenticated requests separately from confirmed commits and offered pages."""
from typing import Annotated

from fastapi import Header, Path, Query

from .database import row, rows, run

SAMPLE_SECONDS = 60
Identifier = Annotated[str, Path(pattern=r'^[0-9a-f]{32}$')]


def authenticated(conn, device_id, now):
    previous = row(conn, 'SELECT authenticated_at FROM device_activity WHERE device_id=:id', id=device_id)
    if previous and previous['authenticated_at'] > now - SAMPLE_SECONDS:
        return
    run(conn, 'INSERT INTO device_activity VALUES (:id,:now) ON CONFLICT(device_id) DO UPDATE SET authenticated_at=:now WHERE device_activity.authenticated_at<=:cutoff',
        id=device_id, now=now, cutoff=now-SAMPLE_SECONDS)


def progress(conn, device_id, vault_id, sequence, now, *, kind):
    assert kind in {'committed', 'offered'}
    field, timestamp = kind+'_sequence', kind+'_at'
    previous = row(conn, f'SELECT {field},{timestamp} FROM device_progress WHERE device_id=:device AND vault_id=:vault', device=device_id, vault=vault_id)
    if previous and previous[field] is not None:
        if previous[field] > sequence or previous[field] == sequence and previous[timestamp] > now-SAMPLE_SECONDS:
            return
    run(conn, f'INSERT INTO device_progress (device_id,vault_id,{field},{timestamp}) VALUES (:device,:vault,:sequence,:now) ON CONFLICT(device_id,vault_id) DO UPDATE SET {field}=:sequence,{timestamp}=:now WHERE device_progress.{field} IS NULL OR device_progress.{field}<:sequence OR (device_progress.{field}=:sequence AND device_progress.{timestamp}<=:cutoff)',
        device=device_id, vault=vault_id, sequence=sequence, now=now, cutoff=now-SAMPLE_SECONDS)


def device_page(conn, *, user_id, vault, limit, before, observed_at):
    found = rows(conn, 'SELECT d.id AS device_id,d.name,d.revoked,a.authenticated_at,p.committed_sequence,p.committed_at,p.offered_sequence,p.offered_at FROM devices d LEFT JOIN device_activity a ON a.device_id=d.id LEFT JOIN device_progress p ON p.device_id=d.id AND p.vault_id=:vault WHERE d.user_id=:user AND d.id<:before ORDER BY d.id DESC LIMIT :limit',
                 user=user_id, vault=vault['id'], before=before or 'f'*33, limit=limit+1)
    items = [dict(item) for item in found[:limit]]
    return {'schema_version':1, 'observed_at':observed_at, 'activity_sample_seconds':SAMPLE_SECONDS,
            'client_application_confirmed':False, 'offered_semantics':'server_generated_page',
            'vault_id':vault['id'], 'vault_sequence':vault['sequence'], 'items':items,
            'next_before':items[-1]['device_id'] if len(found)>limit else None}


def register_device_progress(app, db, identity, error, clock):
    @app.get('/sync/v1/vaults/{vault_id}/devices/progress')
    def device_progress(vault_id: Identifier, limit: int = Query(default=20, ge=1, le=100),
                        before: str | None = Query(default=None, pattern=r'^[0-9a-f]{32}$'), authorization: str = Header(default='')):
        with db.read_only() as conn:
            actor = identity(conn, authorization, observe=False)
            vault = row(conn, 'SELECT id,sequence FROM vaults WHERE id=:id AND user_id=:user', id=vault_id, user=actor['user_id'])
            if not vault: raise error(404, 'VAULT_NOT_FOUND')
            return device_page(conn, user_id=actor['user_id'], vault=vault, limit=limit, before=before, observed_at=int(clock()))
