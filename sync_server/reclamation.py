"""Manually reviewed, backed-up reclamation without pruning revision history.

Prepared intentions commit before object deletion. Every HTTP writer takes the
same vault lock and refuses a prepared hash until the operator resumes that
exact plan. Lost delete or database replies therefore remain reconcilable.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import time
import uuid

from .database import row, rows, run
from .operations import (CONTENT_HASH, VAULT_ID, OperationsError, _load_manifest,
                         _verify_backup_file, sha256_file, verify_backup)

POLICY = {
    'history_retention': 'indefinite', 'automatic_reclamation': False,
    'default_grace_hours': 168, 'minimum_grace_hours': 24,
    'protection': ['all_revisions', 'pending_uploads', 'recent_completion_receipts',
                   'object_grace_period', 'verified_backup', 'accounting_consistency'],
    'completion_after_reclamation': 'new_upload_required',
}
ERROR_CODES = {'GC_PLAN_STALE', 'GC_REFERENCE_CHANGED', 'GC_ACCOUNTING_MISMATCH',
               'GC_REFERENCES_INCONSISTENT', 'BACKUP_OBJECT_MISSING',
               'BACKUP_OBJECT_INTEGRITY_FAILED'}


def pending(conn, vault_id, content_hash):
    return bool(row(conn, "SELECT 1 FROM reclamation_objects WHERE vault_id=:v AND hash=:h AND state='prepared' LIMIT 1",
                    v=vault_id, h=content_hash))


def reclaimed(conn, vault_id, content_hash):
    return bool(row(conn, "SELECT 1 FROM reclamation_objects WHERE vault_id=:v AND hash=:h AND state='reclaimed' LIMIT 1",
                    v=vault_id, h=content_hash))


def _vault(conn, db, vault_id):
    if not VAULT_ID.fullmatch(vault_id):
        raise OperationsError('GC_VAULT_INVALID')
    suffix = '' if db.sqlite else ' FOR UPDATE'
    vault = row(conn, 'SELECT * FROM vaults WHERE id=:v' + suffix, v=vault_id)
    if not vault:
        raise OperationsError('GC_VAULT_NOT_FOUND')
    charged = row(conn, 'SELECT COALESCE(SUM(size),0) AS bytes FROM objects WHERE vault_id=:v', v=vault_id)['bytes']
    if charged != vault['used']:
        raise OperationsError('GC_ACCOUNTING_MISMATCH')
    if row(conn, 'SELECT 1 FROM revisions r LEFT JOIN objects o ON o.vault_id=r.vault_id AND o.hash=r.hash WHERE r.vault_id=:v AND r.hash IS NOT NULL AND o.hash IS NULL LIMIT 1', v=vault_id) or row(conn, 'SELECT 1 FROM files f LEFT JOIN revisions r ON r.vault_id=f.vault_id AND r.sequence=f.sequence WHERE f.vault_id=:v AND r.sequence IS NULL LIMIT 1', v=vault_id):
        raise OperationsError('GC_REFERENCES_INCONSISTENT')
    return vault


def _protected(conn, vault_id, item, cutoff):
    if row(conn, 'SELECT 1 FROM revisions WHERE vault_id=:v AND hash=:h LIMIT 1', v=vault_id, h=item['hash']):
        return 'revision'
    # Expired rows are protected until normal staging cleanup reconciles them.
    if row(conn, 'SELECT 1 FROM uploads WHERE vault_id=:v AND hash=:h LIMIT 1', v=vault_id, h=item['hash']):
        return 'upload'
    if pending(conn, vault_id, item['hash']):
        return 'pending_reclamation'
    if item['created'] > cutoff:
        return 'grace_period'
    if row(conn, 'SELECT 1 FROM upload_receipts WHERE vault_id=:v AND hash=:h AND completed>:cutoff LIMIT 1',
           v=vault_id, h=item['hash'], cutoff=cutoff):
        return 'completion_receipt'
    return None


def _backup(source, max_age_hours, workers, expected_hash=None):
    try:
        source = Path(source).resolve(strict=True)
        before = sha256_file(source / 'manifest.json')
        if expected_hash and before != expected_hash:
            raise OperationsError('GC_BACKUP_CHANGED')
        verify_backup(source, workers=workers, max_age_hours=max_age_hours)
        manifest = _load_manifest(source, max_age_hours)
        after = sha256_file(source / 'manifest.json')
        if before != after:
            raise OperationsError('GC_BACKUP_CHANGED')
        return source, manifest, after
    except OSError:
        raise OperationsError('GC_BACKUP_UNAVAILABLE') from None


def preview(db, source, vault_id, *, grace_hours=168, limit=100, max_age_hours=24, workers=8, now=None, actor='operator-cli'):
    if isinstance(grace_hours, bool) or not isinstance(grace_hours, (int, float)) or not math.isfinite(grace_hours) or not 24 <= grace_hours <= 24*3650:
        raise OperationsError('GC_GRACE_INVALID')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise OperationsError('GC_LIMIT_INVALID')
    now = int(time.time() if now is None else now)
    cutoff = now - int(grace_hours * 3600)
    _, manifest, backup_hash = _backup(source, max_age_hours, workers)
    backed = {(item['vault_id'], item['hash']): item['size'] for item in manifest['objects']}
    selected = []
    protected = {}
    eligible = eligible_bytes = 0
    with db.transaction() as conn:
        vault = _vault(conn, db, vault_id)
        # Streaming rows keep large vault inventories out of an in-memory list.
        inventory = run(conn, 'SELECT hash,size,created FROM objects WHERE vault_id=:v ORDER BY hash', v=vault_id).mappings()
        for item in inventory:
            if not CONTENT_HASH.fullmatch(item['hash']):
                raise OperationsError('OBJECT_ID_INVALID')
            reason = _protected(conn, vault_id, item, cutoff)
            if not reason and backed.get((vault_id, item['hash'])) != item['size']:
                reason = 'absent_from_backup'
            if reason:
                protected[reason] = protected.get(reason, 0) + 1
                continue
            eligible += 1
            eligible_bytes += item['size']
            if len(selected) < limit:
                selected.append(dict(item))
        snapshot = {'sequence': vault['sequence'], 'charged_bytes': vault['used'],
                    'grace_hours': grace_hours, 'cutoff': cutoff, 'policy': POLICY,
                    'candidates': selected, 'eligible_objects': eligible,
                    'candidate_bytes': sum(item['size'] for item in selected), 'eligible_bytes': eligible_bytes,
                    'protected_counts': protected, 'has_more': eligible > len(selected)}
        plan_id = uuid.uuid4().hex
        run(conn, "INSERT INTO reclamation_plans VALUES (:id,:v,:actor,:now,:expires,:backup,:snapshot,'preview',NULL,'GC_PREVIEW_READY')",
            id=plan_id, v=vault_id, actor=actor, now=now, expires=now+3600,
            backup=backup_hash, snapshot=json.dumps(snapshot, sort_keys=True, separators=(',', ':')))
    return status(db, plan_id)


def status(db, plan_id):
    if not VAULT_ID.fullmatch(plan_id):
        raise OperationsError('GC_PLAN_INVALID')
    with db.transaction() as conn:
        plan = row(conn, 'SELECT * FROM reclamation_plans WHERE id=:id', id=plan_id)
        if not plan:
            raise OperationsError('GC_PLAN_NOT_FOUND')
        items = [dict(item) for item in rows(conn, 'SELECT hash,size,state,code,confirmed_at FROM reclamation_objects WHERE plan_id=:id ORDER BY hash', id=plan_id)]
        snapshot = json.loads(plan['snapshot'])
        return {'schema_version': 1, 'plan_id': plan['id'], 'vault_id': plan['vault_id'],
                'state': plan['state'], 'code': plan['code'], 'actor_id': plan['actor_id'],
                'created': plan['created'], 'expires': plan['expires'], 'finished': plan['finished'],
                'backup_manifest_sha256': plan['backup_hash'], 'preview': snapshot, 'items': items,
                'reclaimed_bytes': sum(item['size'] for item in items if item['state'] == 'reclaimed'),
                'reclaimed_objects': sum(item['state'] == 'reclaimed' for item in items),
                'protected_objects': sum(item['state'] == 'protected' for item in items),
                'pending_objects': sum(item['state'] == 'prepared' for item in items)}


def _prepare(db, plan_id, now):
    with db.transaction() as conn:
        plan = row(conn, 'SELECT * FROM reclamation_plans WHERE id=:id', id=plan_id)
        if not plan:
            raise OperationsError('GC_PLAN_NOT_FOUND')
        vault = _vault(conn, db, plan['vault_id'])
        # Re-read after taking the same lock used by competing operators/writers.
        plan = row(conn, 'SELECT * FROM reclamation_plans WHERE id=:id', id=plan_id)
        if plan['state'] in {'complete', 'running'}:
            return
        snapshot = json.loads(plan['snapshot'])
        if plan['expires'] <= now:
            raise OperationsError('GC_PLAN_EXPIRED')
        if vault['sequence'] != snapshot['sequence'] or vault['used'] != snapshot['charged_bytes']:
            raise OperationsError('GC_PLAN_STALE')
        for item in snapshot['candidates']:
            current = row(conn, 'SELECT hash,size,created FROM objects WHERE vault_id=:v AND hash=:h', v=plan['vault_id'], h=item['hash'])
            if not current or dict(current) != item or _protected(conn, plan['vault_id'], current, snapshot['cutoff']):
                raise OperationsError('GC_PLAN_STALE')
        for item in snapshot['candidates']:
            run(conn, "INSERT INTO reclamation_objects VALUES (:id,:v,:h,:size,'prepared','GC_PREPARED',:now)",
                id=plan_id, v=plan['vault_id'], h=item['hash'], size=item['size'], now=now)
        run(conn, "UPDATE reclamation_plans SET state='running',code='GC_RUNNING' WHERE id=:id", id=plan_id)


def apply(db, objects, source, plan_id, *, confirm_plan, max_age_hours=24, workers=8, now=None):
    if confirm_plan != plan_id:
        raise OperationsError('GC_CONFIRMATION_REQUIRED')
    receipt = status(db, plan_id)
    if receipt['state'] == 'complete':
        return receipt
    source, manifest, _ = _backup(source, max_age_hours, workers, receipt['backup_manifest_sha256'])
    backed = {(item['vault_id'], item['hash']): item for item in manifest['objects']}
    for candidate in receipt['preview']['candidates']:
        backup_item = backed.get((receipt['vault_id'], candidate['hash']))
        if not backup_item or backup_item['size'] != candidate['size']:
            raise OperationsError('GC_BACKUP_CHANGED')
    now = int(time.time() if now is None else now)
    _prepare(db, plan_id, now)
    for candidate in receipt['preview']['candidates']:
        try:
            with db.transaction() as conn:
                _vault(conn, db, receipt['vault_id'])
                item = row(conn, 'SELECT * FROM reclamation_objects WHERE plan_id=:id AND hash=:h', id=plan_id, h=candidate['hash'])
                if item['state'] != 'prepared':
                    continue
                # Another application must not add references behind the HTTP guard.
                if row(conn, 'SELECT 1 FROM revisions WHERE vault_id=:v AND hash=:h LIMIT 1', v=receipt['vault_id'], h=candidate['hash']) or row(conn, 'SELECT 1 FROM uploads WHERE vault_id=:v AND hash=:h LIMIT 1', v=receipt['vault_id'], h=candidate['hash']) or row(conn, 'SELECT 1 FROM upload_receipts WHERE vault_id=:v AND hash=:h AND completed>:cutoff LIMIT 1', v=receipt['vault_id'], h=candidate['hash'], cutoff=receipt['preview']['cutoff']):
                    run(conn, "UPDATE reclamation_objects SET state='protected',code='GC_REFERENCE_CHANGED',confirmed_at=:now WHERE plan_id=:id AND hash=:h",
                        id=plan_id, h=candidate['hash'], now=now)
                    continue
                current = row(conn, 'SELECT size FROM objects WHERE vault_id=:v AND hash=:h', v=receipt['vault_id'], h=candidate['hash'])
                if not current or current['size'] != candidate['size']:
                    raise OperationsError('GC_PLAN_STALE')
                # Verify the actual recovery bytes again immediately before delete.
                _verify_backup_file(source, backed[(receipt['vault_id'], candidate['hash'])])
                objects.delete(receipt['vault_id'] + '/' + candidate['hash'])
                run(conn, 'DELETE FROM objects WHERE vault_id=:v AND hash=:h', v=receipt['vault_id'], h=candidate['hash'])
                run(conn, 'UPDATE vaults SET used=used-:size WHERE id=:v', size=candidate['size'], v=receipt['vault_id'])
                run(conn, "UPDATE reclamation_objects SET state='reclaimed',code='GC_OBJECT_RECLAIMED',confirmed_at=:now WHERE plan_id=:id AND hash=:h",
                    id=plan_id, h=candidate['hash'], now=now)
        except Exception as error:
            code = str(error) if isinstance(error, OperationsError) and str(error) in ERROR_CODES else 'GC_STORAGE_OR_DATABASE_UNAVAILABLE'
            # Read-only reconciliation is mandatory if writing even this code fails.
            with db.transaction() as conn:
                _vault(conn, db, receipt['vault_id'])
                run(conn, "UPDATE reclamation_objects SET code=:code,confirmed_at=:now WHERE plan_id=:id AND hash=:h AND state='prepared'",
                    id=plan_id, h=candidate['hash'], code=code, now=now)
    with db.transaction() as conn:
        _vault(conn, db, receipt['vault_id'])
        remaining = row(conn, "SELECT COUNT(*) AS n FROM reclamation_objects WHERE plan_id=:id AND state='prepared'", id=plan_id)['n']
        if not remaining:
            run(conn, "UPDATE reclamation_plans SET state='complete',code='GC_COMPLETE',finished=:now WHERE id=:id", id=plan_id, now=now)
    return status(db, plan_id)
