import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gexc

from app.main import app, get_processor
from tests.conftest import LANDING, make_envelope


@pytest.fixture
def client(processor):
    app.dependency_overrides[get_processor] = lambda: processor
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_pubsub_success(client, storage):
    storage.put(
        LANDING,
        "customer_20261015.csv",
        b"customer_id,first_name,last_name,email,phone,country,signup_date,is_active\n"
        b"1,Ana,Lee,ana@x.com,,UK,2026-01-01,true\n",
    )
    body = make_envelope("customer_20261015.csv").model_dump(by_alias=True, mode="json")
    resp = client.post("/pubsub", json=body)
    assert resp.status_code == 200
    assert resp.json()["status"] == "SUCCESS"


@pytest.mark.parametrize(
    "body", [{}, {"message": {}}, {"message": {"messageId": "1", "data": "!!notbase64"}}]
)
def test_malformed_message_is_400(client, body):
    assert client.post("/pubsub", json=body).status_code == 400


def test_error_before_job_registered_is_500(client, audit):
    audit.fail_insert_job = gexc.ServiceUnavailable("bq down")
    body = make_envelope("customer_20261015.csv").model_dump(by_alias=True, mode="json")
    assert client.post("/pubsub", json=body).status_code == 500
