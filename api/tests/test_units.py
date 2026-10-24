"""Pure unit tests: no database needed."""

import io
from uuid import uuid4

import pytest
from fastapi import UploadFile
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models import FakeListChatModel

from app import crypto, llm
from app.config import Settings
from app.errors import ApiError
from app.storage import save_csv_upload
from app.vectorstore import open_client, open_collections, tenant_where


def test_tenant_where_always_includes_org():
    assert tenant_where("org-1") == {"org_id": "org-1"}
    w = tenant_where("org-1", has_issue=True, created_ts={"$gte": 5})
    assert w == {"$and": [{"org_id": "org-1"}, {"has_issue": True}, {"created_ts": {"$gte": 5}}]}
    # A caller-supplied org_id condition cannot replace the tenant filter.
    with pytest.raises(TypeError):
        tenant_where("org-1", org_id="org-2")  # type: ignore[misc]
    with pytest.raises(ValueError):
        tenant_where("")


def test_crypto_round_trip():
    token = crypto.encrypt("ingest-secret")
    assert b"ingest-secret" not in token
    assert crypto.decrypt(token) == "ingest-secret"


def test_short_jwt_secret_refused():
    with pytest.raises(ValueError, match="32 bytes"):
        Settings(JWT_SECRET="short", APP_ENCRYPTION_KEY="k")


def test_llm_factories_use_fakes():
    fake_chat = FakeListChatModel(responses=["hello"])
    fake_embed = DeterministicFakeEmbedding(size=8)
    llm.set_overrides(chat=fake_chat, strong=fake_chat, embed=fake_embed)
    try:
        assert llm.chat_model().invoke("hi").content == "hello"
        assert llm.chat_model(strong=True) is fake_chat
        assert len(llm.embeddings().embed_query("x")) == 8
    finally:
        llm.set_overrides()


def test_chroma_collections_and_model_check(tmp_path):
    client = open_client(str(tmp_path))
    cols = open_collections(client, "models/a")
    assert set(cols) == {"tickets", "issues"}
    assert cols["tickets"].metadata["embedding_model"] == "models/a"
    assert open_collections(client, "models/a")["issues"].name == "issues"
    with pytest.raises(RuntimeError, match="embedding model"):
        open_collections(client, "models/b")


async def test_csv_upload_streams_and_enforces_cap():
    org = uuid4()
    ok = UploadFile(io.BytesIO(b"a,b\n1,2\n"), filename="t.csv")
    path = await save_csv_upload(ok, org, "ok.csv", max_bytes=100)
    assert path.read_bytes() == b"a,b\n1,2\n"
    big = UploadFile(io.BytesIO(b"x" * 3000), filename="big.csv")
    with pytest.raises(ApiError) as e:
        await save_csv_upload(big, org, "big.csv", max_bytes=1000)
    assert e.value.status_code == 413
    assert not path.with_name("big.csv").exists()
    with pytest.raises(ApiError):
        await save_csv_upload(UploadFile(io.BytesIO(b"x"), filename="x.exe"), org, "x", 10)
