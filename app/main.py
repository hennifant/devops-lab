from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="DevOps Lab API")

Instrumentator().instrument(app).expose(app)


@app.get("/")
def root():
    return {"message": "Hello from the DevOps Lab"}


@app.get("/health")
def health():
    return {"status": "healthy"}