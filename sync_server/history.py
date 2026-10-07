"""Authorized snapshot browsing and CAS restore; historical bytes stay immutable."""
import base64
import hashlib
from io import BytesIO
from pathlib import PurePosixPath
from threading import BoundedSemaphore

from fastapi import Header, Query
from PIL import Image, ImageOps, UnidentifiedImageError
from botocore.exceptions import BotoCoreError, ClientError

from .database import row, rows
from .models import Commit, Restore, canonical_path

TEXT_LIMIT = 1024 * 1024
IMAGE_LIMIT = 5 * 1024 * 1024
PIXEL_LIMIT = 16 * 1024 * 1024
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
TEXT_SUFFIXES = {'.md', '.txt', '.py', '.json', '.csv', '.yaml', '.yml', '.toml', '.log', '.ini', '.js', '.ts', '.rs', '.cpp', '.h'}
DETAILS = ('SELECT r.*, a.created AS created_at, a.restored_from, d.name AS device_name '
           'FROM revisions r LEFT JOIN revision_annotations a '
           'ON a.vault_id=r.vault_id AND a.sequence=r.sequence '
           'LEFT JOIN devices d ON d.id=r.device_id ')


def preview_content(data, path):
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in IMAGE_SUFFIXES:
        try:
            with Image.open(BytesIO(data), formats=['PNG', 'JPEG', 'WEBP', 'GIF']) as image:
                if image.width * image.height > PIXEL_LIMIT:
                    return {'kind': 'unsupported', 'reason': 'IMAGE_TOO_LARGE'}
                animated = bool(getattr(image, 'is_animated', False))
                image.seek(0)
                ImageOps.exif_transpose(image, in_place=True)
                image.thumbnail((1024, 1024))
                # A fresh PNG contains only rendered pixels, with no source metadata.
                safe = Image.new('RGBA', image.size)
                safe.paste(image.convert('RGBA'))
                output = BytesIO()
                safe.save(output, format='PNG')
                return {'kind': 'image', 'mime': 'image/png', 'data': base64.b64encode(output.getvalue()).decode('ascii'),
                        'width': safe.width, 'height': safe.height, 'first_frame_only': animated}
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            return {'kind': 'unsupported', 'reason': 'INVALID_IMAGE'}
    if suffix not in TEXT_SUFFIXES:
        return {'kind': 'unsupported', 'reason': 'UNSUPPORTED_TYPE'}
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return {'kind': 'unsupported', 'reason': 'NOT_UTF8'}
    if '\0' in text:
        return {'kind': 'unsupported', 'reason': 'BINARY_CONTENT'}
    return {'kind': 'text', 'format': suffix.lstrip('.'), 'text': text}


