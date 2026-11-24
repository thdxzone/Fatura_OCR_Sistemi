"""Supabase (PostgreSQL + Storage) arka ucu.

FATURA_DATABASE_URL tanımlıysa uygulama SQLite/JSON yerine bunu kullanır:
  FATURA_DATABASE_URL        postgresql://fatura_backend.<tenant>:<parola>@<supavisor>:6543/postgres
  SUPABASE_URL               https://<supabase-adresi>
  SUPABASE_SERVICE_ROLE_KEY  yalnızca Storage API için; sunucuda kalır
  FATURA_STORAGE_BUCKET      varsayılan: fatura-belgeler
Sırlar yalnızca ortam değişkeninden okunur.
"""

import os


def open_backend(data_root, database_url):
    """Bağlantı havuzunu açar; (store, services) döner. psycopg yalnızca bu modda gerekir."""
    from psycopg_pool import ConnectionPool

    from fatura.supabase.files import SupabaseFiles
    from fatura.supabase.services import PgServices
    from fatura.supabase.store import PgStore

    files = SupabaseFiles(
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
        os.environ.get("FATURA_STORAGE_BUCKET", "fatura-belgeler"),
    )
    # Supavisor transaction modunda hazırlanmış ifade (prepared statement) kullanılamaz.
    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=int(os.environ.get("FATURA_DB_POOL_SIZE", "4")),
        kwargs={"prepare_threshold": None, "application_name": "fatura"},
        open=True,
    )
    pool.wait(timeout=30)
    return PgStore(data_root, pool, files), PgServices(pool)
