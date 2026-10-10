from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_swagger_ok():
    r = client.get("/docs")
    assert r.status_code == 200

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

def test_employees_without_db():
    # When MONGO_URI is missing the app raises ConfigError on import,
    # so this test expects a 500 or 404 because startup never completed.
    r = client.get("/employees")
    assert r.status_code in (404, 500)
