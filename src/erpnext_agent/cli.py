import uvicorn

from erpnext_agent.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "erpnext_agent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
    )


if __name__ == "__main__":
    main()

