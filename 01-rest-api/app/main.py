from fastapi import FastAPI

app = FastAPI(
    title="REST API Lab",
    description="Phase 1: REST fundamentals",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "01-rest-api"}
