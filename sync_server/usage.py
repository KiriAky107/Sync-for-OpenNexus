"""Account-scoped storage ledgers and immutable upload disposition receipts."""
import re
from fastapi import Header, Query
from .database import row, rows, run


def disposition(conn, upload, state, actor, now):
    run(conn, 'INSERT INTO upload_dispositions VALUES (:id,:vault,:state,:actor,:size,:offset,:now)',
        id=upload['id'], vault=upload['vault_id'], state=state, actor=actor,
        size=upload['size'], offset=upload['offset_bytes'], now=now)


def result(conn, vault_id, upload_id, now):
    completed = row(conn, 'SELECT hash,completed FROM upload_receipts WHERE vault_id=:v AND id=:id', v=vault_id, id=upload_id)
    if completed:
        from .reclamation import pending, reclaimed
        state = 'reclamation_pending' if pending(conn, vault_id, completed['hash']) else (
            'reclaimed' if not row(conn, 'SELECT 1 FROM objects WHERE vault_id=:v AND hash=:h', v=vault_id, h=completed['hash']) and reclaimed(conn, vault_id, completed['hash']) else 'completed')
        if state != 'completed':
            audit = row(conn, 'SELECT MAX(confirmed_at) AS confirmed FROM reclamation_objects WHERE vault_id=:v AND hash=:h AND state=:state', v=vault_id, h=completed['hash'], state='prepared' if state == 'reclamation_pending' else 'reclaimed')
            return {'state':state, 'content_hash':completed['hash'], 'completed_at':completed['completed'], 'confirmed_at':audit['confirmed']}
        return {'state': state, 'content_hash': completed['hash'], 'confirmed_at': completed['completed']}
    receipt = row(conn, 'SELECT state,size,offset_bytes,confirmed_at FROM upload_dispositions WHERE vault_id=:v AND id=:id', v=vault_id, id=upload_id)
    if receipt: return dict(receipt)
    upload = row(conn, 'SELECT size,offset_bytes,expires FROM uploads WHERE vault_id=:v AND id=:id', v=vault_id, id=upload_id)
    if upload:
        return {'state': 'expired' if upload['expires'] <= now else 'active', **dict(upload), 'confirmed_at': now}
    return {'state': 'not_found', 'confirmed_at': now}


