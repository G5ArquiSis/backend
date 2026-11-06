"""Healthcheck del connector: cada consumidor esperado debe tener señal de vida reciente."""

import os
import time

import pytest

from config import Settings
from heartbeat import expected_consumers, heartbeat_file, record_heartbeat, stale_consumers


def settings_for(tmp_path, **overrides) -> Settings:
    values = {
        "heartbeat_path": tmp_path / "hb",
        "heartbeat_max_age_seconds": 120,
        "city_broker_password": "",
        "market_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def beat(settings: Settings, consumer: str, age_seconds: float = 0) -> None:
    path = heartbeat_file(consumer, settings)
    path.touch()
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ["observer"]),
        ({"city_broker_password": "x"}, ["observer", "ciudad"]),
        ({"city_broker_password": "x", "market_enabled": True}, ["observer", "ciudad", "mercado"]),
        # Sin credenciales de la ciudad no hay consumo del mercado, aunque el flag esté encendido.
        ({"market_enabled": True}, ["observer"]),
    ],
)
def test_consumidores_esperados_segun_configuracion(tmp_path, overrides, expected) -> None:
    assert expected_consumers(settings_for(tmp_path, **overrides)) == expected


def test_sano_cuando_todos_los_esperados_laten(tmp_path) -> None:
    settings = settings_for(tmp_path, city_broker_password="x")
    beat(settings, "observer")
    beat(settings, "ciudad", age_seconds=60)
    assert stale_consumers(settings) == []


def test_un_consumidor_colgado_no_queda_oculto_por_otro(tmp_path) -> None:
    settings = settings_for(tmp_path, city_broker_password="x")
    beat(settings, "observer")
    beat(settings, "ciudad", age_seconds=121)
    assert stale_consumers(settings) == ["ciudad"]


def test_mercado_encendido_sin_su_consumidor_queda_unhealthy(tmp_path) -> None:
    settings = settings_for(tmp_path, city_broker_password="x", market_enabled=True)
    beat(settings, "observer")
    beat(settings, "ciudad")
    assert stale_consumers(settings) == ["mercado"]


def test_record_heartbeat_escribe_el_archivo_del_consumidor(tmp_path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    monkeypatch.setattr("heartbeat.get_settings", lambda: settings)
    record_heartbeat("observer")
    assert (tmp_path / "hb.observer").exists()
    with pytest.raises(ValueError):
        record_heartbeat("otro")
