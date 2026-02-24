from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class PapertrailConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "PAPERTRAIL_DATA_DIR", str(Path.home() / ".papertrail")
    )))
    index_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "PAPERTRAIL_INDEX_DIR", str(Path.home() / ".cache" / "papertrail")
    )))
    rclone_remote: str = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_RCLONE_REMOTE", ""
    ))
    semantic_scholar_api_key: str | None = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_SEMANTIC_SCHOLAR_API_KEY"
    ))
    http_proxy: str | None = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_HTTP_PROXY"
    ))
    unpaywall_email: str | None = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_UNPAYWALL_EMAIL"
    ))

    @classmethod
    def from_env(cls) -> "PapertrailConfig":
        return cls()

    @property
    def index_db_path(self) -> Path:
        return self.index_dir / "index.db"

    @property
    def papers_dir(self) -> Path:
        return self.data_dir / "papers"

    @property
    def tags_path(self) -> Path:
        return self.data_dir / "tags.json"

    def ensure_directories(self) -> None:
        self.papers_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
