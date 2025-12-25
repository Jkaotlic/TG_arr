# Feature: qBittorrent Integration

## Обзор

Добавление полноценной интеграции с qBittorrent для мониторинга и управления загрузками прямо из Telegram.

## Цели

1. **Мониторинг**: Статус загрузок, скорость, прогресс
2. **Управление**: Пауза/возобновление, удаление, приоритеты
3. **Уведомления**: Оповещения о завершении загрузок
4. **Связь с *arr**: Показывать какой контент загружается

---

## Новые команды

| Команда | Описание |
|---------|----------|
| `/downloads` или `/dl` | Список активных загрузок |
| `/torrents` | Полный список торрентов (с фильтрами) |
| `/qstatus` | Статус qBittorrent (скорости, диск, очередь) |
| `/pause <id\|all>` | Поставить на паузу |
| `/resume <id\|all>` | Возобновить загрузку |
| `/delete <id>` | Удалить торрент (с опцией удаления файлов) |
| `/speed <down> <up>` | Установить лимиты скорости |

---

## Архитектура

### Новые компоненты

```
bot/
├── clients/
│   └── qbittorrent.py      # NEW: qBittorrent API client
├── services/
│   └── download_service.py  # NEW: Логика управления загрузками
├── handlers/
│   └── downloads.py         # NEW: Обработчики команд
└── ui/
    └── download_keyboards.py # NEW: Клавиатуры для торрентов
```

### Модели данных

```python
# bot/models.py - добавить

class TorrentState(str, Enum):
    """Состояние торрента в qBittorrent."""
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    QUEUED = "queued"
    STALLED = "stalled"
    CHECKING = "checking"
    ERROR = "error"
    COMPLETED = "completed"
    MOVING = "moving"
    UNKNOWN = "unknown"


class TorrentInfo(BaseModel):
    """Информация о торренте."""
    hash: str
    name: str
    size: int
    progress: float  # 0.0 - 1.0
    download_speed: int  # bytes/s
    upload_speed: int  # bytes/s
    eta: Optional[int]  # seconds, -1 if unknown
    state: TorrentState
    category: Optional[str]
    tags: list[str] = []
    added_on: datetime
    completion_on: Optional[datetime]
    save_path: str

    # Peer info
    seeds: int
    seeds_total: int
    peers: int
    peers_total: int

    # Ratio
    ratio: float
    uploaded: int
    downloaded: int

    # Tracker
    tracker: Optional[str]

    @property
    def progress_percent(self) -> int:
        return int(self.progress * 100)

    @property
    def eta_formatted(self) -> str:
        if self.eta is None or self.eta < 0:
            return "∞"
        hours, remainder = divmod(self.eta, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"


class QBittorrentStatus(BaseModel):
    """Общий статус qBittorrent."""
    version: str
    connection_status: str  # connected, firewalled, disconnected

    # Transfer info
    download_speed: int
    upload_speed: int
    download_limit: int  # 0 = unlimited
    upload_limit: int

    # Session stats
    total_downloaded: int
    total_uploaded: int

    # Disk
    free_space: int

    # Queue
    active_downloads: int
    active_uploads: int
    total_torrents: int
    paused_torrents: int

    # DHT
    dht_nodes: int


class TorrentFilter(str, Enum):
    """Фильтры для списка торрентов."""
    ALL = "all"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    COMPLETED = "completed"
    PAUSED = "paused"
    ACTIVE = "active"
    INACTIVE = "inactive"
    STALLED = "stalled"
    ERRORED = "errored"
```

---

## qBittorrent API Client

