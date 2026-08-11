# AI Hardware Revival

**Keeping working hardware out of landfills by rebuilding the software it lost.**

Perfectly good hardware gets thrown away every day — not because it broke, but
because its *drivers* did. A vendor drops support, ships no 64-bit / Apple
Silicon / modern-Android build, and a scanner, printer, or capture card that
still works flawlessly becomes e-waste.

This project is (aiming to become) a growing collection of **clean-room, AI-assisted drivers for
abandoned hardware** — reverse-engineered from the original firmware and
reimplemented as open, dependency-light code that runs on current platforms. The
goal is simple: if the silicon still works, the software should too.

---

## Why this exists

- **E-waste is a hardware problem with a software cause.** Millions of functional
  devices are discarded because the driver stopped, not the device.
- **Reverse engineering just got cheaper.** Modern AI can decompile a vendor
  driver, document its register protocol, regenerate its lookup tables, and write
  a clean replacement — work that used to take a specialist months.
- **Open drivers outlive vendors.** A documented, MIT-licensed driver can be
  ported, fixed, and kept alive by anyone, forever.

## What's here

Each driver is a self-contained project: modern code, a written spec of the
reverse-engineered protocol, and honest notes on what's verified versus
work-in-progress.

| Device | Type | Platforms | Status | Project |
|--------|------|-----------|--------|---------|
| Canon CanoScan 8000F | Flatbed scanner (USB) | macOS/Linux (Python CLI + GUI) | 75/300/600 dpi verified; colour/gray/line-art, 8/16-bit; 150/1200 dpi WIP | [`scan8000f`](https://github.com/iappyx/scan8000f) |


## Principles

- **Open.** MIT-licensed, documented, hackable.
- **Clean-room / interoperability.** Drivers are reimplementations for
  interoperability. **No proprietary firmware or vendor binaries are
  redistributed** — only original code and written specifications.
- **Reproducible.** Each project ships the reverse-engineering notes, not just
  the result.
- **Honest.** Experimental status, known limitations, and untested paths are
  stated plainly.

## Contributing

Three ways to help:

- **Revive a device.** Bring a driver for hardware you own. The usual
  ingredients: a USB capture of the original driver in action, the vendor driver
  binary to analyse, and — crucially — the physical device to test against.
- **Test an existing driver.** Run a project on real hardware and report back;
  hardware test reports are as valuable as code.

## A note on safety

These drivers talk directly to hardware — moving motors, lamps, heads, and
high-current components. They are experimental and AI-generated. **Read each
project's safety notes, and use at your own risk.** Bugs can, in principle,
stress or damage a device.

## Legal

Not affiliated with or endorsed by any hardware manufacturer. Product and brand
names are trademarks of their respective owners and are used only to identify
the hardware a driver interoperates with. All drivers are independent, clean-room
reimplementations produced for interoperability. No proprietary firmware, driver
binaries, or other vendor materials are distributed by this project.

## License

MIT unless a project states otherwise. See each project's `LICENSE`.
