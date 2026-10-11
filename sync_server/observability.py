"""Bounded read-only operational observations and an explicit redaction whitelist."""
from fastapi import Header, Query
from .database import row, rows
from .maintenance import HISTORY_LIMIT
from .operation_journal import OperationJournal
from .operations import OperationsError

HISTORY_FIELDS = ('id', 'kind', 'started_at', 'finished_at', 'duration_ms', 'selected', 'removed', 'released_bytes', 'filesystem_failures', 'metadata_failures')
REMINDER_SECONDS = 86400


def snapshot(conn, *, now, limit=20, before=None):
    condition = ''
    values = {'limit': limit+1}
    if before:
        finished, identifier = before.split(':')
        values.update(finished=int(finished), id=identifier)
        condition = ' WHERE finished_at<:finished OR (finished_at=:finished AND id<:id)'
    found = rows(conn, 'SELECT '+','.join(HISTORY_FIELDS)+' FROM maintenance_history'+condition+' ORDER BY finished_at DESC,id DESC LIMIT :limit', **values)
    items = [dict(r) for r in found[:limit]]
    latest = row(conn, "SELECT * FROM maintenance_summary WHERE kind='uploads'")
    capacity = row(conn, '''SELECT COUNT(*) AS vaults,
        COALESCE(SUM(CASE WHEN v.quota<=v.used+COALESCE(u.reserved,0) THEN 1 ELSE 0 END),0) AS blocked,
        COALESCE(SUM(CASE WHEN v.quota>v.used+COALESCE(u.reserved,0) AND v.used+COALESCE(u.reserved,0)>=v.quota-v.quota/5 THEN 1 ELSE 0 END),0) AS near_quota
        FROM vaults v LEFT JOIN (SELECT vault_id,SUM(size) AS reserved FROM uploads WHERE expires>:now GROUP BY vault_id) u ON u.vault_id=v.id''', now=now)
    ledger = row(conn, 'SELECT COUNT(*) AS count FROM vaults v WHERE v.used<>(SELECT COALESCE(SUM(o.size),0) FROM objects o WHERE o.vault_id=v.id)')['count']
    pending = row(conn, "SELECT COUNT(*) AS count FROM reclamation_objects WHERE state='prepared'")['count']
    return {'schema_version': 1, 'snapshot_only': True, 'observed_at': now,
        'history': {'items': items, 'retention_limit': HISTORY_LIMIT, 'idle_checks_recorded': False,
                    'next_before': f"{items[-1]['finished_at']}:{items[-1]['id']}" if len(found)>limit else None},
        'latest_cleanup': dict(latest) if latest else None,
        'capacity': {**{k:int(v) for k,v in capacity.items()}, 'warning_percent':80, 'basis':'charged_objects_and_active_upload_reservations'},
        'accounting_mismatches': int(ledger), 'pending_reclamation_objects': int(pending),
        'history_retention': 'indefinite', 'cache_seconds': 0}


def redacted(report):
    # Do not forward arbitrary response dictionaries into a support export.
    fields = HISTORY_FIELDS[1:]
    receipts = report['backup_receipts']
    summary = {key:receipts[key] for key in ('configured','threshold_seconds','archive_association_known','unfinished_operations','failed_operations')}
    for kind in ('backup','verify'):
        summary[kind] = None if receipts[kind] is None else {key:receipts[kind][key] for key in ('finished_utc','seconds_ago','state','recorded_backup_age_seconds')}
    return {'schema_version':1, 'redacted':True, 'observed_at':report['observed_at'],
        'history_retention':'indefinite', 'cache_seconds':0,
        'capacity': {key:report['capacity'][key] for key in ('vaults','blocked','near_quota','warning_percent','basis')},
        'accounting_mismatches':report['accounting_mismatches'], 'pending_reclamation_objects':report['pending_reclamation_objects'],
        'cleanup_retention_limit':HISTORY_LIMIT,
        'cleanup_page': [{key:item[key] for key in fields} for item in report['history']['items']],
        'backup_receipts': summary}


def register_observability(app, db, authorize, error, clock, operations_path):
    def report(authorization, limit, before):
        observed = clock(); now = int(observed)
        with db.read_only() as conn:
            authorize(conn, authorization, observe=False)
            if before and int(before.split(':')[0])>2**53-1: raise error(422,'INVALID_REQUEST')
            result = snapshot(conn, now=now, limit=limit, before=before)
        # This original CLI receipt store is independently observed, not part of
        # the database snapshot and not evidence for one specific archive.
        receipts = {'backup':None, 'verify':None, 'unfinished_operations':0, 'failed_operations':0}
        if operations_path is not None:
            try: receipts = OperationJournal(operations_path).reminders(now_ms=int(observed*1000))
            except OperationsError: raise error(503,'OPERATIONS_STATUS_UNAVAILABLE') from None
        result['backup_receipts'] = {**receipts, 'configured':operations_path is not None,
            'threshold_seconds':REMINDER_SECONDS, 'archive_association_known':False}
        return result

    @app.get('/sync/v1/admin/observability')
    def observations(limit: int=Query(default=20,ge=1,le=100),
                     before: str | None=Query(default=None,pattern=r'^[0-9]{1,16}:[0-9a-f]{32}$'),
                     authorization: str=Header(default='')):
        return report(authorization, limit, before)

    @app.get('/sync/v1/admin/observability/export')
    def export(authorization: str=Header(default='')):
        return redacted(report(authorization, 20, None))
