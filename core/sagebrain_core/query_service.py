from .neptune import NeptuneConfig, NeptuneResult, execute_query
from .validation import validate_query


def run_query(
    query,
    *,
    principal: str,
    source: str,
    job_id: str,
    source_ip: str = "internal",
    user_agent: str = "sagebrain-internal",
    config: NeptuneConfig | None = None,
) -> NeptuneResult:
    """Validate and execute a query for an in-process caller.

    This is the agent's path to Neptune. It applies exactly the query limits and audit logging
    that POST /query applies, attributed to `principal` (the original caller).
    """
    query = validate_query(query)
    return execute_query(
        query,
        config=config or NeptuneConfig.from_env(),
        job_id=job_id,
        source=source,
        principal=principal,
        source_ip=source_ip,
        user_agent=user_agent,
    )
