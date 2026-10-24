"""Переход базы и настроек на 4.0 (описание 5.4, стандарт 6.6): копия → новые таблицы → сверка чисел.

При первом запуске 4.0 на старой базе:
  1. копия `anon.db` и `settings.json` → `backup-before-4.0/` в папке данных;
  2. `init_db` (схема 5: таблица `never_mask`, у `known_entities` - откуда значение; прежняя память - «до версии 4.0»);
  3. сверка: сессии, записи, места до и после совпадают - иначе файлы возвращаются из копии, окно сообщает
     «Обновление базы не удалось - данные не тронуты».
Число перенесённых значений прежней памяти - в настройках (`dict_migrated`) для строки в «Словаре».
"""
import shutil
import sqlite3
from pathlib import Path

BACKUP = 'backup-before-4.0'
COUNTED = ('sessions', 'mappings', 'places', 'place_forms', 'exclusions', 'known_entities')


def _version(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute('PRAGMA user_version').fetchone()[0]
    finally:
        con.close()


def counts(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in COUNTED if t in names}
    finally:
        con.close()


def migrate(data_dir: Path, init_db) -> dict:
    """{'done': False} - переход не нужен (новая база или уже 4.0); {'done': True, 'moved': N, 'before', 'after'};
    {'error': '…'} - откат из копии выполнен."""
    data_dir = Path(data_dir)
    db = data_dir / 'anon.db'
    if not db.exists() or _version(db) >= 5:
        init_db(db)
        return {'done': False}
    backup = data_dir / BACKUP
    backup.mkdir(exist_ok=True)
    # WAL: в копию - всё, что ещё не перенесено в основной файл
    con = sqlite3.connect(str(db))
    try:
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if not (backup / 'anon.db').exists():
            con.execute('VACUUM INTO ?', (str(backup / 'anon.db'),))
    finally:
        con.close()
    if (data_dir / 'settings.json').exists() and not (backup / 'settings.json').exists():
        shutil.copy2(data_dir / 'settings.json', backup / 'settings.json')
    before = counts(db)
    try:
        init_db(db)
        after = counts(db)
        con = sqlite3.connect(str(db))
        try:
            moved = con.execute("SELECT COUNT(*) FROM known_entities WHERE source='legacy'").fetchone()[0]
        finally:
            con.close()
        if any(after.get(k) != v for k, v in before.items()):     # new tables (places…) may appear - not a loss
            raise RuntimeError(f'числа не совпали: {before} → {after}')
    except Exception as ex:
        for suffix in ('-wal', '-shm'):
            Path(str(db) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup / 'anon.db', db)
        return {'error': str(ex), 'backup': str(backup)}
    return {'done': True, 'moved': moved, 'before': before, 'after': after, 'backup': str(backup)}
