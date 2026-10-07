"""Configured-operator management with transactionally immutable receipts."""
import hashlib
import json
import secrets
from typing import Annotated

from fastapi import Header, Path, Query
from pydantic import Field, StrictInt, field_validator

from .database import password_hash, row, rows, run
from .models import DTO

Identifier = Annotated[str, Path(pattern=r'^[0-9a-f]{32}$')]


class Operation(DTO):
    operation_id: str = Field(pattern=r'^[0-9a-f]{32}$')


class AccountCreate(Operation):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    default_quota: StrictInt = Field(ge=0, le=2**53-1)

    @field_validator('username')
    @classmethod
    def account_name(cls, value):
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Invalid account name')
        return value


class VaultQuota(Operation):
    expected_quota: StrictInt = Field(ge=0, le=2**53-1)
    quota: StrictInt = Field(ge=0, le=2**53-1)


class DefaultQuota(Operation):
    expected_revision: StrictInt = Field(ge=0, le=2**53-1)
    quota: StrictInt = Field(ge=0, le=2**53-1)


def register_admin(app, db, identity, error, clock, operators, default_quota, readiness):
    def authorize(conn, authorization):
        actor = identity(conn, authorization)
        if actor['user_id'] not in operators:
            raise error(403, 'OPERATIONS_FORBIDDEN')
        return actor['user_id']

    def account(conn, user_id):
        user = row(conn, 'SELECT id,username FROM users WHERE id=:id', id=user_id)
        if not user:
            raise error(404, 'ACCOUNT_NOT_FOUND')
        return user

    def fingerprint(kind, target, body, salt):
        # Password retries cost the same scrypt work as authentication. No raw
        # password or cheap dictionary-verifiable digest is saved in the receipt.
        values = body.model_dump(exclude={'password'})
        if isinstance(body, AccountCreate):
            values['credential_proof'] = password_hash(body.password, salt)
        return hashlib.sha256(json.dumps([kind,target,values],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def perform(conn, actor, kind, target, body, action):
        if not db.sqlite:
            run(conn, 'SELECT pg_advisory_xact_lock(1330534489)')
        previous = row(conn, 'SELECT * FROM admin_receipts WHERE id=:id', id=body.operation_id)
        salt = previous['input_salt'] if previous else secrets.token_hex(16)
        digest = fingerprint(kind,target,body,salt)
        if previous:
            if previous['actor_id'] != actor or previous['fingerprint'] != digest:
                raise error(409, 'IDEMPOTENCY_REUSED')
            return json.loads(previous['response'])
        result = {'schema_version':1, 'state':'completed', 'operation_id':body.operation_id,
                  'kind':kind, 'confirmed_at':int(clock()), **action()}
        run(conn, 'INSERT INTO admin_receipts VALUES (:id,:actor,:kind,:target,:salt,:fingerprint,:response,:now)',
            id=body.operation_id, actor=actor, kind=kind, target=target, salt=salt,
            fingerprint=digest,response=json.dumps(result,ensure_ascii=False),now=result['confirmed_at'])
        return result

    @app.get('/sync/v1/admin/access')
    def access(authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor = authorize(conn,authorization)
            return {'operator':True, 'user_id':actor, 'deployment_default_quota':default_quota}

    @app.get('/sync/v1/admin/accounts')
    def accounts(limit: int = Query(default=20,ge=1,le=100), before: str | None = Query(default=None,pattern=r'^[0-9a-f]{32}$'), authorization: str = Header(default='')):
        with db.transaction() as conn:
            authorize(conn,authorization)
            found = rows(conn, 'SELECT u.id,u.username,COALESCE(p.default_quota,:default) AS default_quota,COALESCE(p.revision,0) AS policy_revision FROM users u LEFT JOIN account_policy p ON p.user_id=u.id WHERE u.id<:before ORDER BY u.id DESC LIMIT :limit',default=default_quota,before=before or 'f'*33,limit=limit+1)
            visible = [dict(item) for item in found[:limit]]
            return {'items':visible,'next_before':visible[-1]['id'] if len(found)>limit else None,'confirmed_at':int(clock())}

    @app.get('/sync/v1/admin/accounts/{user_id}')
    def account_details(user_id: Identifier, limit: int = Query(default=20,ge=1,le=100), vault_before: str | None = Query(default=None,pattern=r'^[0-9a-f]{32}$'), device_before: str | None = Query(default=None,pattern=r'^[0-9a-f]{32}$'), authorization: str = Header(default='')):
        with db.transaction() as conn:
            authorize(conn,authorization)
            user = account(conn,user_id)
            policy=row(conn,'SELECT default_quota,revision FROM account_policy WHERE user_id=:id',id=user_id)
            vaults=[dict(item) for item in rows(conn,'SELECT v.id,v.name,v.quota,v.used,v.sequence,(SELECT COALESCE(SUM(size),0) FROM uploads u WHERE u.vault_id=v.id AND u.expires>:now) AS reserved_bytes FROM vaults v WHERE v.user_id=:id AND v.id<:before ORDER BY v.id DESC LIMIT :limit',id=user_id,before=vault_before or 'f'*33,limit=limit+1,now=int(clock()))]
            devices=[dict(item) for item in rows(conn,'SELECT id,name,revoked FROM devices WHERE user_id=:id AND id<:before ORDER BY id DESC LIMIT :limit',id=user_id,before=device_before or 'f'*33,limit=limit+1)]
            totals=dict(row(conn,'SELECT COUNT(*) AS vault_count,COALESCE(SUM(used),0) AS charged_bytes,COALESCE(SUM(quota),0) AS quota_bytes FROM vaults WHERE user_id=:id',id=user_id))
            return {'account':dict(user),'policy':dict(policy) if policy else {'default_quota':default_quota,'revision':0},
                    'vaults':vaults[:limit],'devices':devices[:limit],'totals':totals,
                    'vaults_next_before':vaults[limit-1]['id'] if len(vaults)>limit else None,
                    'devices_next_before':devices[limit-1]['id'] if len(devices)>limit else None,
                    'confirmed_at':int(clock())}

    @app.post('/sync/v1/admin/accounts')
    def create_account(body: AccountCreate, authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor=authorize(conn,authorization)
            def action():
                if row(conn,'SELECT id FROM users WHERE username=:name',name=body.username):
                    raise error(409,'ACCOUNT_EXISTS')
                user_id=secrets.token_hex(16)
                run(conn,'INSERT INTO users VALUES (:id,:name,:password)',id=user_id,name=body.username,password=password_hash(body.password))
                run(conn,'INSERT INTO account_policy VALUES (:id,:quota,1)',id=user_id,quota=body.default_quota)
                return {'user_id':user_id,'username':body.username,'default_quota':body.default_quota,'policy_revision':1}
            return perform(conn,actor,'create_account',body.username,body,action)

    @app.put('/sync/v1/admin/accounts/{user_id}/policy')
    def default_policy(user_id: Identifier, body: DefaultQuota, authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor=authorize(conn,authorization)
            def action():
                account(conn,user_id)
                previous=row(conn,'SELECT * FROM account_policy WHERE user_id=:id',id=user_id)
                revision=previous['revision'] if previous else 0
                if revision != body.expected_revision:
                    raise error(409,'POLICY_CHANGED',{'revision':revision})
                run(conn,'INSERT INTO account_policy VALUES (:id,:quota,:revision) ON CONFLICT(user_id) DO UPDATE SET default_quota=:quota,revision=:revision',id=user_id,quota=body.quota,revision=revision+1)
                return {'user_id':user_id,'default_quota':body.quota,'policy_revision':revision+1}
            return perform(conn,actor,'default_quota',user_id,body,action)

    @app.put('/sync/v1/admin/accounts/{user_id}/vaults/{vault_id}/quota')
    def vault_quota(user_id: Identifier, vault_id: Identifier, body: VaultQuota, authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor=authorize(conn,authorization)
            def action():
                account(conn,user_id)
                suffix='' if db.sqlite else ' FOR UPDATE'
                vault=row(conn,'SELECT * FROM vaults WHERE user_id=:user AND id=:v'+suffix,user=user_id,v=vault_id)
                if not vault: raise error(404,'VAULT_NOT_FOUND')
                if vault['quota'] != body.expected_quota: raise error(409,'QUOTA_CHANGED',{'quota':vault['quota']})
                # PostgreSQL SUM(bigint) yields Decimal; the byte count must
                # remain an exact JSON integer in both errors and receipts.
                reserved=int(row(conn,'SELECT COALESCE(SUM(size),0) AS bytes FROM uploads WHERE vault_id=:v AND expires>:now',v=vault_id,now=int(clock()))['bytes'])
                if body.quota < vault['used']+reserved:
                    raise error(409,'QUOTA_IN_USE',{'charged_bytes':vault['used'],'reserved_bytes':reserved})
                run(conn,'UPDATE vaults SET quota=:quota WHERE id=:v',quota=body.quota,v=vault_id)
                return {'user_id':user_id,'vault_id':vault_id,'previous_quota':vault['quota'],'quota':body.quota,'charged_bytes':vault['used'],'reserved_bytes':reserved}
            return perform(conn,actor,'vault_quota',user_id+'/'+vault_id,body,action)

    @app.post('/sync/v1/admin/accounts/{user_id}/devices/{device_id}/revoke')
    def revoke_device(user_id: Identifier, device_id: Identifier, body: Operation, authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor=authorize(conn,authorization)
            def action():
                account(conn,user_id)
                device=row(conn,'SELECT * FROM devices WHERE id=:id AND user_id=:user',id=device_id,user=user_id)
                if not device: raise error(404,'DEVICE_NOT_FOUND')
                run(conn,'UPDATE devices SET revoked=1 WHERE id=:id AND user_id=:user',id=device_id,user=user_id)
                return {'user_id':user_id,'device_id':device_id,'revoked':True}
            return perform(conn,actor,'revoke_device',user_id+'/'+device_id,body,action)

    @app.get('/sync/v1/admin/operations/{operation_id}')
    def operation_result(operation_id: Identifier, authorization: str = Header(default='')):
        with db.transaction() as conn:
            actor=authorize(conn,authorization)
            receipt=row(conn,'SELECT response FROM admin_receipts WHERE id=:id AND actor_id=:actor',id=operation_id,actor=actor)
            return json.loads(receipt['response']) if receipt else {'schema_version':1,'state':'not_found','operation_id':operation_id,'confirmed_at':int(clock())}

    @app.get('/sync/v1/admin/diagnostics')
    async def diagnostics(authorization: str = Header(default='')):
        with db.transaction() as conn:
            authorize(conn,authorization)
            ledger=row(conn,'SELECT COUNT(*) AS n FROM vaults v WHERE v.used<>(SELECT COALESCE(SUM(o.size),0) FROM objects o WHERE o.vault_id=v.id)')['n']
            pending=row(conn,"SELECT COUNT(*) AS n FROM reclamation_objects WHERE state='prepared'")['n']
            maintenance=row(conn,"SELECT * FROM maintenance_summary WHERE kind='uploads'")
        probe=await readiness.snapshot()
        return {'schema_version':1,'confirmed_at':int(clock()),'dependencies':probe,
                'accounting_mismatches':ledger,'pending_reclamation_objects':pending,
                'upload_maintenance':dict(maintenance) if maintenance else None}
