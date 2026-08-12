"""Каждый символ в bot/ должен кем-то вызываться — и не только тестом.

Три находки финального ревью ветки feat/arr-restore были кодом, на который
ссылались только тесты. Ни ruff, ни зелёный набор такого не видят: тест
*является* ссылкой.

Наивный обход имён (собрать определения, собрать ast.Name/ast.Attribute) это не
ловит — `ProwlarrClient.search` для него невидим, потому что имя `search`
определено ещё у трёх классов. Нужны квалифицированные имена, и mypy их даёт:
`--cache-fine-grained` кладёт рядом с обычным кэшем файлы `*.deps.json` — карту
«триггер → цели, которые от него зависят», с полными именами вида
`<bot.clients.prowlarr.ProwlarrClient.search>`. Символ, чей триггер не
встречается ключом ни в одном deps.json, внутри bot/ не используется.

Два класса ложных срабатываний отфильтрованы здесь же:

* **переопределения** — `check_connection`, `_get_headers` и подобные
  вызываются полиморфно через базовый класс, у триггера подкласса зависимых
  нет;
* **вызовы через `Any`** — у mypy нет ребра, если объект пришёл
  нетипизированным параметром. Это лечится аннотацией места вызова, а НЕ
  записью в allowlist: аннотация заодно возвращает mypy контроль над этим
  вызовом.

Классы проверяются наивным сканом имён: в deps.json они ненадёжны — класс
попадает туда ключом из-за собственного тела.

Формат `*.deps.json` — внутренний для mypy, поэтому версия пиньована
(`mypy>=1.18,<2`), а нераспознанный кэш роняет тест, а не пропускает его:
молча переставший защищать тест хуже отсутствующего.
"""

import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "bot"

#: Полное имя → причина, по которой символ намеренно не вызывается из bot/.
#: Запись без причины не принимается (см. test_allowlist_entries_have_reasons).
ORPHAN_ALLOWLIST: dict[str, str] = {
    "bot.services.add_service.AddService.ensure_title": (
        "Намеренная NotImplementedError-заглушка отката 2026-08-10: композитный "
        "поток прежнего бэкенда, у которого нет эквивалента в *arr. Падает громко "
        "и называет задачу, владеющую заменой — это лучше и молчаливой догадки, и "
        "голого AttributeError."
    ),
    "bot.services.add_service.AddService.add_and_queue_best": (
        "Та же заглушка отката: «добавить + дать профилю выбрать + поставить в "
        "очередь» одним действием. Ближайший эквивалент — add_movie/add_series с "
        "search_for_movie=True, но форму для пользователя никто не специфицировал."
    ),
    "bot.services.add_service.AddService.grab_with_fallback": (
        "Та же заглушка отката: многокандидатный retry-цикл прежнего бэкенда. "
        "grab_release's native/push split закрывает тот один релиз, который выбрал "
        "пользователь."
    ),
    "bot.services.search_service._reset_module_state": (
        "Хелпер для тестов, вызывается autouse-фикстурой в tests/conftest.py. "
        "_DETECTION_CACHE и _CIRCUIT_BREAKER — процессные глобалы, без сброса "
        "между тестами набор становится порядкозависимым."
    ),
    "bot.clients.radarr.RadarrClient.delete_movie": (
        "Вызывается из bot/handlers/titles.py:63 через утиный параметр `client` — "
        "он приходит как `get_radarr() if is_movie else get_sonarr()`, то есть "
        "union без общего базового метода. Аннотировать его нельзя, не сломав "
        "мокабельность: tests/test_title_management.py передаёт туда AsyncMock, а "
        "isinstance-сужение сделало бы моки непригодными. Осознанный утиный шов."
    ),
    "bot.clients.sonarr.SonarrClient.delete_series": (
        "Вторая половина того же утиного шва — bot/handlers/titles.py:64. "
        "См. причину у RadarrClient.delete_movie."
    ),
}

#: Декораторы, отдающие функцию фреймворку: её вызывает aiogram или pydantic, а
#: не наш код, поэтому ссылки внутри bot/ на неё быть не должно.
_ROOT_DECORATORS = frozenset({
    "message", "callback_query", "edited_message", "inline_query",
    "error", "startup", "shutdown",
    "field_validator", "model_validator", "computed_field",
})

#: Точки входа процесса.
_ENTRY_POINTS = frozenset({"bot.main.main"})


class _Definition(NamedTuple):
    fullname: str
    file: str
    line: int
    kind: str                 # "func" | "class"
    name: str
    owner: Optional[str]      # простое имя класса-владельца, если это метод
    is_root: bool


