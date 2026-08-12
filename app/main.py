import os

import psycopg
from fastapi import FastAPI

app = FastAPI(title="DevOps Lab API")


@app.get("/")
def root():
    return {"message": "Hello from the DevOps Lab"}


@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/info")
def info():
    return {
        "service": "devops-lab-api",
        "version": "0.1",
    }

@app.get("/db")
def database():
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()

    return {
        "database": "connected",
        "result": result[0],
    }