```python
# bot/clients/qbittorrent.py

"""qBittorrent Web API client."""

from typing import Any, Optional
from datetime import datetime

import structlog
from bot.clients.base import BaseAPIClient, APIError
from bot.models import TorrentInfo, TorrentState, QBittorrentStatus, TorrentFilter

logger = structlog.get_logger()

# State mapping from qBittorrent API
STATE_MAP = {
    "allocating": TorrentState.CHECKING,
    "checkingDL": TorrentState.CHECKING,
    "checkingResumeData": TorrentState.CHECKING,
    "checkingUP": TorrentState.CHECKING,
    "downloading": TorrentState.DOWNLOADING,
    "error": TorrentState.ERROR,
    "forcedDL": TorrentState.DOWNLOADING,
    "forcedMetaDL": TorrentState.DOWNLOADING,
    "forcedUP": TorrentState.SEEDING,
    "metaDL": TorrentState.DOWNLOADING,
    "missingFiles": TorrentState.ERROR,
    "moving": TorrentState.MOVING,
    "pausedDL": TorrentState.PAUSED,
    "pausedUP": TorrentState.PAUSED,
    "queuedDL": TorrentState.QUEUED,
    "queuedUP": TorrentState.QUEUED,
    "stalledDL": TorrentState.STALLED,
    "stalledUP": TorrentState.SEEDING,
    "uploading": TorrentState.SEEDING,
}


class QBittorrentClient(BaseAPIClient):
    """Client for qBittorrent Web API."""

    def __init__(self, base_url: str, username: str, password: str):
        # qBittorrent uses session cookies, not API key
        super().__init__(base_url, "", "qBittorrent")
        self.username = username
        self.password = password
        self._sid: Optional[str] = None

    def _get_headers(self) -> dict[str, str]:
        """Get headers with session cookie."""
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.base_url,
        }
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        return headers

    async def login(self) -> bool:
        """Authenticate with qBittorrent."""
        try:
            response = await self._request(
                "POST",
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
            # SID is set via Set-Cookie header, handled by httpx
            self._sid = response.cookies.get("SID")
            return self._sid is not None
        except Exception as e:
            logger.error("qBittorrent login failed", error=str(e))
            return False

    async def ensure_logged_in(self) -> None:
        """Ensure we have a valid session."""
        if not self._sid:
            if not await self.login():
                raise APIError("Failed to authenticate with qBittorrent")

    async def get_status(self) -> QBittorrentStatus:
        """Get qBittorrent global status."""
        await self.ensure_logged_in()

        # Get transfer info
        transfer = await self.get("/api/v2/transfer/info")

        # Get main data for disk space
        maindata = await self.get("/api/v2/sync/maindata")
        server_state = maindata.get("server_state", {})

        # Get torrent counts
        torrents = await self.get_torrents()

        return QBittorrentStatus(
            version=await self.get_version(),
            connection_status=transfer.get("connection_status", "unknown"),
            download_speed=transfer.get("dl_info_speed", 0),
            upload_speed=transfer.get("up_info_speed", 0),
            download_limit=transfer.get("dl_rate_limit", 0),
            upload_limit=transfer.get("up_rate_limit", 0),
            total_downloaded=transfer.get("dl_info_data", 0),
            total_uploaded=transfer.get("up_info_data", 0),
            free_space=server_state.get("free_space_on_disk", 0),
            active_downloads=sum(1 for t in torrents if t.state == TorrentState.DOWNLOADING),
            active_uploads=sum(1 for t in torrents if t.state == TorrentState.SEEDING),
            total_torrents=len(torrents),
            paused_torrents=sum(1 for t in torrents if t.state == TorrentState.PAUSED),
            dht_nodes=server_state.get("dht_nodes", 0),
        )

    async def get_version(self) -> str:
        """Get qBittorrent version."""
        await self.ensure_logged_in()
        result = await self.get("/api/v2/app/version")
        return str(result) if result else "unknown"

    async def get_torrents(
        self,
        filter_type: TorrentFilter = TorrentFilter.ALL,
        category: Optional[str] = None,
        sort: str = "added_on",
        reverse: bool = True,
        limit: Optional[int] = None,
    ) -> list[TorrentInfo]:
        """Get list of torrents."""
        await self.ensure_logged_in()

        params = {
            "filter": filter_type.value,
            "sort": sort,
            "reverse": str(reverse).lower(),
        }
        if category:
            params["category"] = category
        if limit:
            params["limit"] = limit

        result = await self.get("/api/v2/torrents/info", params=params)

        torrents = []
        for item in result:
            torrents.append(self._parse_torrent(item))

        return torrents

    async def get_torrent(self, hash: str) -> Optional[TorrentInfo]:
        """Get single torrent by hash."""
        await self.ensure_logged_in()

        params = {"hashes": hash}
        result = await self.get("/api/v2/torrents/info", params=params)

        if result and len(result) > 0:
            return self._parse_torrent(result[0])
        return None

    async def pause(self, hashes: list[str] | str = "all") -> None:
        """Pause torrent(s)."""
        await self.ensure_logged_in()

        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        await self.post("/api/v2/torrents/pause", data={"hashes": hashes})

    async def resume(self, hashes: list[str] | str = "all") -> None:
        """Resume torrent(s)."""
        await self.ensure_logged_in()

        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        await self.post("/api/v2/torrents/resume", data={"hashes": hashes})

    async def delete(self, hashes: list[str] | str, delete_files: bool = False) -> None:
        """Delete torrent(s)."""
        await self.ensure_logged_in()

        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        await self.post("/api/v2/torrents/delete", data={
            "hashes": hashes,
            "deleteFiles": str(delete_files).lower(),
        })

    async def set_speed_limit(
        self,
        download_limit: Optional[int] = None,
        upload_limit: Optional[int] = None,
    ) -> None:
        """Set global speed limits (0 = unlimited)."""
        await self.ensure_logged_in()

        if download_limit is not None:
            await self.post("/api/v2/transfer/setDownloadLimit",
                          data={"limit": download_limit})

        if upload_limit is not None:
            await self.post("/api/v2/transfer/setUploadLimit",
                          data={"limit": upload_limit})

    async def set_torrent_priority(self, hashes: list[str], priority: str) -> None:
        """Set torrent priority: max, min, increase, decrease."""
        await self.ensure_logged_in()

        endpoint = f"/api/v2/torrents/top{priority.capitalize()}"
        if priority in ("increase", "decrease"):
            endpoint = f"/api/v2/torrents/{priority}Prio"

        await self.post(endpoint, data={"hashes": "|".join(hashes)})

    async def recheck(self, hashes: list[str] | str) -> None:
        """Force recheck torrent(s)."""
        await self.ensure_logged_in()

        if isinstance(hashes, list):
            hashes = "|".join(hashes)

        await self.post("/api/v2/torrents/recheck", data={"hashes": hashes})

    async def get_torrent_files(self, hash: str) -> list[dict]:
        """Get files in a torrent."""
        await self.ensure_logged_in()
        result = await self.get("/api/v2/torrents/files", params={"hash": hash})
        return result if isinstance(result, list) else []

    async def get_torrent_trackers(self, hash: str) -> list[dict]:
        """Get trackers for a torrent."""
        await self.ensure_logged_in()
        result = await self.get("/api/v2/torrents/trackers", params={"hash": hash})
        return result if isinstance(result, list) else []

    def _parse_torrent(self, item: dict) -> TorrentInfo:
        """Parse qBittorrent torrent response."""
        state_str = item.get("state", "unknown")
        state = STATE_MAP.get(state_str, TorrentState.UNKNOWN)

        # Parse timestamps
        added_on = datetime.fromtimestamp(item.get("added_on", 0))
        completion_on = None
        if item.get("completion_on", 0) > 0:
            completion_on = datetime.fromtimestamp(item["completion_on"])

        return TorrentInfo(
            hash=item.get("hash", ""),
            name=item.get("name", "Unknown"),
            size=item.get("total_size", 0),
            progress=item.get("progress", 0),
            download_speed=item.get("dlspeed", 0),
            upload_speed=item.get("upspeed", 0),
            eta=item.get("eta"),
            state=state,
            category=item.get("category"),
            tags=item.get("tags", "").split(",") if item.get("tags") else [],
            added_on=added_on,
            completion_on=completion_on,
            save_path=item.get("save_path", ""),
            seeds=item.get("num_seeds", 0),
            seeds_total=item.get("num_complete", 0),
            peers=item.get("num_leechs", 0),
            peers_total=item.get("num_incomplete", 0),
            ratio=item.get("ratio", 0),
            uploaded=item.get("uploaded", 0),
            downloaded=item.get("downloaded", 0),
            tracker=item.get("tracker"),
        )

    async def check_connection(self) -> tuple[bool, Optional[str], Optional[float]]:
        """Check if qBittorrent is available."""
        import time
        start = time.monotonic()
        try:
            await self.ensure_logged_in()
            version = await self.get_version()
            elapsed = (time.monotonic() - start) * 1000
            return True, version, round(elapsed, 2)
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("qBittorrent health check failed", error=str(e))
            return False, None, round(elapsed, 2)
```

