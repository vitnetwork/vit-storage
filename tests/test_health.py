"""Smoke test for the storage service health endpoint."""
from fastapi.testclient import TestClient

from tachyon.main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json().get("status") == "quantum_stable"
