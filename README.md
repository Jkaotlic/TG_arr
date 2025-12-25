# TG_arr - Telegram Bot for Prowlarr/Radarr/Sonarr

A production-ready Telegram bot that provides a mobile-friendly interface for managing your media server stack (Prowlarr, Radarr, Sonarr) directly from Telegram.

## Features

- **Smart Search**: Auto-detects if query is for a movie or series
- **Release Browser**: View and compare releases with quality info, size, seeders
- **Quality Scoring**: Intelligent scoring system to rank releases
- **One-Click Grab**: Download releases directly to your media server
- **User Settings**: Persistent preferences for quality profiles and folders
- **Access Control**: Whitelist-based authorization with admin roles
- **Full Async**: Built on aiogram 3.x with httpx for high performance

## Architecture

```
bot/
├── main.py              # Entry point
├── config.py            # Pydantic settings
├── db.py                # SQLite database layer
├── models.py            # Data models
├── services/
│   ├── search_service.py    # Search orchestration
│   ├── add_service.py       # Content addition logic
│   └── scoring.py           # Release scoring algorithm
├── clients/
│   ├── base.py              # Base HTTP client with retries
│   ├── prowlarr.py          # Prowlarr API client
│   ├── radarr.py            # Radarr API client
│   └── sonarr.py            # Sonarr API client
├── handlers/
│   ├── start.py             # /start, /help, /cancel
│   ├── search.py            # Search and grab handlers
│   ├── settings.py          # User preferences
│   ├── status.py            # Service health check
│   └── history.py           # Action history
├── ui/
│   ├── keyboards.py         # Inline keyboard builders
│   └── formatters.py        # Message formatters
└── middleware/
    └── auth.py              # Authorization middleware
```

## Quick Start

### 1. Prerequisites

- Docker and Docker Compose
- Prowlarr, Radarr, and Sonarr running and accessible
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))

### 2. Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
# Required
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_TG_IDS=123456789,987654321

# Prowlarr
PROWLARR_URL=http://prowlarr:9696
PROWLARR_API_KEY=your_prowlarr_api_key

# Radarr
RADARR_URL=http://radarr:7878
RADARR_API_KEY=your_radarr_api_key

# Sonarr
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=your_sonarr_api_key

