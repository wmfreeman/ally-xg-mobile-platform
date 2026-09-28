"""core/gpu_roles.py — resolve a GPU ROLE to the device it names TODAY.

The scratch resolver (~/gpu_resolve.sh) moved into the repo proper.
HIP/ROCm enumeration order is NOT stable across reboots or eGPU hotplug,
so nothing may ever hardcode a device index. A role's identity is the
gfx target + PCI address:

    egpu -> gfx1031 @ pci 0000:03:00.0   (RX 6850M XT, 12 GiB VRAM)
    igpu -> gfx1103 @ pci 0000:09:00.0   (Radeon 780M,   4 GiB VRAM)

``resolve_role`` maps a role to its CURRENT HIP index — the position of
the role's gfx target among the GPU agents ``rocm_agent_enumerator``
reports, in enumerator order — and verifies the render node for the
role's PCI address exists under /dev/dri/by-path. BOTH halves of the
identity must check out; anything less raises ``GpuRoleUnavailable``
LOUDLY. There is deliberately NO fallback to another device: the egpu
role silently landing on the 4 GiB iGPU would OOM a 14B model in the
most confusing way possible, which is exactly the failure this module
exists to prevent.

Pure resolution, no side effects. The consumer (core.model_host) turns
``hip_index`` into HIP_VISIBLE_DEVICES for the child process. Probing
(``rocm_agent_enumerator``, /dev/dri) is injectable so tests never
touch real hardware.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

_ROCM_ENUMERATOR = "/opt/rocm/bin/rocm_agent_enumerator"
_DEFAULT_DEV_ROOT = Path("/dev/dri/by-path")


class GpuRoleUnavailable(RuntimeError):
    """The requested role's device is not present RIGHT NOW (eGPU
    detached, ROCm missing, render node gone). Loud by design — callers
    must surface this, never fall through to a different device."""


@dataclass(frozen=True)
class GpuIdentity:
    """What a role IS, independent of today's enumeration order."""
    gfx: str            # ROCm gfx target, e.g. "gfx1031"
    pci: str            # PCI address, e.g. "0000:03:00.0"
    description: str

    @property
    def render_node(self) -> str:
        return f"pci-{self.pci}-render"


@dataclass(frozen=True)
class ResolvedGpu:
    """A role pinned to its CURRENT seat. ``hip_index`` is only valid
    until the next reboot/hotplug — resolve again per launch, never
    persist it as configuration."""
    role: str
    gfx: str
    pci: str
    hip_index: int
    render_path: str


# The known roles of this workstation (GlaDOS). Identity constants live
# in code, like mode profiles: editing a role means editing this file.
ROLES: Mapping[str, GpuIdentity] = MappingProxyType({
    "egpu": GpuIdentity(gfx="gfx1031", pci="0000:03:00.0",
                        description="RX 6850M XT, 12 GiB VRAM"),
    "igpu": GpuIdentity(gfx="gfx1103", pci="0000:09:00.0",
                        description="Radeon 780M, 4 GiB VRAM"),
})


def role_names() -> list[str]:
    return sorted(ROLES)


def _default_enumerate() -> list[str]:
    """GPU agents in enumerator order (the order HIP indexes follow).
    Local binary, local bus — no network, no model, no firewall."""
    exe = _ROCM_ENUMERATOR if Path(_ROCM_ENUMERATOR).exists() \
        else shutil.which("rocm_agent_enumerator")
    if exe is None:
        raise GpuRoleUnavailable(
            "rocm_agent_enumerator not found (looked at "
            f"{_ROCM_ENUMERATOR} and $PATH) — is ROCm installed?")
    try:
        out = subprocess.run([exe], capture_output=True, text=True,
                             timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise GpuRoleUnavailable(
            f"rocm_agent_enumerator failed: {e}") from e
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def resolve_role(role: str, *,
                 enumerate_fn: Callable[[], list[str]] | None = None,
                 dev_root: Path | None = None,
                 roles: Mapping[str, GpuIdentity] | None = None) -> ResolvedGpu:
    """Resolve ``role`` to today's HIP index + render node, or raise.

    ``ValueError`` for a role this machine does not define at all;
    ``GpuRoleUnavailable`` when the role exists but its device is not
    fully present (gfx target missing from the enumerator, or the PCI
    render node absent — a detached eGPU fails both).
    """
    table = roles if roles is not None else ROLES
    ident = table.get(role)
    if ident is None:
        raise ValueError(
            f"unknown GPU role {role!r} (one of {sorted(table)}); roles "
            f"are defined in core/gpu_roles.py")

    agents = (enumerate_fn or _default_enumerate)()
    # gfx000 is the CPU agent — not a GPU, never counted in HIP order.
    gpus = [a for a in agents if a != "gfx000"]
    try:
        hip_index = gpus.index(ident.gfx)
    except ValueError:
        raise GpuRoleUnavailable(
            f"GPU role {role!r} ({ident.gfx} @ {ident.pci}, "
            f"{ident.description}) is NOT present: enumerator reports "
            f"{gpus or ['no GPU agents']} — eGPU detached? Refusing to "
            f"fall back to another device.") from None

    render_path = (dev_root if dev_root is not None
                   else _DEFAULT_DEV_ROOT) / ident.render_node
    if not render_path.exists():
        raise GpuRoleUnavailable(
            f"GPU role {role!r}: {ident.gfx} is enumerated but the render "
            f"node {render_path} is missing — device not fully present "
            f"(eGPU mid-hotplug?). Refusing to guess.")

    return ResolvedGpu(role=role, gfx=ident.gfx, pci=ident.pci,
                       hip_index=hip_index, render_path=str(render_path))
