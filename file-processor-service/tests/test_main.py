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


# --- POST /contracts: a contract changed in the config bucket ----------------------------------
def contract_event(name: str, event_type: str = "OBJECT_FINALIZE") -> dict:
    return make_envelope(name, bucket="proj-poc-config", event_type=event_type).model_dump(
        by_alias=True, mode="json"
    )


def test_contract_change_refreshes_that_table(client, processor):
    refreshed = []
    processor.contracts.refresh = lambda key: refreshed.append(key)
    resp = client.post("/contracts", json=contract_event("contracts/supplementary_data.customer.yaml"))
    assert resp.status_code == 204
    assert refreshed == ["supplementary_data.customer"]

    client.post(
        "/contracts", json=contract_event("contracts/supplementary_data.customer.yaml", "OBJECT_DELETE")
    )
    assert refreshed == ["supplementary_data.customer"] * 2


@pytest.mark.parametrize(
    "name, event_type",
    [("contracts/README.md", "OBJECT_FINALIZE"), ("contracts/ds.t.yaml", "OBJECT_METADATA_UPDATE")],
)
def test_other_objects_and_events_are_ignored(client, processor, name, event_type):
    processor.contracts.refresh = lambda key: pytest.fail("should not refresh")
    assert client.post("/contracts", json=contract_event(name, event_type)).status_code == 204


def test_malformed_contract_message_is_400(client):
    assert client.post("/contracts", json={"message": {}}).status_code == 400


def test_a_failed_refresh_is_500_so_pubsub_retries(client, processor):
    def boom(key):
        raise RuntimeError("gcs down")

    processor.contracts.refresh = boom
    assert client.post("/contracts", json=contract_event("contracts/ds.t.yaml")).status_code == 500
