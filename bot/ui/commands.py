"""The single catalog of user-facing commands.

Telegram's "/" menu and the `/help` text used to be two hand-kept lists, so
they drifted: `/help` was missing `/anime`, `/album`, `/track`, `/title`,
`/wanted`, `/calendar` and `/health`, and still called `/music` "артисты
(Lidarr)" long after music moved to Lidarr+slskd. Both are rendered from here
now, so a new command shows up in both places or in neither.

Admin-only commands (`/users`, `/adduser`, `/deluser`) are deliberately absent:
advertising them to everyone only produces "недостаточно прав" replies.
"""

from aiogram.types import BotCommand

#: (heading, [(command, description)]) — order is the order both surfaces show.
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("🔍 Поиск", [
        ("search", "Найти фильм, сериал или аниме"),
        ("find", "Искать раздачи напрямую, мимо каталога"),
        ("movie", "Найти фильм"),
        ("series", "Найти сериал"),
        ("anime", "Найти аниме"),
        ("music", "Найти исполнителя"),
        ("album", "Найти альбом"),
        ("track", "Найти трек"),
    ]),
    ("📚 Библиотека", [
        ("title", "Тайтл в каталоге: мониторинг, удаление"),
        ("wanted", "Что ищется и не находится"),
        ("calendar", "Календарь выходов"),
        ("emby", "Что нового в библиотеке"),
        ("ts", "TorrServer — смотреть сейчас"),
    ]),
    ("📥 Загрузки", [
        ("downloads", "Текущие загрузки"),
        ("history", "История запросов"),
    ]),
    ("⚙️ Другое", [
        ("status", "Состояние сервисов"),
        ("health", "Диагностика проблем"),
        ("settings", "Настройки"),
        ("menu", "Главное меню"),
        ("help", "Справка"),
        ("cancel", "Отменить текущее действие"),
    ]),
]


def bot_commands() -> list[BotCommand]:
    """The catalog as Telegram's `setMyCommands` payload."""
    return [
        BotCommand(command=name, description=description)
        for _, entries in COMMAND_GROUPS
        for name, description in entries
    ]


def render_help() -> str:
    """The `/help` text, grouped the same way the catalog is."""
    blocks = []
    for heading, entries in COMMAND_GROUPS:
        lines = [f"<b>{heading}:</b>"]
        lines += [f"<code>/{name}</code> — {desc}" for name, desc in entries]
        blocks.append("\n".join(lines))

    return (
        "<b>🤖 TG_arr — Справка</b>\n\n"
        + "\n\n".join(blocks)
        + "\n\n💡 Просто напишите название — бот сам поймёт, что искать."
    )
