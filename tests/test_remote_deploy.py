"""deploy/remote-deploy.sh con `docker`, `nginx` y `systemctl` de mentira.

Comprueba el reinicio escalonado, el rollback automático cuando la versión nueva no queda
healthy y que la config de Nginx sólo se instala si pasa `nginx -t`.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "remote-deploy.sh"

# `docker compose ... up --wait` falla si la imagen de master en release.env es la marcada
# como rota: así se simula una versión que no queda healthy.
FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
if [[ " $* " == *" up "* && " $* " == *" --wait "* ]] && grep -q "MASTER_IMAGE=$BROKEN" release.env
then
    exit 1
fi
exit 0
"""
FAKE_NGINX = """#!/usr/bin/env bash
echo "nginx $*" >> "$CALLS"
exit "${NGINX_RC:-0}"
"""
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="necesita bash")


@pytest.fixture
def ec2(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("docker", FAKE_DOCKER),
        ("nginx", FAKE_NGINX),
        ("systemctl", FAKE_SYSTEMCTL),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text("POSTGRES_USER=x\n")
    site = tmp_path / "site.conf"
    site.write_text("config vieja\n")
    return {"app": app, "site": site, "bin": bin_dir, "calls": tmp_path / "calls.log"}


def deploy(
    ec2, master, compose="compose nuevo\n", nginx="config vieja\n", broken="nada", nginx_rc=0
):
    (ec2["app"] / "docker-compose.prod.yml.new").write_text(compose)
    (ec2["app"] / "energyshark-nginx.conf.new").write_text(nginx)
    env = {
        **os.environ,
        "PATH": f"{ec2['bin']}:{os.environ['PATH']}",
        "APP_DIR": str(ec2["app"]),
        "NGINX_SITE": str(ec2["site"]),
        "CALLS": str(ec2["calls"]),
        "BROKEN": broken,
        "NGINX_RC": str(nginx_rc),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), master, master.replace("master", "connector")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def release(ec2) -> str:
    return (ec2["app"] / "release.env").read_text()


def test_primer_deploy_actualiza_las_replicas_de_a_una(ec2):
    result = deploy(ec2, "reg/master:v1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MASTER_IMAGE=reg/master:v1" in release(ec2)
    calls = ec2["calls"].read_text().splitlines()
    ups = [c.split(" up ")[1] for c in calls if " up " in c]
    assert ups == [
        "-d --no-deps --wait --wait-timeout 120 postgres",
        "-d --no-deps --wait --wait-timeout 120 master-1",
        "-d --no-deps --wait --wait-timeout 120 master-2",
        "-d --remove-orphans --wait --wait-timeout 180",
    ]
    assert "nginx sin cambios" in result.stdout


def test_version_que_no_queda_healthy_vuelve_a_la_anterior(ec2):
    assert deploy(ec2, "reg/master:v1", compose="compose v1\n").returncode == 0

    result = deploy(ec2, "reg/master:v2", compose="compose v2\n", broken="reg/master:v2")

    assert result.returncode == 1
    assert "ROLLBACK OK" in result.stdout
    assert "MASTER_IMAGE=reg/master:v1" in release(ec2)
    assert (ec2["app"] / "docker-compose.prod.yml").read_text() == "compose v1\n"
    # Nginx no se toca si el deploy falló.
    assert "nginx -t" not in ec2["calls"].read_text()


def test_sin_version_anterior_el_rollback_avisa(ec2):
    result = deploy(ec2, "reg/master:v1", broken="reg/master:v1")
    assert result.returncode == 1
    assert "ROLLBACK FALLIDO" in result.stdout


def test_nginx_nuevo_se_instala_solo_si_pasa_la_validacion(ec2):
    ok = deploy(ec2, "reg/master:v1", nginx="config nueva\n")
    assert ok.returncode == 0
    assert ec2["site"].read_text() == "config nueva\n"
    assert "systemctl reload nginx" in ec2["calls"].read_text()

    bad = deploy(ec2, "reg/master:v2", nginx="config rota\n", nginx_rc=1)
    assert bad.returncode == 1
    assert ec2["site"].read_text() == "config nueva\n"
