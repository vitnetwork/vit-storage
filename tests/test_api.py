import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "quantum_stable"

def test_api_status():
    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["module"] == "tachyon.api"

def test_upload_placeholder():
    # Since we don't have real providers configured, we just test the endpoint existence
    files = {'file': ('test.txt', b'hello world')}
    response = client.post("/api/v1/upload", files=files)
    assert response.status_code == 200
    assert response.json()["status"] == "uploaded"
