"""Command-line interface for pyFlir.

Entry point registered as the ``pyflir`` console script.

Usage::

    pyflir discover     Find cameras on the network
    pyflir info         Show camera configuration
    pyflir grab         Grab a single frame and save to file
    pyflir live         Open live viewer (requires gui extra)
    pyflir setup        Print OS network setup hints
"""

from __future__ import annotations

import argparse
import sys


def cmd_discover(args: argparse.Namespace) -> int:
    """Find cameras on the network and print a summary table."""
    from .camera import discover

    cameras = discover(timeout=args.timeout)
    if not cameras:
        print("No cameras found.")
        return 1

    for i, cam in enumerate(cameras):
        print(f"\n[{i}] {cam.get('manufacturer', '?')} {cam.get('model', '?')}")
        print(f"    IP:      {cam['ip']}")
        print(f"    Serial:  {cam.get('serial', '?')}")
        print(f"    Version: {cam.get('device_version', '?')}")

    print(f"\nFound {len(cameras)} camera(s)")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Connect to a camera and print its current configuration."""
    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        if args.xml:
            cam.load_xml(args.xml)
        info = cam.info()
        for key, val in info.items():
            print(f"  {key:20s}: {val}")
    return 0


def cmd_grab(args: argparse.Namespace) -> int:
    """Grab a single frame from the camera and save it to a file."""
    import numpy as np

    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        if args.xml:
            cam.load_xml(args.xml)
        else:
            cam.download_xml()
            cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        if args.exposure_ms is not None:
            cam.exposure_ms = args.exposure_ms

        frame = cam.grab(timeout=args.timeout)

    output = args.output or "frame.npy"
    if output.endswith(".npy"):
        np.save(output, frame)
    elif output.endswith(".csv"):
        np.savetxt(output, frame, delimiter=",", fmt="%d")
    else:
        np.save(output + ".npy", frame)
        output += ".npy"

    print(f"Saved {frame.shape} {frame.dtype} to {output}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Open the live thermal viewer window (requires the gui extra)."""
    from .camera import Camera

    with Camera(ip=args.ip) as cam:
        if args.xml:
            cam.load_xml(args.xml)
        else:
            cam.download_xml()
            cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")
        cam.live_view(colormap=args.colormap, scale=args.scale)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Print OS-specific setup instructions for GigE Vision camera use."""
    import platform

    system = platform.system()

    if system == "Windows":
        python_exe = sys.executable
        print("Windows GigE Vision setup")
        print("=" * 40)
        print()
        print(f"Python: {python_exe}")
        print()
        print("1. Firewall rule for GVSP (inbound UDP):")
        print(
            f"   netsh advfirewall firewall add rule "
            f'name="pyFlir-GVSP" dir=in action=allow '
            f'protocol=UDP program="{python_exe}"'
        )
        print()
        print("2. Set camera network adapter to Private profile:")
        print("   Set-NetConnectionProfile -InterfaceAlias 'Ethernet 5' -NetworkCategory Private")
        print()
        print("3. (Optional) Enable jumbo frames for higher throughput:")
        print("   Set MTU to 9000 in adapter properties > Advanced")
        print()

    elif system == "Linux":
        print("Linux GigE Vision setup")
        print("=" * 40)
        print()
        print("1. Increase UDP receive buffer:")
        print("   sudo sysctl -w net.core.rmem_max=16777216")
        print("   sudo sysctl -w net.core.rmem_default=16777216")
        print()
        print("2. (Optional) Enable jumbo frames:")
        print("   sudo ip link set eth0 mtu 9000")
        print()
        print("3. Firewall (if active):")
        print("   sudo ufw allow in proto udp to any port 3956")
        print("   sudo ufw allow in proto udp from 169.254.0.0/16")
        print()
    else:
        print(f"No setup instructions for {system}")
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the appropriate sub-command handler."""
    parser = argparse.ArgumentParser(
        prog="pyflir", description="pyFlir: FLIR thermal camera driver"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_get_version()}")

    sub = parser.add_subparsers(dest="command")

    # discover
    p_disc = sub.add_parser("discover", help="Find cameras on the network")
    p_disc.add_argument("--timeout", type=float, default=2.0)

    # info
    p_info = sub.add_parser("info", help="Show camera configuration")
    p_info.add_argument("--ip", default=None, help="Camera IP (auto-discover if omitted)")
    p_info.add_argument("--xml", default=None, help="Path to GenICam XML file")

    # grab
    p_grab = sub.add_parser("grab", help="Grab a single frame")
    p_grab.add_argument("-o", "--output", default="frame.npy", help="Output file (.npy or .csv)")
    p_grab.add_argument("--ip", default=None)
    p_grab.add_argument("--xml", default=None, help="Path to GenICam XML file")
    p_grab.add_argument(
        "--exposure-ms",
        type=float,
        default=None,
        dest="exposure_ms",
        help="Integration time in milliseconds",
    )
    p_grab.add_argument("--timeout", type=float, default=5.0)

    # live
    p_live = sub.add_parser("live", help="Open live viewer (requires gui extra)")
    p_live.add_argument("--ip", default=None)
    p_live.add_argument("--xml", default=None, help="Path to GenICam XML file")
    p_live.add_argument("--colormap", default="inferno", help="Matplotlib colormap name")
    p_live.add_argument("--scale", type=int, default=2, help="Display scale factor")

    # setup
    sub.add_parser("setup", help="Print OS network setup hints")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    commands = {
        "discover": cmd_discover,
        "info": cmd_info,
        "grab": cmd_grab,
        "live": cmd_live,
        "setup": cmd_setup,
    }

    return commands[args.command](args)


def _get_version() -> str:
    try:
        from . import __version__

        return __version__
    except ImportError:
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
