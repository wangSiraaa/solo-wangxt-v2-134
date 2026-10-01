import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import get_settings


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


def _fk(target: str) -> mapped_column:
    return mapped_column(ForeignKey(target, ondelete="CASCADE"))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    api_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Image(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    width: Mapped[int]
    height: Mapped[int]
    channels: Mapped[int]
    dtype: Mapped[str] = mapped_column(String(16))
    # Immutability: the object is content-addressed, never overwritten in place
    data_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    object_key: Mapped[str] = mapped_column(String(512))
    uploaded_by: Mapped[int] = mapped_column(Integer)
    synthetic_ground_truth: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    grants: Mapped[list["ImageGrant"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )


class ImageGrant(Base):
    __tablename__ = "image_grants"
    __table_args__ = (UniqueConstraint("image_id", "user_id", name="uq_grant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = _fk("images.id")
    user_id: Mapped[int] = _fk("users.id")
    can_view: Mapped[bool] = mapped_column(Boolean, default=False)
    can_publish: Mapped[bool] = mapped_column(Boolean, default=False)

    image: Mapped[Image] = relationship(back_populates="grants")


class ControlSample(Base):
    """A single-fluorophore control observation feeding a mixing matrix."""

    __tablename__ = "control_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    fluorophore: Mapped[str] = mapped_column(String(64))
    spectrum: Mapped[list] = mapped_column(JSON)  # per-channel relative response
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatrixVersion(Base):
    """
    A bleeding-matrix version. Rows are detection channels, columns are
    fluorophores (components). Content is immutable per version; a revision
    creates a new row with a new digest — old versions are never overwritten.
    """

    __tablename__ = "matrix_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    matrix: Mapped[list] = mapped_column(JSON)  # shape (C, K)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_matrix_name_version"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_id: Mapped[int] = _fk("images.id")
    matrix_version_id: Mapped[int] = _fk("matrix_versions.id")
    algorithm: Mapped[str] = mapped_column(String(32), default="nnls")
    algorithm_params: Mapped[dict] = mapped_column(JSON, default=dict)

    # Digests are pinned at job START time; the pipeline reads immutable
    # objects by digest, so a matrix swap mid-compute cannot leak into tiles.
    image_digest: Mapped[str] = mapped_column(String(64))
    matrix_digest: Mapped[str] = mapped_column(String(64))

    # Frozen whole-image parameters (stats estimated once, before fan-out).
    # Tiles verify params_hash; per-tile re-estimation is impossible.
    frozen_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    params_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    successor_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    generations: Mapped[list["Generation"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class Generation(Base):
    """
    One dispatch wave for a job. Every matrix revision or retry starts a new
    generation (for retries a new generation on the SAME job). Late tiles of
    abandoned generations are explicitly marked and never count toward
    completion or release.
    """

    __tablename__ = "generations"
    __table_args__ = (UniqueConstraint("job_id", "gen_no", name="uq_generation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = _fk("jobs.id")
    gen_no: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="dispatching", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Whole-generation quality gates, computed by the finalize stage
    recovery_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconstruction_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="generations")
    tiles: Mapped[list["TileRecord"]] = relationship(
        back_populates="generation", cascade="all, delete-orphan"
    )


class TileRecord(Base):
    __tablename__ = "tile_records"
    __table_args__ = (
        UniqueConstraint(
            "generation_id", "level", "tile_x", "tile_y", name="uq_generation_tile"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    generation_id: Mapped[int] = _fk("generations.id")
    level: Mapped[int] = mapped_column(Integer)
    tile_x: Mapped[int] = mapped_column(Integer)
    tile_y: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Object keys for the three coordinated views (None if not produced)
    raw_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    comp_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resid_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tile_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    generation: Mapped[Generation] = relationship(back_populates="tiles")


class ResultVersion(Base):
    """
    An immutable released report. A release points at one generation and all
    its tile objects; failed releases never overwrite previous reports — the
    'current' pointer only moves once the new report is fully written.
    Exactly one release per job (re-revisions create successor jobs).
    """

    __tablename__ = "result_versions"
    __table_args__ = (UniqueConstraint("job_id", name="uq_one_release_per_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = _fk("jobs.id")
    generation_id: Mapped[int] = _fk("generations.id")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    manifest: Mapped[dict] = mapped_column(JSON)
    recovery_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconstruction_rmse: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_object_key: Mapped[str] = mapped_column(String(512))
    report_digest: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16))  # allowed / denied / error
    resource: Mapped[str | None] = mapped_column(String(256), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(2048), nullable=True)


_settings = get_settings()
connect_args = {"check_same_thread": False, "timeout": 30} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if _settings.database_url.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def init_db(seed: bool = True) -> None:
    Base.metadata.create_all(engine)
    if seed:
        from .bootstrap import seed_data

        seed_data()
