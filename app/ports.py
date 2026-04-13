import socket
from contextlib import closing

# Slot N: VNC = VNC_HOST_BASE + N, CDP = CDP_HOST_BASE + N (N = 0, 1, 2, ...)
VNC_HOST_BASE = 5901
CDP_HOST_BASE = 9223
_MAX_SLOT = 10_000


def _can_bind_port(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
        except OSError:
            return False
        return True


def allocate_sequential_pool_ports(used_ports: set[int]) -> tuple[int, int]:
    """
    Walk pairs upward from 5901/9223 with the same offset; skip ports already
    used by the pool or not bindable on the host.
    """
    for k in range(_MAX_SLOT):
        vnc = VNC_HOST_BASE + k
        cdp = CDP_HOST_BASE + k
        if vnc in used_ports or cdp in used_ports:
            continue
        if _can_bind_port(vnc) and _can_bind_port(cdp):
            return vnc, cdp
    raise RuntimeError(
        f"No free sequential pool ports (tried slots 0..{_MAX_SLOT - 1})"
    )
