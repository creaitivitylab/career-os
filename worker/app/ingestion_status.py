def direct_run_status(requested: int, successful: int) -> str:
    """Empty discovery is a no-op; valid empty boards count as successful."""
    return "failed" if requested > 0 and successful == 0 else "success"


ALL_TENANTS_FAILED = "No requested tenant/board produced usable source data"
