import os

from fastapi import FastAPI, Header, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator

APP_NAME = os.getenv("APP_NAME", "Demo API")
APP_ENV = os.getenv("APP_ENV", "local")
API_TOKEN = os.getenv("API_TOKEN", "")

app = FastAPI(title=APP_NAME)

ITEMS = [
    {"id": 1, "name": "prezentacia"},
    {"id": 2, "name": "beta"},
    {"id": 3, "name": "delta"},
]


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/info")
def info():
    return {"app": APP_NAME, "env": APP_ENV}


@app.get("/api/items")
def items():
    return {"items": ITEMS}


@app.get("/api/admin")
def admin(x_api_token: str = Header(default="")):
    if not API_TOKEN or x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")
    return {"area": "admin", "granted": True}


Instrumentator().instrument(app).expose(app)
