"""Read existing CLI plans and receipts without recomputing or executing them."""
import json

from .database import row, rows


def summary(plan, observed_at):
    snapshot = json.loads(plan['snapshot'])
    return {
        'plan_id': plan['id'], 'vault_id': plan['vault_id'], 'actor_id': plan['actor_id'],
        'state': plan['state'], 'code': plan['code'], 'created': plan['created'],
        'expires': plan['expires'], 'finished': plan['finished'],
        'expired': plan['expires'] <= observed_at,
        'sequence': snapshot['sequence'], 'charged_bytes': snapshot['charged_bytes'],
        'selected_objects': len(snapshot['candidates']),
        'candidate_bytes': snapshot['candidate_bytes'],
        'eligible_objects': snapshot['eligible_objects'], 'eligible_bytes': snapshot['eligible_bytes'],
        'has_more': snapshot['has_more'],
    }


def plan_page(conn, *, observed_at, limit, before, vault_id, state):
    clauses, params = [], {'limit': limit + 1}
    if before:
        created, plan_id = before.split(':')
        clauses.append('(created<:created OR (created=:created AND id<:id))')
        params.update(created=int(created), id=plan_id)
    if vault_id:
        clauses.append('vault_id=:vault')
        params['vault'] = vault_id
    if state:
        clauses.append('state=:state')
        params['state'] = state
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    found = rows(conn, 'SELECT * FROM reclamation_plans' + where + ' ORDER BY created DESC,id DESC LIMIT :limit', **params)
    items = [summary(plan, observed_at) for plan in found[:limit]]
    return {'schema_version': 1, 'snapshot_only': True, 'observed_at': observed_at, 'items': items,
            'next_before': f"{items[-1]['created']}:{items[-1]['plan_id']}" if len(found) > limit else None}


def plan_detail(conn, plan_id, *, observed_at):
    plan = row(conn, 'SELECT * FROM reclamation_plans WHERE id=:id', id=plan_id)
    if not plan:
        return None
    items = [dict(item) for item in rows(conn, 'SELECT hash,size,state,code,confirmed_at FROM reclamation_objects WHERE plan_id=:id ORDER BY hash', id=plan_id)]
    return {'schema_version': 1, 'snapshot_only': True, 'observed_at': observed_at,
            **summary(plan, observed_at), 'backup_manifest_sha256': plan['backup_hash'],
            'preview': json.loads(plan['snapshot']), 'items': items,
            'reclaimed_bytes': sum(item['size'] for item in items if item['state'] == 'reclaimed'),
            'reclaimed_objects': sum(item['state'] == 'reclaimed' for item in items),
            'protected_objects': sum(item['state'] == 'protected' for item in items),
            'pending_objects': sum(item['state'] == 'prepared' for item in items)}