---

## UI: Форматирование и клавиатуры

### Форматирование сообщений

```python
# bot/ui/formatters.py - добавить методы

@staticmethod
def format_torrent_info(torrent: TorrentInfo, compact: bool = False) -> str:
    """Format torrent information."""
    # Progress bar
    progress_bar = Formatters._make_progress_bar(torrent.progress, 10)

    # State emoji
    state_emoji = {
        TorrentState.DOWNLOADING: "⬇️",
        TorrentState.SEEDING: "⬆️",
        TorrentState.PAUSED: "⏸",
        TorrentState.QUEUED: "⏳",
        TorrentState.STALLED: "⚠️",
        TorrentState.CHECKING: "🔍",
        TorrentState.ERROR: "❌",
        TorrentState.COMPLETED: "✅",
        TorrentState.MOVING: "📦",
    }.get(torrent.state, "❓")

    if compact:
        return (
            f"{state_emoji} **{torrent.name[:40]}**\n"
            f"{progress_bar} {torrent.progress_percent}%"
        )

    lines = [
        f"{state_emoji} **{torrent.name}**",
        "",
        f"📊 Progress: {progress_bar} {torrent.progress_percent}%",
        f"💾 Size: {Formatters._format_size(torrent.size)}",
    ]

    if torrent.state == TorrentState.DOWNLOADING:
        lines.append(f"⬇️ Speed: {Formatters._format_speed(torrent.download_speed)}")
        lines.append(f"⏱ ETA: {torrent.eta_formatted}")
    elif torrent.state == TorrentState.SEEDING:
        lines.append(f"⬆️ Speed: {Formatters._format_speed(torrent.upload_speed)}")
        lines.append(f"📈 Ratio: {torrent.ratio:.2f}")

    lines.append(f"👥 Seeds: {torrent.seeds}/{torrent.seeds_total} | Peers: {torrent.peers}/{torrent.peers_total}")

    if torrent.category:
        lines.append(f"📁 Category: {torrent.category}")

    return "\n".join(lines)

@staticmethod
def format_qbittorrent_status(status: QBittorrentStatus) -> str:
    """Format qBittorrent status."""
    lines = [
        "**qBittorrent Status**",
        f"Version: {status.version}",
        f"Connection: {status.connection_status}",
        "",
        "**Transfer:**",
        f"⬇️ {Formatters._format_speed(status.download_speed)} | ⬆️ {Formatters._format_speed(status.upload_speed)}",
    ]

    if status.download_limit > 0 or status.upload_limit > 0:
        dl_limit = Formatters._format_speed(status.download_limit) if status.download_limit else "∞"
        ul_limit = Formatters._format_speed(status.upload_limit) if status.upload_limit else "∞"
        lines.append(f"Limits: ⬇️ {dl_limit} | ⬆️ {ul_limit}")

    lines.extend([
        "",
        "**Torrents:**",
        f"📥 Downloading: {status.active_downloads}",
        f"📤 Seeding: {status.active_uploads}",
        f"⏸ Paused: {status.paused_torrents}",
        f"📊 Total: {status.total_torrents}",
        "",
        f"💽 Free space: {Formatters._format_size(status.free_space)}",
        f"🌐 DHT nodes: {status.dht_nodes}",
    ])

    return "\n".join(lines)

@staticmethod
def format_torrent_list(torrents: list[TorrentInfo], page: int, total_pages: int) -> str:
    """Format paginated torrent list."""
    if not torrents:
        return "No torrents found."

    lines = [f"**Active Torrents** (Page {page + 1}/{total_pages})\n"]

    for i, t in enumerate(torrents, 1):
        state_emoji = {
            TorrentState.DOWNLOADING: "⬇️",
            TorrentState.SEEDING: "⬆️",
            TorrentState.PAUSED: "⏸",
            TorrentState.ERROR: "❌",
        }.get(t.state, "•")

        progress = f"{t.progress_percent}%"
        speed = ""
        if t.state == TorrentState.DOWNLOADING:
            speed = f" @ {Formatters._format_speed(t.download_speed)}"
        elif t.state == TorrentState.SEEDING:
            speed = f" ↑{Formatters._format_speed(t.upload_speed)}"

        name = t.name[:35] + "..." if len(t.name) > 38 else t.name
        lines.append(f"{i}. {state_emoji} {name}\n   {progress}{speed}")

    return "\n".join(lines)

@staticmethod
def _make_progress_bar(progress: float, length: int = 10) -> str:
    """Create a text progress bar."""
    filled = int(progress * length)
    empty = length - filled
    return "█" * filled + "░" * empty

@staticmethod
def _format_speed(bytes_per_sec: int) -> str:
    """Format speed in human-readable format."""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    elif bytes_per_sec < 1024 * 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    return f"{bytes_per_sec / (1024 * 1024 * 1024):.1f} GB/s"

@staticmethod
def _format_size(bytes: int) -> str:
    """Format size in human-readable format."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(bytes) < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} PB"
```

