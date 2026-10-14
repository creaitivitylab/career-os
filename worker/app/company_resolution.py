"""Exact company identity using the existing PostgreSQL normalization contract.

No Python normalization, fuzzy matching, display-name updates or implicit commit.
Existing duplicate rows resolve consistently to the oldest row, then UUID.
"""


def find_existing_company(cur, company_name: str | None):
    """Read-only resolution; safe for audit/preview, never locks or inserts."""
    if not company_name:
        return None
    cur.execute(
        """select id from public.companies
        where normalized_name = nullif(public.normalize_company_text(%s), '')
        order by created_at, id limit 1""",
        (company_name,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_or_create_company(cur, company_name: str | None):
    """Resolve/create inside the caller's transaction, without changing its scope.

    Creation requires READ COMMITTED and autocommit disabled: after waiting for
    a creator's transaction lock the second lookup must get a fresh snapshot.
    Locks are only needed on a lookup miss. All worker creators share this lock;
    uncooperative database writers still need a future uniqueness constraint.
    """
    if not company_name:
        return None
    existing = find_existing_company(cur, company_name)
    if existing is not None:
        return existing

    cur.execute(
        """select public.normalize_company_text(%s),
        current_setting('transaction_isolation'), current_setting('transaction_read_only')""",
        (company_name,),
    )
    key, isolation, read_only = cur.fetchone()
    if not key:
        return None
    if cur.connection.autocommit or isolation != "read committed" or read_only != "off":
        raise RuntimeError("Company creation requires a writable READ COMMITTED transaction with autocommit disabled")

    # A transaction lock survives every statement and releases on commit/rollback.
    # Hash collisions only serialize unrelated creators; equality uses the key.
    cur.execute(
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("career-os:company:" + key,),
    )
    cur.execute(
        """select id from public.companies where normalized_name = %s
        order by created_at, id limit 1""",
        (key,),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """insert into public.companies (name, normalized_name)
        values (%s, %s) returning id""",
        (company_name.strip(), key),
    )
    return cur.fetchone()[0]
