#!/usr/bin/env python3
"""Read and write the M-VAVE SMC-Mixer CC / User profile over USB-MIDI.

The protocol is the one used by deadjoe/SMC-Mixer-Tool: a vendor frame
(00 59, command, 24-bit length, body, ~sum) packed into a bare F0 ... F7 SysEx
message on the mixer's "Private" USB-MIDI port.

This tool uses the ALSA `amidi` utility, so it needs no Python packages.

Examples:
    ./smc_mixer.py ports
    ./smc_mixer.py info
    ./smc_mixer.py dump dumps/settings-before.bin
    ./smc_mixer.py show dumps/settings-before.bin --profile cc
    ./smc_mixer.py apply-opz --dry-run --out profiles/opz-cc-user.bin
    ./smc_mixer.py apply-opz --knob-cc 3
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HDR = b"\x00\x59"
CMD_VERSION = 0x11
CMD_ACK = 0x00
CMD_WRITE = 0x22
CMD_READ = 0x23
SETTINGS_FLASH = 5

PROFILE_SIZE = 3141
SNAPSHOT_SIZE = PROFILE_SIZE * 2
BUTTON_COUNT = 43
BUTTON_SIZE = 71
TRACK_BUTTON_COUNT = 32
GLOBAL_BUTTON_COUNT = 11
KNOB_COUNT = 8
KNOB_SIZE = 6
SLIDER_COUNT = 8
SLIDER_SIZE = 5
KNOBS_OFFSET = BUTTON_COUNT * BUTTON_SIZE
SLIDERS_OFFSET = KNOBS_OFFSET + KNOB_COUNT * KNOB_SIZE
READ_CHUNK = 1009
MAX_WRITE = 256

PROFILE_NAMES = {0: "DAW", 1: "CC / User"}
TRACK_BUTTON_NAMES = ("Mute", "Solo", "Record", "Select")
GLOBAL_BUTTON_NAMES = (
    "Play", "Stop", "Record", "Rewind", "Fast Forward",
    "Bank Previous", "Bank Next", "Up", "Down", "Left", "Right",
)


@dataclass
class Port:
    device: str
    name: str


def le(value: int, size: int) -> bytes:
    return value.to_bytes(size, "little")


def read_le(data: bytes, offset: int, size: int) -> int:
    return int.from_bytes(data[offset:offset + size], "little")


def checksum(body: bytes) -> int:
    return (~sum(body)) & 0xFF


def frame(command: int, body: bytes = b"") -> bytes:
    return HDR + bytes([command]) + le(len(body), 3) + body + bytes([checksum(body)])


def pack7(data: bytes) -> bytes:
    out = bytearray([0xF0])
    acc = 0
    bits = 0
    for byte in data:
        acc |= byte << bits
        bits += 8
        while bits >= 7:
            out.append(acc & 0x7F)
            acc >>= 7
            bits -= 7
    if bits:
        out.append(acc & 0x7F)
    out.append(0xF7)
    return bytes(out)


def unpack7(sysex: bytes) -> bytes:
    body = sysex[1:-1] if sysex[:1] == b"\xF0" and sysex[-1:] == b"\xF7" else sysex
    out = bytearray()
    acc = 0
    bits = 0
    for byte in body:
        acc |= (byte & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    return bytes(out)


def parse_frame(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < 7 or payload[:2] != HDR:
        raise ValueError("not a vendor frame")
    command = payload[2]
    length = read_le(payload, 3, 3)
    if len(payload) != 7 + length:
        raise ValueError("vendor frame length mismatch")
    body = payload[6:6 + length]
    if payload[-1] != checksum(body):
        raise ValueError("vendor frame checksum mismatch")
    return command, body


def list_ports() -> list[Port]:
    result = subprocess.run(["amidi", "-l"], text=True, capture_output=True)
    ports: list[Port] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] in {"IO", "I", "O"}:
            ports.append(Port(parts[1], parts[2].strip()))
    return ports


def resolve_port(requested: str | None) -> str:
    ports = list_ports()
    if requested:
        for port in ports:
            if port.device == requested or requested.lower() in port.name.lower():
                return port.device
        return requested
    candidates = [p for p in ports if "smc" in p.name.lower()]
    private = [p for p in candidates if "private" in p.name.lower()]
    selected = private or candidates
    if not selected:
        lines = "\n".join(f"  {p.device}  {p.name}" for p in ports) or "  (none)"
        raise SystemExit(f"SMC-Mixer MIDI port not found. ALSA raw MIDI ports:\n{lines}")
    if len(selected) > 1:
        lines = "\n".join(f"  {p.device}  {p.name}" for p in selected)
        raise SystemExit(f"More than one SMC-Mixer port; choose one with --port:\n{lines}")
    return selected[0].device


class MixerLink:
    def __init__(self, port: str):
        self.port = port

    def send(self, payload: bytes) -> None:
        sysex = pack7(payload)
        text = " ".join(f"{byte:02X}" for byte in sysex)
        result = subprocess.run(
            ["amidi", "-p", self.port, "-S", text],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "amidi could not send the message")

    def capture(self, payload: bytes, timeout: float) -> list[bytes]:
        process = subprocess.Popen(
            ["amidi", "-p", self.port, "-d", "-t", str(timeout)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(0.12)
            self.send(payload)
            try:
                output, error = process.communicate(timeout=timeout + 3.0)
            except subprocess.TimeoutExpired:
                process.kill()
                output, error = process.communicate()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if output:
            return self._parse_dump(output)
        if error and b"Device or resource busy" in error:
            raise RuntimeError(error.decode(errors="replace").strip())
        return []

    @staticmethod
    def _parse_dump(output: bytes) -> list[bytes]:
        text = output.decode("ascii", "ignore")
        raw = bytes(int(token, 16) for token in re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?![0-9A-Fa-f])", text))
        payloads: list[bytes] = []
        index = 0
        while index < len(raw):
            if raw[index] != 0xF0:
                index += 1
                continue
            end = raw.find(b"\xF7", index + 1)
            if end < 0:
                break
            payloads.append(unpack7(raw[index:end + 1]))
            index = end + 1
        return payloads

    def query_version(self, timeout: float = 1.5) -> bytes:
        for payload in self.capture(frame(CMD_VERSION), timeout):
            try:
                command, body = parse_frame(payload)
            except ValueError:
                continue
            if command == CMD_VERSION:
                return body
        raise RuntimeError("the mixer did not answer the version query")

    def read_chunk(
        self,
        address: int,
        length: int,
        attempts: int = 3,
        timeout: float = 1.5,
        flash_type: int = SETTINGS_FLASH,
    ) -> bytes:
        request = frame(CMD_READ, bytes([flash_type]) + le(address, 4) + le(length, 3))
        for _ in range(attempts):
            for payload in self.capture(request, timeout):
                try:
                    command, body = parse_frame(payload)
                except ValueError:
                    continue
                if command != CMD_READ or len(body) < 8 or body[0] != flash_type:
                    continue
                reply_address = read_le(body, 1, 4)
                reply_length = read_le(body, 5, 3)
                if reply_address == address and reply_length == length and len(body) == 8 + length:
                    return body[8:]
            time.sleep(0.05)
        raise RuntimeError(f"no flash reply at address {address} length {length} type {flash_type}")

    def read_settings(self) -> bytes:
        data = bytearray()
        for address in range(0, SNAPSHOT_SIZE, READ_CHUNK):
            length = min(READ_CHUNK, SNAPSHOT_SIZE - address)
            print(f"\rread {address:4d}/{SNAPSHOT_SIZE}", end="", file=sys.stderr, flush=True)
            data += self.read_chunk(address, length)
        print(file=sys.stderr)
        if len(data) != SNAPSHOT_SIZE:
            raise RuntimeError(f"settings image is {len(data)} bytes, expected {SNAPSHOT_SIZE}")
        return bytes(data)

    def write_record(self, address: int, data: bytes, attempts: int = 2) -> None:
        if not data or len(data) > MAX_WRITE:
            raise ValueError("record length is invalid")
        request = frame(
            CMD_WRITE,
            bytes([SETTINGS_FLASH]) + le(address, 4) + le(len(data), 3) + data,
        )
        accepted = False
        for _ in range(attempts):
            for payload in self.capture(request, 0.7):
                try:
                    command, body = parse_frame(payload)
                except ValueError:
                    continue
                if command == CMD_ACK and len(body) == 1:
                    if body[0] != 0:
                        raise RuntimeError(f"mixer rejected the write, status {body[0]:#04x}")
                    accepted = True
            if accepted:
                break
        if not accepted:
            raise RuntimeError(f"no write acknowledgement at address {address}")
        if self.read_chunk(address, len(data)) != data:
            raise RuntimeError(f"read-back mismatch at address {address}")

    def save(self, attempts: int = 2) -> None:
        request = frame(CMD_WRITE, bytes([SETTINGS_FLASH]) + le(0, 4) + le(0, 3))
        accepted = False
        for _ in range(attempts):
            for payload in self.capture(request, 0.9):
                try:
                    command, body = parse_frame(payload)
                except ValueError:
                    continue
                if command == CMD_ACK and len(body) == 1:
                    if body[0] != 0:
                        raise RuntimeError(f"mixer refused to save, status {body[0]:#04x}")
                    accepted = True
            if accepted:
                break
        if not accepted:
            raise RuntimeError("no save acknowledgement")


def parse_profile(image: bytes, which: int) -> dict:
    if len(image) != SNAPSHOT_SIZE:
        raise ValueError(f"settings image must be {SNAPSHOT_SIZE} bytes")
    base = which * PROFILE_SIZE
    buttons = []
    for index in range(BUTTON_COUNT):
        offset = base + index * BUTTON_SIZE
        buttons.append({
            "mode": image[offset],
            "channel": image[offset + 1],
            "cc": image[offset + 2],
            "value1": image[offset + 3],
            "value2": image[offset + 4],
            "led": image[offset + BUTTON_SIZE - 1],
        })
    knobs = []
    for index in range(KNOB_COUNT):
        offset = base + KNOBS_OFFSET + index * KNOB_SIZE
        knobs.append({
            "type": image[offset],
            "speed": image[offset + 1],
            "channel": image[offset + 2],
            "cc": image[offset + 3],
            "minimum": image[offset + 4],
            "maximum": image[offset + 5],
        })
    sliders = []
    for index in range(SLIDER_COUNT):
        offset = base + SLIDERS_OFFSET + index * SLIDER_SIZE
        sliders.append({
            "type": image[offset],
            "channel": image[offset + 1],
            "cc": image[offset + 2],
            "minimum": image[offset + 3],
            "maximum": image[offset + 4],
        })
    return {"buttons": buttons, "knobs": knobs, "sliders": sliders}


def apply_opz(image: bytes, solo: str, knob_cc: int | None = None) -> bytes:
    data = bytearray(image)
    base = PROFILE_SIZE
    for index in range(8):
        offset = base + SLIDERS_OFFSET + index * SLIDER_SIZE
        data[offset:offset + SLIDER_SIZE] = bytes([0, index, 16, 0, 127])

        offset = base + (index * 4) * BUTTON_SIZE
        data[offset:offset + 5] = bytes([1, index, 53, 0, 127])

        offset = base + (index * 4 + 1) * BUTTON_SIZE
        if solo == "cc":
            data[offset:offset + 5] = bytes([1, 0, 20 + index, 0, 127])
        elif solo == "mute":
            data[offset:offset + 5] = bytes([1, index, 53, 0, 127])

        if knob_cc is not None:
            offset = base + KNOBS_OFFSET + index * KNOB_SIZE
            data[offset:offset + KNOB_SIZE] = bytes([0, 2, index, knob_cc, 0, 127])
    return bytes(data)


def user_spans(old: bytes, new: bytes) -> list[tuple[int, bytes]]:
    spans: list[tuple[int, bytes]] = []
    base = PROFILE_SIZE
    for index in range(BUTTON_COUNT):
        offset = base + index * BUTTON_SIZE
        if old[offset:offset + BUTTON_SIZE] != new[offset:offset + BUTTON_SIZE]:
            spans.append((offset, new[offset:offset + BUTTON_SIZE]))
    for index in range(KNOB_COUNT):
        offset = base + KNOBS_OFFSET + index * KNOB_SIZE
        if old[offset:offset + KNOB_SIZE] != new[offset:offset + KNOB_SIZE]:
            spans.append((offset, new[offset:offset + KNOB_SIZE]))
    for index in range(SLIDER_COUNT):
        offset = base + SLIDERS_OFFSET + index * SLIDER_SIZE
        if old[offset:offset + SLIDER_SIZE] != new[offset:offset + SLIDER_SIZE]:
            spans.append((offset, new[offset:offset + SLIDER_SIZE]))
    return spans


def write_user_settings(link: MixerLink, old: bytes, new: bytes) -> None:
    spans = user_spans(old, new)
    if not spans:
        print("No changes in the CC / User profile.")
        return
    print(f"Writing {len(spans)} changed CC / User records.")
    for number, (address, data) in enumerate(spans, 1):
        print(f"\rwrite {number}/{len(spans)} at 0x{address:04X}", end="", file=sys.stderr, flush=True)
        link.write_record(address, data)
    print(file=sys.stderr)
    print("Saving to flash.")
    link.save()


def show_image(image: bytes, which: int) -> None:
    profile = parse_profile(image, which)
    print(f"{PROFILE_NAMES[which]} profile")
    print("\nFaders:")
    for index, slider in enumerate(profile["sliders"], 1):
        print(
            f"  {index}: type={slider['type']} channel={slider['channel'] + 1} "
            f"cc={slider['cc']} min={slider['minimum']} max={slider['maximum']}"
        )
    print("\nKnobs:")
    for index, knob in enumerate(profile["knobs"], 1):
        print(
            f"  {index}: type={knob['type']} speed={knob['speed']} "
            f"channel={knob['channel'] + 1} cc={knob['cc']} "
            f"min={knob['minimum']} max={knob['maximum']}"
        )
    print("\nTrack buttons:")
    for index, button in enumerate(profile["buttons"][:TRACK_BUTTON_COUNT]):
        track = index // 4 + 1
        name = TRACK_BUTTON_NAMES[index % 4]
        print(
            f"  {track:2d} {name:6s}: mode={button['mode']} channel={button['channel'] + 1} "
            f"cc={button['cc']} v1={button['value1']} v2={button['value2']} led={button['led']}"
        )
    print("\nGlobal buttons:")
    for index, button in enumerate(profile["buttons"][TRACK_BUTTON_COUNT:]):
        print(
            f"  {GLOBAL_BUTTON_NAMES[index]:13s}: mode={button['mode']} "
            f"channel={button['channel'] + 1} cc={button['cc']} "
            f"v1={button['value1']} v2={button['value2']} led={button['led']}"
        )


def print_mapping(knob_cc: int | None = None) -> None:
    print("OP-Z mapping written to CC / User:")
    for index in range(8):
        print(f"  fader {index + 1}: CC16 channel {index + 1} (track {index + 1} volume)")
        print(f"  mute  {index + 1}: CC53 channel {index + 1}, toggle 0/127")
        if knob_cc is not None:
            print(f"  knob  {index + 1}: CC{knob_cc} channel {index + 1} "
                  f"(track {index + 1} parameter)")


def load_image(path: str) -> bytes:
    data = Path(path).read_bytes()
    if len(data) != SNAPSHOT_SIZE:
        raise SystemExit(f"{path} is {len(data)} bytes; expected {SNAPSHOT_SIZE}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ports", help="list ALSA raw MIDI ports")

    info = sub.add_parser("info", help="query the mixer name and version")
    info.add_argument("--port")

    dump = sub.add_parser("dump", help="read the 6,282-byte settings image")
    dump.add_argument("output")
    dump.add_argument("--port")

    show = sub.add_parser("show", help="print a settings image")
    show.add_argument("image")
    show.add_argument("--profile", choices=["daw", "cc"], default="cc")

    apply = sub.add_parser("apply-opz", help="write the OP-Z fader/mute/knob mapping to CC / User")
    apply.add_argument("--port")
    apply.add_argument("--image", help="edit this image instead of reading the mixer")
    apply.add_argument("--out", help="also save the modified image here")
    apply.add_argument("--dry-run", action="store_true", help="do not write to the mixer")
    apply.add_argument("--solo", choices=["cc", "mute", "none"], default="cc",
                       help="solo buttons: unique CC20-27 (default), mirror mute, or leave unchanged")
    apply.add_argument("--knob-cc", type=int, metavar="CC",
                       help="also map knobs 1-8 to this absolute CC on channels 1-8")

    write = sub.add_parser("write", help="write the CC / User profile from an image")
    write.add_argument("image")
    write.add_argument("--port")

    args = parser.parse_args()
    if args.command == "apply-opz" and args.knob_cc is not None and not 0 <= args.knob_cc <= 127:
        parser.error("--knob-cc must be between 0 and 127")

    if args.command == "ports":
        ports = list_ports()
        if not ports:
            print("No ALSA raw MIDI ports.")
            return 1
        for port in ports:
            print(f"{port.device:20s} {port.name}")
        return 0

    if args.command == "show":
        image = load_image(args.image)
        show_image(image, 1 if args.profile == "cc" else 0)
        return 0

    if args.command == "info":
        link = MixerLink(resolve_port(args.port))
        body = link.query_version()
        text = body.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        print(text or body.hex(" "))
        return 0

    if args.command == "dump":
        link = MixerLink(resolve_port(args.port))
        image = link.read_settings()
        Path(args.output).write_bytes(image)
        print(f"Wrote {len(image)} bytes to {args.output}")
        return 0

    if args.command == "write":
        link = MixerLink(resolve_port(args.port))
        image = load_image(args.image)
        current = link.read_settings()
        write_user_settings(link, current, image)
        return 0

    if args.command == "apply-opz":
        link: MixerLink | None = None
        if args.image:
            image = load_image(args.image)
        else:
            link = MixerLink(resolve_port(args.port))
            image = link.read_settings()
        updated = apply_opz(image, args.solo, args.knob_cc)
        print_mapping(args.knob_cc)
        if args.solo == "cc":
            print("  solo 1-8: CC20-27 channel 1 (needs stateful solo logic; no native OP-Z solo CC)")
        elif args.solo == "mute":
            print("  solo 1-8: mirror mute (CC53 on their own channel)")
        else:
            print("  solo 1-8: left unchanged")
        if args.out:
            Path(args.out).write_bytes(updated)
            print(f"Wrote modified image to {args.out}")
        if args.dry_run:
            print("Dry run: the mixer was not changed.")
            return 0
        if link is None:
            link = MixerLink(resolve_port(args.port))
        write_user_settings(link, image, updated)
        return 0

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
