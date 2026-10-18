import os


def database_url() -> str | None:
    value = os.environ.get("DATABASE_URL")
    if value is None or value.strip() == "":
        return None
    return value


def require_database_url() -> str:
    url = database_url()
    if url is None:
        raise RuntimeError("DATABASE_URL is required")
    return url