def _module_name(path: Path) -> str:
    rel = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        while isinstance(target, ast.Attribute):
            names.add(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _walk_definitions() -> tuple[list[_Definition], dict[str, list[str]], dict[str, set[str]]]:
    """Определения bot/, плюс карты «класс → базы» и «класс → его методы»."""
    definitions: list[_Definition] = []
    class_bases: dict[str, list[str]] = {}
    class_methods: dict[str, set[str]] = {}

    for path in sorted(BOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        rel = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")

        def visit(node: ast.AST, prefix: str, owner: Optional[str]) -> None:
            for child in getattr(node, "body", []):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                is_class = isinstance(child, ast.ClassDef)
                fullname = f"{prefix}.{child.name}"
                definitions.append(_Definition(
                    fullname=fullname,
                    file=rel,
                    line=child.lineno,
                    kind="class" if is_class else "func",
                    name=child.name,
                    owner=owner,
                    is_root=bool(_decorator_names(child) & _ROOT_DECORATORS),
                ))
                if is_class:
                    class_bases[child.name] = _base_names(child)
                    class_methods.setdefault(child.name, set())
                    visit(child, fullname, child.name)
                elif owner is not None:
                    class_methods.setdefault(owner, set()).add(child.name)

        visit(tree, module, None)

    return definitions, class_bases, class_methods


def _is_override(
    definition: _Definition,
    class_bases: dict[str, list[str]],
    class_methods: dict[str, set[str]],
) -> bool:
    """Определён ли тот же метод у любого предка внутри bot/."""
    if definition.owner is None:
        return False
    seen: set[str] = set()
    queue = list(class_bases.get(definition.owner, []))
    while queue:
        base = queue.pop()
        if base in seen:
            continue
        seen.add(base)
        if definition.name in class_methods.get(base, set()):
            return True
        queue.extend(class_bases.get(base, []))
    return False


def _referenced_names() -> Counter:
    """Сколько раз каждое простое имя упоминается внутри bot/."""
    used: Counter = Counter()
    for path in sorted(BOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used[node.id] += 1
            elif isinstance(node, ast.Attribute):
                used[node.attr] += 1
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    used[alias.name] += 1
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                used[node.value] += 1
    return used


def _fine_grained_triggers(cache_dir: Path) -> set[str]:
    """Ключи всех *.deps.json — символы, от которых хоть что-то зависит."""
    result = subprocess.run(
        [
            sys.executable, "-m", "mypy", "bot/",
            "--cache-fine-grained", f"--cache-dir={cache_dir}",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    deps_files = list(cache_dir.rglob("*.deps.json"))
    if not deps_files:
        pytest.fail(
            "mypy не отдал fine-grained кэш — проверка на недостижимый код "
            "перестала работать. Пропустить её молча нельзя.\n"
            f"exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    triggers: set[str] = set()
    for path in deps_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            pytest.fail(f"Кэш mypy не разобрался ({path}): {e}")
        if not isinstance(data, dict):
            pytest.fail(
                f"Неожиданная форма {path}: ожидался объект, получен {type(data).__name__}"
            )
        triggers.update(data)
    return triggers


def _find_orphans(triggers: set[str]) -> dict[str, str]:
    """Полное имя → «файл:строка» для каждого неиспользуемого символа."""
    definitions, class_bases, class_methods = _walk_definitions()
    used = _referenced_names()

    orphans: dict[str, str] = {}
    for definition in definitions:
        if definition.is_root or definition.name.startswith("__"):
            continue
        if definition.fullname in _ENTRY_POINTS:
            continue

        if definition.kind == "class":
            # Классы в deps.json ненадёжны — они попадают туда ключом из-за
            # собственного тела. Для них наивный скан имён точнее.
            if used[definition.name] == 0:
                orphans[definition.fullname] = f"{definition.file}:{definition.line}"
            continue

        if _is_override(definition, class_bases, class_methods):
            continue
        if f"<{definition.fullname}>" not in triggers:
            orphans[definition.fullname] = f"{definition.file}:{definition.line}"

    return orphans


@pytest.fixture(scope="module")
def orphans(tmp_path_factory) -> dict[str, str]:
    cache_dir = tmp_path_factory.mktemp("mypy-fine-grained")
    return _find_orphans(_fine_grained_triggers(cache_dir))


def test_no_symbol_is_referenced_only_by_its_own_tests(orphans):
    unexpected = {name: where for name, where in orphans.items() if name not in ORPHAN_ALLOWLIST}
    listing = "\n".join(f"  {where:45s} {name}" for name, where in sorted(unexpected.items()))
    assert not unexpected, (
        "Ничто внутри bot/ не обращается к этим символам — значит, их держат "
        "только тесты (либо вызов идёт через нетипизированный параметр, и "
        "правильный ответ — аннотировать место вызова, а не занести сюда):\n"
        f"{listing}\n\n"
        "Удалить вместе с тестами, которые их только и держали, либо внести в "
        "ORPHAN_ALLOWLIST с причиной."
    )


def test_allowlist_has_no_stale_entries(orphans):
    stale = sorted(set(ORPHAN_ALLOWLIST) - set(orphans))
    assert not stale, (
        "Эти символы снова используются — уберите их из ORPHAN_ALLOWLIST, "
        f"иначе он перестанет быть списком известных исключений: {stale}"
    )


def test_allowlist_entries_have_reasons():
    empty = sorted(name for name, reason in ORPHAN_ALLOWLIST.items() if not (reason or "").strip())
    assert not empty, f"Запись в ORPHAN_ALLOWLIST без причины: {empty}"


def test_the_check_can_actually_see_a_known_used_symbol(orphans):
    """Страховка от «зелено, потому что сломалось»: широко используемый метод
    не должен попасть в список сирот."""
    assert "bot.clients.sonarr.SonarrClient.add_series" not in orphans
