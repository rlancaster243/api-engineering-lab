from fastapi import FastAPI

app = FastAPI(
    title="API Hardening Lab",
    description="Phase 3: security, resilience, and observability",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "03-api-hardening"}
