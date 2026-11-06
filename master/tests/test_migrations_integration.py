"""Migraciones SQL: se aplican en orden, una sola vez, también con dos réplicas a la vez."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import engine
from app.migrations import pending_files, run_migrations

TABLE = "migration_test_table"


@pytest.fixture
async def clean(database: None, tmp_path_factory: pytest.TempPathFactory):
    """Deja la base sin la tabla de prueba ni las versiones 9xxx que usan estos tests."""
    # Una corrida sobre un directorio vacío crea el registro sin aplicar nada.
    await run_migrations(engine, tmp_path_factory.mktemp("vacio"))

    async def drop() -> None:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            await connection.execute(text("DELETE FROM schema_migrations WHERE version LIKE '9%'"))

    await drop()
    yield
    await drop()


def _write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


async def _columns() -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": TABLE},
        )
        return set(rows.scalars())


def test_files_are_applied_in_numeric_order(tmp_path: Path) -> None:
    for name in ("9002_b.sql", "9001_a.sql", "9010_c.sql", "notas.sql", "README.md"):
        _write(tmp_path, name, "SELECT 1;")

    assert [path.name for path in pending_files(tmp_path)] == [
        "9001_a.sql",
        "9002_b.sql",
        "9010_c.sql",
    ]


async def test_applies_each_migration_once(clean: None, tmp_path: Path) -> None:
    # Varias sentencias en un archivo: asyncpg las corre con el protocolo simple.
    _write(
        tmp_path, "9001_crea.sql", f"CREATE TABLE {TABLE} (id INT); INSERT INTO {TABLE} VALUES (1);"
    )
    _write(
        tmp_path, "9002_columna.sql", f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS extra TEXT;"
    )

    assert await run_migrations(engine, tmp_path) == ["9001_crea", "9002_columna"]
    assert await _columns() == {"id", "extra"}

    # Segunda vez: todo está registrado y no se ejecuta nada (el INSERT no se repite).
    assert await run_migrations(engine, tmp_path) == []
    async with engine.connect() as connection:
        assert (await connection.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar() == 1


async def test_two_replicas_starting_together_apply_once(clean: None, tmp_path: Path) -> None:
    _write(tmp_path, "9001_crea.sql", f"CREATE TABLE {TABLE} (id INT);")

    first, second = await asyncio.gather(
        run_migrations(engine, tmp_path), run_migrations(engine, tmp_path)
    )

    assert sorted([first, second]) == [[], ["9001_crea"]]


async def test_a_failing_migration_rolls_back_the_whole_batch(clean: None, tmp_path: Path) -> None:
    _write(tmp_path, "9001_crea.sql", f"CREATE TABLE {TABLE} (id INT);")
    _write(tmp_path, "9002_rota.sql", "ALTER TABLE tabla_que_no_existe ADD COLUMN x INT;")

    with pytest.raises(Exception, match="tabla_que_no_existe"):
        await run_migrations(engine, tmp_path)

    # Ni la tabla ni el registro de 9001 sobreviven: master no arranca a medias.
    assert await _columns() == set()
    async with engine.connect() as connection:
        registered = await connection.execute(
            text("SELECT count(*) FROM schema_migrations WHERE version LIKE '9%'")
        )
        assert registered.scalar() == 0
