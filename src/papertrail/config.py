from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class PapertrailConfig:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get(
        "PAPERTRAIL_DATA_DIR", str(Path.home() / ".papertrail")
    )))
    rclone_remote: str = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_RCLONE_REMOTE", ""
    ))
    semantic_scholar_api_key: str | None = field(default_factory=lambda: os.environ.get(
        "PAPERTRAIL_SEMANTIC_SCHOLAR_API_KEY"
    ))

    @classmethod
    def from_env(cls) -> "PapertrailConfig":
        return cls()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "papers.db"

    @property
    def papers_dir(self) -> Path:
        return self.data_dir / "papers"

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.papers_dir.mkdir(parents=True, exist_ok=True)