### Клавиатуры

```python
# bot/ui/keyboards.py - добавить

class DownloadCallbackData:
    """Callback data prefixes for download management."""
    TORRENT = "torrent:"        # torrent:<hash>
    PAUSE = "t_pause:"          # t_pause:<hash>
    RESUME = "t_resume:"        # t_resume:<hash>
    DELETE = "t_delete:"        # t_delete:<hash>
    DELETE_FILES = "t_delf:"    # t_delf:<hash>
    RECHECK = "t_recheck:"      # t_recheck:<hash>
    PRIORITY = "t_prio:"        # t_prio:<hash>:<priority>
    FILTER = "t_filter:"        # t_filter:<filter_type>
    REFRESH = "t_refresh"
    PAGE = "t_page:"            # t_page:<page_num>
    PAUSE_ALL = "t_pause_all"
    RESUME_ALL = "t_resume_all"
    SPEED_MENU = "t_speed"
    SPEED_SET = "t_speed_set:"  # t_speed_set:<dl>:<ul>


@staticmethod
def torrent_list(
    torrents: list[TorrentInfo],
    current_page: int,
    total_pages: int,
    current_filter: TorrentFilter = TorrentFilter.ALL,
) -> InlineKeyboardMarkup:
    """Create keyboard for torrent list."""
    keyboard = []

    # Torrent buttons (show hash[:8] for identification)
    for t in torrents:
        state_icon = "⏸" if t.state == TorrentState.PAUSED else "▶️"
        name = t.name[:30] + "..." if len(t.name) > 33 else t.name
        keyboard.append([
            InlineKeyboardButton(
                text=f"{state_icon} {name}",
                callback_data=f"{DownloadCallbackData.TORRENT}{t.hash[:8]}",
            )
        ])

    # Pagination
    nav_row = []
    if current_page > 0:
        nav_row.append(InlineKeyboardButton(
            text="◀️ Prev",
            callback_data=f"{DownloadCallbackData.PAGE}{current_page - 1}",
        ))
    nav_row.append(InlineKeyboardButton(
        text=f"{current_page + 1}/{total_pages}",
        callback_data="noop",
    ))
    if current_page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            text="Next ▶️",
            callback_data=f"{DownloadCallbackData.PAGE}{current_page + 1}",
        ))
    if nav_row:
        keyboard.append(nav_row)

    # Filter buttons
    keyboard.append([
        InlineKeyboardButton(
            text="🔄 Refresh",
            callback_data=DownloadCallbackData.REFRESH,
        ),
        InlineKeyboardButton(
            text="🔽 Filter",
            callback_data=f"{DownloadCallbackData.FILTER}menu",
        ),
    ])

    # Global actions
    keyboard.append([
        InlineKeyboardButton(text="⏸ Pause All", callback_data=DownloadCallbackData.PAUSE_ALL),
        InlineKeyboardButton(text="▶️ Resume All", callback_data=DownloadCallbackData.RESUME_ALL),
    ])

    keyboard.append([
        InlineKeyboardButton(text="⚡ Speed Limits", callback_data=DownloadCallbackData.SPEED_MENU),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@staticmethod
def torrent_details(torrent: TorrentInfo) -> InlineKeyboardMarkup:
    """Create keyboard for torrent details."""
    keyboard = []

    # Pause/Resume based on state
    if torrent.state == TorrentState.PAUSED:
        keyboard.append([
            InlineKeyboardButton(
                text="▶️ Resume",
                callback_data=f"{DownloadCallbackData.RESUME}{torrent.hash[:8]}",
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                text="⏸ Pause",
                callback_data=f"{DownloadCallbackData.PAUSE}{torrent.hash[:8]}",
            )
        ])

    # Priority
    keyboard.append([
        InlineKeyboardButton(
            text="⬆️ Max Priority",
            callback_data=f"{DownloadCallbackData.PRIORITY}{torrent.hash[:8]}:max",
        ),
        InlineKeyboardButton(
            text="⬇️ Min Priority",
            callback_data=f"{DownloadCallbackData.PRIORITY}{torrent.hash[:8]}:min",
        ),
    ])

    # Recheck
    keyboard.append([
        InlineKeyboardButton(
            text="🔍 Force Recheck",
            callback_data=f"{DownloadCallbackData.RECHECK}{torrent.hash[:8]}",
        ),
    ])

    # Delete
    keyboard.append([
        InlineKeyboardButton(
            text="🗑 Delete",
            callback_data=f"{DownloadCallbackData.DELETE}{torrent.hash[:8]}",
        ),
        InlineKeyboardButton(
            text="🗑💾 Delete + Files",
            callback_data=f"{DownloadCallbackData.DELETE_FILES}{torrent.hash[:8]}",
        ),
    ])

    # Back
    keyboard.append([
        InlineKeyboardButton(text="◀️ Back to List", callback_data=DownloadCallbackData.REFRESH),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@staticmethod
def torrent_filters() -> InlineKeyboardMarkup:
    """Create keyboard for filter selection."""
    filters = [
        ("All", TorrentFilter.ALL),
        ("Downloading", TorrentFilter.DOWNLOADING),
        ("Seeding", TorrentFilter.SEEDING),
        ("Completed", TorrentFilter.COMPLETED),
        ("Paused", TorrentFilter.PAUSED),
        ("Active", TorrentFilter.ACTIVE),
        ("Errored", TorrentFilter.ERRORED),
    ]

    keyboard = []
    row = []
    for name, f in filters:
        row.append(InlineKeyboardButton(
            text=name,
            callback_data=f"{DownloadCallbackData.FILTER}{f.value}",
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(text="◀️ Back", callback_data=DownloadCallbackData.REFRESH),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@staticmethod
def speed_limits_menu() -> InlineKeyboardMarkup:
    """Create keyboard for speed limit selection."""
    presets = [
        ("Unlimited", 0, 0),
        ("1 MB/s", 1024*1024, 512*1024),
        ("5 MB/s", 5*1024*1024, 2*1024*1024),
        ("10 MB/s", 10*1024*1024, 5*1024*1024),
        ("20 MB/s", 20*1024*1024, 10*1024*1024),
    ]

    keyboard = []
    for name, dl, ul in presets:
        keyboard.append([
            InlineKeyboardButton(
                text=f"⬇️{name}",
                callback_data=f"{DownloadCallbackData.SPEED_SET}{dl}:{ul}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(text="◀️ Back", callback_data=DownloadCallbackData.REFRESH),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
```

