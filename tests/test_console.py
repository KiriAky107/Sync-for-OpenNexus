"""Same-origin Vue console delivery and its public account workflow."""

import re

from fastapi.testclient import TestClient

from sync_server.app import create_app
from sync_server.database import Database
from sync_server.storage import DiskObjects


def test_vue_console_is_hardened_and_uses_the_public_sync_api(tmp_path):
    database = Database("sqlite:///" + str(tmp_path / "sync.db"))
    app = create_app(
        database,
        DiskObjects(tmp_path / "objects"),
        tmp_path / "staging",
    )
    database.add_user("demo", "controlled-demo-password")
    with TestClient(app) as client:
        root = client.get("/")
        console = client.get("/console/")
        assert root.status_code == console.status_code == 200
        assert "OpenNexus Sync Console" in console.text
        assert "<script type=\"module\"" in console.text
        assert "controlled-demo-password" not in console.text
        for response in (root, console):
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

        assets = re.findall(r'(?:src|href)="(/console/assets/[^"]+)"', console.text)
        assert len(assets) == 2
        bodies = []
        for asset in assets:
            response = client.get(asset)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            bodies.append(response.content)
        bundle = b"".join(bodies)
        assert b"localStorage" not in bundle
        assert b"sessionStorage" not in bundle
        assert b"controlled-demo-password" not in bundle

        session = client.post(
            "/sync/v1/auth/sessions",
            json={
                "username": "demo",
                "password": "controlled-demo-password",
                "device_name": "Console test",
            },
        )
        assert session.status_code == 200
        token = session.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        created = client.post("/sync/v1/vaults", headers=headers, json={"name": "Demo Vault"})
        assert created.status_code == 200
        assert client.get("/sync/v1/vaults", headers=headers).json()["items"][0]["name"] == "Demo Vault"
        assert client.get("/sync/v1/devices", headers=headers).json()["items"][0]["name"] == "Console test"
    database.engine.dispose()