def register_usage(app, db, staging, vault, identity, error, clock, operators):
    @app.get('/sync/v1/vaults/{vault_id}/usage')
    def usage(vault_id: str, authorization: str = Header(default='')):
        with db.transaction() as conn:
            _, item = vault(conn, vault_id, authorization, lock=True)
            now = int(clock())
            objects = row(conn, 'SELECT COALESCE(SUM(size),0) AS bytes,COUNT(*) AS count FROM objects WHERE vault_id=:v', v=vault_id)
            current = row(conn, "SELECT COALESCE(SUM(o.size),0) AS bytes,COUNT(*) AS count FROM objects o WHERE o.vault_id=:v AND EXISTS (SELECT 1 FROM files f JOIN revisions r ON r.vault_id=f.vault_id AND r.sequence=f.sequence WHERE f.vault_id=o.vault_id AND f.deleted=0 AND r.hash=o.hash)", v=vault_id)
            referenced = row(conn, 'SELECT COALESCE(SUM(o.size),0) AS bytes,COUNT(*) AS count FROM objects o WHERE o.vault_id=:v AND EXISTS (SELECT 1 FROM revisions r WHERE r.vault_id=o.vault_id AND r.hash=o.hash)', v=vault_id)
            logical = row(conn, 'SELECT COALESCE(SUM(r.size),0) AS bytes,COUNT(*) AS count FROM files f JOIN revisions r ON r.vault_id=f.vault_id AND r.sequence=f.sequence WHERE f.vault_id=:v AND f.deleted=0', v=vault_id)
            uploads = row(conn, 'SELECT COALESCE(SUM(CASE WHEN expires>:now THEN size ELSE 0 END),0) AS reserved,COALESCE(SUM(offset_bytes),0) AS confirmed,COUNT(*) AS count,COALESCE(SUM(CASE WHEN expires<=:now THEN 1 ELSE 0 END),0) AS expired FROM uploads WHERE vault_id=:v', v=vault_id, now=now)
            pending_gc = row(conn, "SELECT COALESCE(SUM(size),0) AS bytes,COUNT(*) AS count FROM objects o WHERE o.vault_id=:v AND EXISTS (SELECT 1 FROM reclamation_objects g WHERE g.vault_id=o.vault_id AND g.hash=o.hash AND g.state='prepared')", v=vault_id)
            return {'schema_version':1, 'confirmed_at':now, 'sequence':item['sequence'], 'quota':item['quota'], 'charged_bytes':item['used'],
                'object_bytes':objects['bytes'], 'object_count':objects['count'], 'current_object_bytes':current['bytes'],
                'historical_only_bytes':referenced['bytes']-current['bytes'], 'unreferenced_object_bytes':objects['bytes']-referenced['bytes'],
                'logical_file_bytes':logical['bytes'], 'active_files':logical['count'], 'reserved_bytes':uploads['reserved'],
                'confirmed_upload_bytes':uploads['confirmed'], 'pending_uploads':uploads['count'], 'expired_uploads':uploads['expired'],
                'available_bytes':max(0, item['quota']-item['used']-uploads['reserved']), 'accounting_matches':item['used']==objects['bytes'],
                'history_retention':'indefinite', 'reclamation_pending_objects':pending_gc['count'], 'reclamation_pending_bytes':pending_gc['bytes']}

    @app.get('/sync/v1/vaults/{vault_id}/uploads')
    def upload_page(vault_id: str, limit: int = Query(default=30, ge=1, le=100), before: str | None = Query(default=None, max_length=60), authorization: str = Header(default='')):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization, lock=True)
            now = int(clock()); values = {'v':vault_id, 'limit':limit+1}; condition = ''
            if before:
                match = re.fullmatch(r'([0-9]{1,15}):([0-9a-f]{32})', before)
                if not match: raise error(422, 'INVALID_CURSOR')
                values.update(expires=int(match[1]), id=match[2])
                condition = ' AND (u.expires<:expires OR (u.expires=:expires AND u.id<:id))'
            items = rows(conn, 'SELECT u.*,d.name AS device_name,d.revoked AS device_revoked FROM uploads u JOIN devices d ON d.id=u.device_id WHERE u.vault_id=:v' + condition + ' ORDER BY u.expires DESC,u.id DESC LIMIT :limit', **values)
            visible = [{**dict(upload), 'state':'expired' if upload['expires']<=now else 'device_revoked' if upload['device_revoked'] else 'active'} for upload in items[:limit]]
            return {'schema_version':1, 'confirmed_at':now, 'items':visible, 'next_before':f"{visible[-1]['expires']}:{visible[-1]['id']}" if len(items)>limit else None}

    @app.get('/sync/v1/vaults/{vault_id}/uploads/{upload_id}/result')
    def upload_result(vault_id: str, upload_id: str, authorization: str = Header(default='')):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization, lock=True)
            return result(conn, vault_id, upload_id, int(clock()))

    @app.post('/sync/v1/vaults/{vault_id}/uploads/{upload_id}/cancel')
    def owner_cancel(vault_id: str, upload_id: str, authorization: str = Header(default='')):
        with db.transaction() as conn:
            session, _ = vault(conn, vault_id, authorization, lock=True)
            now = int(clock()); previous = result(conn, vault_id, upload_id, now)
            if previous['state'] in {'completed', 'cancelled', 'damaged', 'reclaimed', 'reclamation_pending'}: return previous
            upload = row(conn, 'SELECT * FROM uploads WHERE vault_id=:v AND id=:id', v=vault_id, id=upload_id)
            if not upload: return previous
            if not re.fullmatch(r'[0-9a-f]{32}', upload_id): raise error(409, 'UPLOAD_ID_INVALID')
            try: (staging / upload_id).unlink(missing_ok=True)
            except OSError: raise error(503, 'STAGING_UNAVAILABLE') from None
            disposition(conn, upload, 'cancelled', session['user_id'], now)
            run(conn, 'DELETE FROM uploads WHERE id=:id', id=upload_id)
            return result(conn, vault_id, upload_id, now)

    @app.get('/sync/v1/admin/maintenance')
    def maintenance(authorization: str = Header(default='')):
        with db.transaction() as conn:
            session = identity(conn, authorization)
            if session['user_id'] not in operators: raise error(403, 'OPERATIONS_FORBIDDEN')
            status = row(conn, "SELECT * FROM maintenance_summary WHERE kind='uploads'")
            return {'schema_version':1, 'confirmed_at':int(clock()), 'latest':dict(status) if status else None}
