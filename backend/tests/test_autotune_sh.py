"""scripts/autotune.sh:未设→export min(cores,8);显式优先;AUTOTUNE=0 关闭。"""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]  # backend/tests -> repo root
SCRIPT = ROOT / "scripts" / "autotune.sh"


def _run(env):
    base = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    base.update(env)
    return subprocess.run(
        ["bash", "-c", f"source '{SCRIPT}'; echo \"OMP=${{OMP_NUM_THREADS-}}\""],
        capture_output=True, text=True, env=base,
    ).stdout.strip()


def test_autotune_64_cores_caps_blas_at_8():
    assert _run({"CORES": "64"}) == "OMP=8"


def test_autotune_small_machine_uses_all():
    assert _run({"CORES": "4"}) == "OMP=4"


def test_autotune_disabled_sets_nothing():
    assert _run({"CORES": "64", "AUTOTUNE": "0"}) == "OMP="


def test_explicit_omp_is_preserved():
    assert _run({"CORES": "64", "OMP_NUM_THREADS": "2"}) == "OMP=2"
