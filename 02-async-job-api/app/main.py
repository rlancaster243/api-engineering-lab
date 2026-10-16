from fastapi import FastAPI

app = FastAPI(
    title="Async Job API Lab",
    description="Phase 2: async jobs and status APIs",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "02-async-job-api"}
