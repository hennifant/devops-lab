from fastapi import FastAPI

app = FastAPI(title="DevOps Lab API")


@app.get("/")
def root():
    return {"message": "Hello from the DevOps Lab"}


@app.get("/health")
def health():
    return {"status": "healthy"}