---

## Конфигурация

```python
# bot/config.py - добавить поля

class Settings(BaseSettings):
    # ... existing fields ...

    # qBittorrent
    qbittorrent_url: Optional[str] = Field(default=None, description="qBittorrent Web UI URL")
    qbittorrent_username: str = Field(default="admin", description="qBittorrent username")
    qbittorrent_password: Optional[str] = Field(default=None, description="qBittorrent password")

    # Notifications
    notify_download_complete: bool = Field(default=True, description="Notify when download completes")
    notify_check_interval: int = Field(default=60, description="Check interval for notifications (seconds)")

    @property
    def qbittorrent_enabled(self) -> bool:
        return self.qbittorrent_url is not None and self.qbittorrent_password is not None
```

```env
# .env.example - добавить

# qBittorrent (optional)
QBITTORRENT_URL=http://qbittorrent:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=your_password

# Notifications
NOTIFY_DOWNLOAD_COMPLETE=true
NOTIFY_CHECK_INTERVAL=60
```

---

## Уведомления о завершении

```python
# bot/services/notification_service.py

"""Background service for download notifications."""

import asyncio
from typing import Optional, Set

import structlog

from bot.clients.qbittorrent import QBittorrentClient
from bot.config import get_settings
from bot.db import Database
from bot.models import TorrentState

logger = structlog.get_logger()


class NotificationService:
    """Service for monitoring and notifying about completed downloads."""

    def __init__(self, bot, db: Database, qbt: QBittorrentClient):
        self.bot = bot
        self.db = db
        self.qbt = qbt
        self.settings = get_settings()
        self._running = False
        self._known_completed: Set[str] = set()

    async def start(self) -> None:
        """Start the notification background task."""
        if not self.settings.qbittorrent_enabled:
            return

        if not self.settings.notify_download_complete:
            return

        self._running = True
        logger.info("Starting notification service")

        # Initialize known completed torrents
        try:
            torrents = await self.qbt.get_torrents()
            for t in torrents:
                if t.progress >= 1.0:
                    self._known_completed.add(t.hash)
        except Exception as e:
            logger.warning("Failed to initialize completed torrents", error=str(e))

        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the notification service."""
        self._running = False

    async def _monitor_loop(self) -> None:
        """Background loop checking for completed downloads."""
        while self._running:
            try:
                await self._check_completed()
            except Exception as e:
                logger.error("Notification check failed", error=str(e))

            await asyncio.sleep(self.settings.notify_check_interval)

    async def _check_completed(self) -> None:
        """Check for newly completed downloads."""
        torrents = await self.qbt.get_torrents()

        for torrent in torrents:
            if torrent.progress >= 1.0 and torrent.hash not in self._known_completed:
                self._known_completed.add(torrent.hash)
                await self._notify_completion(torrent)

    async def _notify_completion(self, torrent) -> None:
        """Send notification about completed download."""
        # Get all users to notify (admins or specific users)
        # For simplicity, notify all allowed users
        settings = get_settings()

        message = (
            f"✅ **Download Complete!**\n\n"
            f"📦 {torrent.name}\n"
            f"💾 Size: {self._format_size(torrent.size)}\n"
            f"📁 Path: `{torrent.save_path}`"
        )

        for user_id in settings.allowed_tg_ids:
            try:
                await self.bot.send_message(user_id, message, parse_mode="Markdown")
            except Exception as e:
                logger.warning("Failed to send notification", user_id=user_id, error=str(e))

    @staticmethod
    def _format_size(bytes: int) -> str:
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if abs(bytes) < 1024.0:
                return f"{bytes:.1f} {unit}"
            bytes /= 1024.0
        return f"{bytes:.1f} PB"
```

