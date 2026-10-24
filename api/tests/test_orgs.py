import asyncpg

from tests.conftest import APP_URL, PASSWORD, WORKER_URL, register_and_login


async def _create_org(client, headers, name="Acme") -> dict:
    r = await client.post(
        "/api/v1/orgs", json={"name": name, "timezone": "Europe/London"}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_org_and_list_mine(client):
    u = await register_and_login(client)
    org = await _create_org(client, u["headers"])
    assert org["role"] == "admin"
    assert len(org["ingest_secret"]) >= 40
    r = await client.get("/api/v1/orgs", headers=u["headers"])
    assert [o["org_id"] for o in r.json()["items"]] == [org["org_id"]]
    me = (await client.get("/api/v1/me", headers=u["headers"])).json()
    assert me["orgs"] == [{"org_id": org["org_id"], "name": "Acme", "role": "admin"}]


async def test_create_org_rejects_bad_timezone(client):
    u = await register_and_login(client)
    r = await client.post(
        "/api/v1/orgs", json={"name": "X", "timezone": "Mars/Base"}, headers=u["headers"]
    )
    assert r.status_code == 422


async def _count(conn, user_id, sql, *args) -> int:
    async with conn.transaction():
        await conn.execute("select set_config('app.user_id', $1, true)", user_id)
        return await conn.fetchval(sql, *args)


async def test_tenant_isolation_api_and_sql(client):
    a = await register_and_login(client)
    b = await register_and_login(client)
    org_a = (await _create_org(client, a["headers"], "Org A"))["org_id"]
    org_b = (await _create_org(client, b["headers"], "Org B"))["org_id"]

    # Through the API: B sees only its own org and gets 404 on A's members.
    items = (await client.get("/api/v1/orgs", headers=b["headers"])).json()["items"]
    assert [o["org_id"] for o in items] == [org_b]
    r = await client.get(f"/api/v1/orgs/{org_a}/members", headers=b["headers"])
    assert r.status_code == 404
    r = await client.post(
        f"/api/v1/orgs/{org_a}/members", json={"email": b["email"]}, headers=b["headers"]
    )
    assert r.status_code == 404

    # Through raw SQL on the app role: RLS hides org A from B on several tenant tables.
    app_conn = await asyncpg.connect(APP_URL)
    try:
        for table in ("organizations", "org_members", "integrations"):
            col = "id" if table == "organizations" else "org_id"
            sql = f"select count(*) from {table} where {col} = $1::uuid"
            assert await _count(app_conn, b["user_id"], sql, org_a) == 0, table
            assert await _count(app_conn, a["user_id"], sql, org_a) == 1, table
        # No user id set -> nothing visible at all.
        assert await app_conn.fetchval("select count(*) from organizations") == 0
        # create_org refuses without a user.
        try:
            await app_conn.fetchval("select public.create_org('x', 'UTC', '\\x00'::bytea)")
            raise AssertionError("create_org should fail without app.user_id")
        except asyncpg.InsufficientPrivilegeError:
            pass
    finally:
        await app_conn.close()

    # The worker role bypasses RLS and sees both orgs.
    worker = await asyncpg.connect(WORKER_URL)
    try:
        n = await worker.fetchval(
            "select count(*) from organizations where id = any($1::uuid[])", [org_a, org_b]
        )
        assert n == 2
    finally:
        await worker.close()


async def test_invite_existing_user_and_new_email(client):
    admin = await register_and_login(client)
    other = await register_and_login(client)
    org_id = (await _create_org(client, admin["headers"]))["org_id"]

    r = await client.post(
        f"/api/v1/orgs/{org_id}/members",
        json={"email": other["email"], "role": "lead"},
        headers=admin["headers"],
    )
    assert r.status_code == 201
    assert r.json() == {"user_id": other["user_id"], "role": "lead"}
    # A lead is not an admin -> cannot invite.
    r = await client.post(
        f"/api/v1/orgs/{org_id}/members", json={"email": "z@example.com"}, headers=other["headers"]
    )
    assert r.status_code == 403

    r = await client.post(
        f"/api/v1/orgs/{org_id}/members",
        json={"email": "newbie@example.com", "role": "viewer"},
        headers=admin["headers"],
    )
    assert r.status_code == 201
    token = r.json()["invite_token"]

    r = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "password": PASSWORD, "display_name": "Newbie"},
    )
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/api/v1/me", headers=headers)).json()
    assert me["email"] == "newbie@example.com"
    assert me["orgs"] == [{"org_id": org_id, "name": "Acme", "role": "viewer"}]
    # The invite is single-use.
    r = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": token, "password": PASSWORD, "display_name": "Newbie"},
    )
    assert r.status_code == 400

    members = (await client.get(f"/api/v1/orgs/{org_id}/members", headers=headers)).json()
    assert {m["role"] for m in members["items"]} == {"admin", "lead", "viewer"}
