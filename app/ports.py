import socket
from contextlib import closing

# Slot N: VNC = VNC_HOST_BASE + N, CDP = CDP_HOST_BASE + N,
# noVNC (container 8080/tcp) = NOVNC_HOST_BASE + N (N = 0, 1, 2, ...)
VNC_HOST_BASE = 5901
CDP_HOST_BASE = 9223
NOVNC_HOST_BASE = 6080
_MAX_SLOT = 10_000


def _can_bind_port(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
        except OSError:
            return False
        return True


def allocate_cdp_port(used_ports: set[int]) -> int:
    """
    Walk CDP ports upward from CDP_HOST_BASE; skip ports already used by the
    pool or not bindable on the host.
    """
    for k in range(_MAX_SLOT):
        cdp = CDP_HOST_BASE + k
        if cdp in used_ports:
            continue
        if _can_bind_port(cdp):
            return cdp
    raise RuntimeError(
        f"No free CDP port (tried slots 0..{_MAX_SLOT - 1} from {CDP_HOST_BASE})"
    )


def allocate_sequential_pool_ports(used_ports: set[int]) -> tuple[int, int, int]:
    """
    Walk triples upward from 5901/9223/6080 with the same offset; skip ports
    already used by the pool or not bindable on the host.
    """
    for k in range(_MAX_SLOT):
        vnc = VNC_HOST_BASE + k
        cdp = CDP_HOST_BASE + k
        novnc = NOVNC_HOST_BASE + k
        if vnc in used_ports or cdp in used_ports or novnc in used_ports:
            continue
        if _can_bind_port(vnc) and _can_bind_port(cdp) and _can_bind_port(novnc):
            return vnc, cdp, novnc
    raise RuntimeError(
        f"No free sequential pool ports (tried slots 0..{_MAX_SLOT - 1})"
    )