# Optional
ADMIN_TG_IDS=123456789
TIMEZONE=Europe/Moscow
LOG_LEVEL=INFO
AUTO_GRAB_SCORE_THRESHOLD=80
```

### 3. Network Setup

If your *arr stack uses a Docker network, create it first:

```bash
docker network create arr-network
```

Or update `docker-compose.yml` to use your existing network name.

### 4. Deploy

```bash
docker compose up -d
```

Check logs:

```bash
docker compose logs -f tg-arr-bot
```

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot and see welcome message |
| `/help` | Show help and command list |
| `/search <query>` | Search for movies or series (auto-detect) |
| `/movie <query>` | Search specifically for movies |
| `/series <query>` | Search specifically for TV series |
| `/settings` | Configure your preferences |
| `/status` | Check Prowlarr/Radarr/Sonarr availability |
| `/history` | View your recent actions |
| `/cancel` | Cancel current operation |

You can also just send any text message as a search query.

## Search Examples

```
Dune 2021              # Movie with year
Breaking Bad S02       # Series, Season 2
The Office 1080p       # With quality preference
Пацаны 3 сезон         # Russian language supported
Andor S01E05           # Specific episode
```

## API Endpoints Used

### Prowlarr
- `GET /api/v1/search` - Search across all indexers
- `GET /api/v1/indexer` - List configured indexers
- `GET /api/v1/system/status` - Health check

### Radarr
- `GET /api/v3/movie/lookup` - Search movies
- `GET /api/v3/movie/lookup/tmdb` - Lookup by TMDB ID
- `GET /api/v3/movie` - Get movies in library
- `POST /api/v3/movie` - Add movie
- `POST /api/v3/release/push` - Push release for download
- `POST /api/v3/release` - Grab specific release
- `POST /api/v3/command` - Trigger movie search
- `GET /api/v3/qualityprofile` - List quality profiles
- `GET /api/v3/rootfolder` - List root folders

### Sonarr
- `GET /api/v3/series/lookup` - Search series
- `GET /api/v3/series` - Get series in library
- `POST /api/v3/series` - Add series
- `POST /api/v3/release/push` - Push release for download
- `POST /api/v3/release` - Grab specific release
- `POST /api/v3/command` - Trigger series/season search
- `GET /api/v3/qualityprofile` - List quality profiles
- `GET /api/v3/rootfolder` - List root folders

## Scoring System

Releases are scored based on multiple factors:

### Positive Factors
| Factor | Bonus |
|--------|-------|
| 2160p resolution | +25 |
| 1080p resolution | +20 |
| REMUX | +30 |
| BluRay source | +20 |
| WEB-DL source | +15 |
| x265/HEVC codec | +10 |
| Dolby Vision | +15 |
| HDR10+ | +12 |
| Atmos audio | +10 |
| REPACK/PROPER | +5 |
| Seeders (per 10, capped) | +2 |

### Negative Factors
| Factor | Penalty |
|--------|---------|
| CAM source | -50 |
| TS/Telesync | -40 |
| "sample" in title | -100 |
| "trailer" in title | -100 |
| Too small file size | -20 |
| Too large file size | -10 |

## User Preferences

Each user can configure:
- **Radarr Quality Profile**: Default profile for movies
- **Radarr Root Folder**: Where to store movies
- **Sonarr Quality Profile**: Default profile for series
- **Sonarr Root Folder**: Where to store series
- **Preferred Resolution**: Filter results (Any/720p/1080p/2160p)
- **Auto-Grab**: Enable "Grab Best" button for high-scored releases

## Troubleshooting

### Bot doesn't respond
1. Check if your Telegram ID is in `ALLOWED_TG_IDS`
2. Verify the bot token is correct
3. Check container logs: `docker compose logs tg-arr-bot`

### "Cannot connect to Prowlarr/Radarr/Sonarr"
1. Ensure services are running
2. Verify URLs are accessible from the bot container
3. Check API keys are correct
4. Use `/status` command to diagnose

### "No releases found"
1. Check if Prowlarr has working indexers
2. Try a more specific search query
3. Verify Prowlarr can search (test in Prowlarr UI)

### "Failed to add movie/series"
1. Check quality profiles exist in Radarr/Sonarr
2. Verify root folders are configured
3. Check Radarr/Sonarr logs for errors

### Database issues
The SQLite database is stored in the `data/` volume. To reset:

```bash
docker compose down
docker volume rm tg_arr_bot-data
docker compose up -d
```

## Development

### Local Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings

# Run bot
python -m bot.main
```

### Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# With coverage
pytest --cov=bot --cov-report=html
```

### Project Structure

- `bot/config.py` - All configuration via pydantic-settings
- `bot/clients/base.py` - Customize HTTP behavior (timeouts, retries)
- `bot/services/scoring.py` - Adjust scoring weights
- `bot/ui/keyboards.py` - Modify UI buttons
- `bot/ui/formatters.py` - Change message formats

## Security Notes

- Only users in `ALLOWED_TG_IDS` can use the bot
- API keys are never exposed in messages
- The bot doesn't store torrent files or media
- SQLite database contains only metadata and preferences

## License

MIT License - feel free to use and modify.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests
5. Submit a pull request

## Acknowledgments

- [aiogram](https://github.com/aiogram/aiogram) - Telegram Bot framework
- [Prowlarr](https://github.com/Prowlarr/Prowlarr) - Indexer manager
- [Radarr](https://github.com/Radarr/Radarr) - Movie collection manager
- [Sonarr](https://github.com/Sonarr/Sonarr) - TV series collection manager