---

## Интеграция с главным файлом

```python
# bot/main.py - добавить

from bot.clients.qbittorrent import QBittorrentClient
from bot.services.notification_service import NotificationService
from bot.handlers.downloads import router as downloads_router

# В setup_routers()
main_router.include_router(downloads_router)

# В main()
async def main():
    # ... existing setup ...

    # Initialize qBittorrent if configured
    qbt_client = None
    notification_service = None

    if settings.qbittorrent_enabled:
        qbt_client = QBittorrentClient(
            settings.qbittorrent_url,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
        )
        notification_service = NotificationService(bot, db, qbt_client)

    # Start notification service
    if notification_service:
        await notification_service.start()

    # ... existing polling ...

    # Cleanup
    if notification_service:
        await notification_service.stop()
    if qbt_client:
        await qbt_client.close()
```

---

## Обновление /status

```python
# bot/handlers/status.py - обновить

@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status command - check all services status."""
    settings = get_settings()

    # ... existing clients ...

    # Add qBittorrent if configured
    if settings.qbittorrent_enabled:
        qbt = QBittorrentClient(
            settings.qbittorrent_url,
            settings.qbittorrent_username,
            settings.qbittorrent_password,
        )
        # Add to parallel check
```

---

## План внедрения

### Фаза 1: Базовая интеграция ✅ COMPLETED
1. ✅ Добавить модели данных в `models.py`
2. ✅ Создать `QBittorrentClient` в `clients/qbittorrent.py`
3. ✅ Добавить конфигурацию
4. ✅ Добавить в `/status`

