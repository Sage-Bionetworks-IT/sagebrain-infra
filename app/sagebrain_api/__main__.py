"""`python -m sagebrain_api` — the container entrypoint."""

import logging
import os

import uvicorn


def main() -> None:
    # JSON lines on stdout -> CloudWatch (awslogs driver).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uvicorn.run(
        "sagebrain_api.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        # AccessLogMiddleware writes the access log; the app reads X-Forwarded-For itself.
        access_log=False,
        proxy_headers=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
