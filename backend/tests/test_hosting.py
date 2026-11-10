"""The always-on collector config stays paper-only and honest about slot windows."""
from pathlib import Path

from sqlalchemy.engine import make_url

from app.collector.schedule import schedule_can_hit, tolerance_seconds
from app.config import Settings
from app.db.session import split_database_url
from app.research.sync import _same_database

BACKEND = Path(__file__).resolve().parents[1]
FLY = (BACKEND / "fly.toml").read_text()
DOCKERFILE = (BACKEND / "Dockerfile").read_text()
RUNNER = (BACKEND / "bin" / "run-collector").read_text()


def test_poll_fits_the_one_minute_window_and_a_five_minute_cron_does_not():
    poll = Settings().collector_poll_s
    assert tolerance_seconds(1) == 45
    assert tolerance_seconds(5) == 90
    assert tolerance_seconds(15) == 180
    # 20s poll plus 20s of restart slack still lands inside the tightest window.
    assert schedule_can_hit(poll, 20, 1)
    assert schedule_can_hit(poll, 0, 5)
    # GitHub's shortest cron is 5 minutes, which is longer than every window.
    assert not schedule_can_hit(300, 0, 1)
    assert not schedule_can_hit(300, 0, 5)
    assert not schedule_can_hit(300, 0, 15)
    assert not schedule_can_hit(300, 0, 60)
    assert not schedule_can_hit(0, 0, 1)


def test_hosted_poll_stays_inside_provider_limits_and_local_defaults_stay():
    settings = Settings()
    assert settings.collector_poll_s == 20
    assert settings.collector_gt_interval_s >= 180
    assert settings.collector_max_new_per_day == 150
    assert settings.risk_max_position_usd == 50
    assert settings.risk_max_daily_loss_usd == 50
    pair_reads_per_minute = settings.collector_snapshots_per_cycle * (60 / settings.collector_poll_s)
    assert pair_reads_per_minute < settings.dexscreener_pairs_rpm
    assert "DEXBOT_COLLECTOR_POLL_S" in FLY and '"20"' in FLY
    assert "DEXBOT_COLLECTOR_GT_INTERVAL_S" in FLY and '"180"' in FLY
    assert 'DEXBOT_COLLECTOR_MAX_NEW_PER_DAY = "40"' in FLY
    assert "DEXBOT_RISK_" not in FLY
    assert "TRADING" not in FLY
    assert "postgresql" not in FLY.lower()
    assert "password" not in FLY.lower()
    assert "http_service" not in FLY
    assert "/app/bin/run-collector" in FLY


def test_images_do_not_embed_credentials_and_the_api_image_stays_the_default():
    assert "AS collector" in DOCKERFILE
    assert DOCKERFILE.strip().endswith('CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]')
    assert "postgresql://" not in DOCKERFILE
    assert "alembic upgrade head" in RUNNER
    assert "python -m app.collector" in RUNNER
    assert "localhost" in RUNNER
    assert "postgresql://" not in RUNNER


def test_hosted_url_requests_ssl_without_passing_sslmode_to_asyncpg():
    url, args = split_database_url(
        "postgresql://postgres.abc:s3cret%40x@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
        "?sslmode=require&channel_binding=require"
    )
    parsed = make_url(url)
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.password == "s3cret@x"
    assert parsed.host == "aws-0-us-east-1.pooler.supabase.com"
    assert "sslmode" not in parsed.query
    assert "channel_binding" not in parsed.query
    assert args["ssl"] is True
    assert args["timeout"] == 5
    assert args["command_timeout"] == 10

    local, local_args = split_database_url("postgresql+asyncpg://dexbot:dexbot@127.0.0.1:5432/dexbot")
    assert make_url(local).database == "dexbot"
    assert "ssl" not in local_args
    _, disabled = split_database_url("postgresql://u:p@localhost/db?sslmode=disable")
    assert "ssl" not in disabled


def test_sync_refuses_to_copy_a_database_onto_itself():
    hosted = "postgresql://postgres.abc:s3cret@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
    assert not _same_database(hosted, "postgresql+asyncpg://dexbot:dexbot@127.0.0.1:5432/dexbot")
    assert _same_database(hosted, hosted.replace("postgresql://", "postgresql+asyncpg://"))