### Фаза 2: Просмотр и управление ✅ COMPLETED
1. ✅ Создать UI компоненты (клавиатуры, форматтеры)
2. ✅ Создать handler `downloads.py`
3. ✅ Реализовать команды `/downloads`, `/qstatus`
4. ✅ Реализовать pause/resume/delete

### Фаза 3: Продвинутые функции ✅ COMPLETED
1. ✅ Фильтры и пагинация
2. ✅ Управление скоростью
3. ✅ Детали торрента (файлы, трекеры)
4. ✅ Приоритеты

### Фаза 4: Уведомления ✅ COMPLETED
1. ✅ Background service
2. ✅ Уведомления о завершении
3. ✅ Связь с категориями Radarr/Sonarr

### Фаза 5: Тестирование ✅ COMPLETED
1. ✅ Unit тесты для клиента
2. ✅ Unit тесты для моделей и форматтеров
3. ✅ Unit тесты для сервиса уведомлений

---

## Зависимости

Никаких новых зависимостей не требуется - используем существующий `httpx`.

---

## Безопасность

1. **Пароль**: Хранится только в `.env`, не логируется
2. **Сессия**: SID cookie хранится в памяти, не персистируется
3. **Доступ**: Только авторизованные пользователи (существующий middleware)
4. **Удаление**: Требует подтверждения через отдельную кнопку

---

## Альтернативные клиенты

Архитектура позволяет легко добавить поддержку других клиентов:

- **Transmission** - похожий REST API
- **Deluge** - JSON-RPC API
- **rTorrent** - XML-RPC через ruTorrent

Для этого нужно создать аналогичный клиент, реализующий те же методы.