def register_history_routes(app, db, objects, vault, perform_commit, error):
    preview_slots = BoundedSemaphore(2)

    def current(conn, vault_id, file_id):
        return row(conn, DETAILS + 'JOIN files f ON f.vault_id=r.vault_id AND f.sequence=r.sequence '
                   'WHERE f.vault_id=:v AND f.file_id=:f', v=vault_id, f=file_id)

    def boundary_for(item, requested):
        end = item['sequence'] if requested is None else requested
        if end > item['sequence']:
            raise error(409, 'CURSOR_INVALID')
        return end

    @app.get('/sync/v1/vaults/{vault_id}/restore-target')
    def restore_target(vault_id: str, path: str = Query(min_length=1),
                       file_id: str = Query(pattern=r'^[a-zA-Z0-9-]{16,80}$'),
                       authorization: str = Header(default='')):
        try:
            canonical_path(path)
        except ValueError:
            raise error(422, 'INVALID_REQUEST') from None
        with db.transaction() as conn:
            _, item = vault(conn, vault_id, authorization)
            head = current(conn, vault_id, file_id)
            if not head:
                raise error(404, 'FILE_NOT_FOUND')
            key = path.casefold()
            occupied = row(conn, 'SELECT f.file_id,r.path FROM files f JOIN revisions r '
                'ON r.vault_id=f.vault_id AND r.sequence=f.sequence '
                'WHERE f.vault_id=:v AND f.deleted=0 AND f.file_id<>:f AND '
                '(f.path_key=:key OR substr(f.path_key,1,:length)=:desc '
                'OR substr(:key,1,length(f.path_key)+1)=f.path_key||\'/\') LIMIT 1',
                v=vault_id, f=file_id, key=key, length=len(key)+1, desc=key+'/')
            return {'path': path, 'current_revision': head['sequence'], 'boundary': item['sequence'],
                    'occupied': dict(occupied) if occupied else None}

    @app.get('/sync/v1/vaults/{vault_id}/files')
    def files(vault_id: str, before: int = Query(default=9223372036854775807, ge=1),
              boundary: int | None = Query(default=None, ge=0), limit: int = Query(default=50, ge=1, le=100),
              q: str = Query(default='', max_length=120), include_deleted: bool = False,
              authorization: str = Header(default='')):
        with db.transaction() as conn:
            _, item = vault(conn, vault_id, authorization)
            end = boundary_for(item, boundary)
            # Latest revision per identity at the pinned boundary, not mutable heads.
            match = '%' + q.strip().casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            values = rows(conn, DETAILS +
                'JOIN (SELECT file_id,MAX(sequence) AS sequence FROM revisions '
                'WHERE vault_id=:v AND sequence<=:end GROUP BY file_id) head '
                'ON head.file_id=r.file_id AND head.sequence=r.sequence '
                "WHERE r.vault_id=:v AND r.sequence<:before AND r.path_key LIKE :q ESCAPE '\\' "
                'AND (:deleted=1 OR r.operation<>\'delete\') ORDER BY r.sequence DESC LIMIT :limit',
                v=vault_id, end=end, before=before, q=match, deleted=int(include_deleted), limit=limit+1)
            items = [dict(value) for value in values[:limit]]
            return {'items': items, 'boundary': end, 'has_more': len(values) > limit,
                    'next_before': items[-1]['sequence'] if len(values) > limit else None}

    @app.get('/sync/v1/vaults/{vault_id}/files/{file_id}')
    def file_head(vault_id: str, file_id: str, authorization: str = Header(default='')):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization)
            value = current(conn, vault_id, file_id)
            if not value:
                raise error(404, 'FILE_NOT_FOUND')
            return dict(value)

    @app.get('/sync/v1/vaults/{vault_id}/files/{file_id}/restore-results/{operation_id}')
    def restore_result(vault_id: str, file_id: str, operation_id: str, authorization: str = Header(default='')):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization)
            value = row(conn, DETAILS + 'WHERE r.vault_id=:v AND r.file_id=:f AND r.operation_id=:op '
                        'AND a.restored_from IS NOT NULL', v=vault_id, f=file_id, op=operation_id)
            if not value:
                raise error(404, 'RESTORE_NOT_FOUND')
            return dict(value)

    @app.get('/sync/v1/vaults/{vault_id}/history/{file_id}')
    def history(vault_id: str, file_id: str, before: int = Query(default=9223372036854775807, ge=1),
                limit: int = Query(default=100, ge=1, le=500), boundary: int | None = Query(default=None, ge=0),
                authorization: str = Header(default='')):
        with db.transaction() as conn:
            _, item = vault(conn, vault_id, authorization)
            end = boundary_for(item, boundary)
            values = rows(conn, DETAILS + 'WHERE r.vault_id=:v AND r.file_id=:f AND r.sequence<:before '
                          'AND r.sequence<=:end ORDER BY r.sequence DESC LIMIT :limit',
                          v=vault_id, f=file_id, before=before, end=end, limit=limit+1)
            items = [dict(value) for value in values[:limit]]
            head = current(conn, vault_id, file_id)
            return {'items': items, 'boundary': end, 'has_more': len(values) > limit,
                    'next_before': items[-1]['sequence'] if len(values) > limit else None,
                    'current': dict(head) if head else None}

    @app.get('/sync/v1/vaults/{vault_id}/history/{file_id}/{sequence}/preview')
    def preview(vault_id: str, file_id: str, sequence: int, authorization: str = Header(default='')):
        with db.transaction() as conn:
            vault(conn, vault_id, authorization)
            revision = row(conn, 'SELECT * FROM revisions WHERE vault_id=:v AND file_id=:f AND sequence=:s',
                           v=vault_id, f=file_id, s=sequence)
            if not revision:
                raise error(404, 'REVISION_NOT_FOUND')
            metadata = {'sequence': sequence, 'path': revision['path'], 'size': revision['size'], 'hash': revision['hash']}
            if revision['operation'] == 'delete':
                return dict(metadata, kind='deleted')
            image = PurePosixPath(revision['path']).suffix.casefold() in IMAGE_SUFFIXES
            limit = IMAGE_LIMIT if image else TEXT_LIMIT
            if revision['size'] > limit:
                return dict(metadata, kind='unsupported', reason='PREVIEW_TOO_LARGE', limit=limit)
            stored = row(conn, 'SELECT size FROM objects WHERE vault_id=:v AND hash=:h', v=vault_id, h=revision['hash'])
            if not stored or stored['size'] != revision['size']:
                raise error(503, 'STORAGE_INTEGRITY')
        if not preview_slots.acquire(blocking=False):
            raise error(429, 'PREVIEW_BUSY')
        try:
            try:
                with objects.open(vault_id + '/' + revision['hash']) as stream:
                    data = stream.read(limit+1)
            except (OSError, BotoCoreError, ClientError):
                raise error(503, 'STORAGE_UNAVAILABLE') from None
            if len(data) != revision['size'] or hashlib.sha256(data).hexdigest() != revision['hash']:
                raise error(503, 'STORAGE_INTEGRITY')
            return dict(metadata, **preview_content(data, revision['path']))
        finally:
            preview_slots.release()

    @app.post('/sync/v1/vaults/{vault_id}/files/{file_id}/restore')
    def restore(vault_id: str, file_id: str, body: Restore, authorization: str = Header(default='')):
        fingerprint = hashlib.sha256(('restore\0'+file_id+'\0'+body.model_dump_json()).encode()).hexdigest()
        with db.transaction() as conn:
            session, item = vault(conn, vault_id, authorization, lock=True)
            previous = row(conn, 'SELECT * FROM revisions WHERE vault_id=:v AND operation_id=:op',
                           v=vault_id, op=body.operation_id)
            if previous:
                if previous['fingerprint'] != fingerprint or previous['device_id'] != session['device_id']:
                    raise error(409, 'IDEMPOTENCY_REUSED')
                return dict(previous)
            source = row(conn, 'SELECT * FROM revisions WHERE vault_id=:v AND file_id=:f AND sequence=:s',
                         v=vault_id, f=file_id, s=body.source_revision)
            if not source:
                raise error(404, 'REVISION_NOT_FOUND')
            if source['operation'] != 'put':
                raise error(422, 'RESTORE_SOURCE_DELETED')
            head = current(conn, vault_id, file_id)
            if not head:
                raise error(404, 'FILE_NOT_FOUND')
            commit = Commit(operation_id=body.operation_id, file_id=file_id, base_revision=body.base_revision,
                            path=body.path if body.path is not None else head['path'], operation='put',
                            content_hash=source['hash'], size=source['size'])
            return perform_commit(conn, vault_id, commit, session, item,
                                  fingerprint=fingerprint, restored_from=body.source_revision)
