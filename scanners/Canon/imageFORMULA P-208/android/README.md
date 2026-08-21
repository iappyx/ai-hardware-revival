# P-208 Scan (Android)

The imageFORMULA P-208 driven directly from an Android phone over USB-C. No
computer, no vendor software, no root — the phone is the host and the scanner
is the peripheral.

Jetpack Compose, Material 3, dynamic colour, light and dark.

## What's implemented

- 150 / 200 / 300 / 400 / 600 dpi, colour or greyscale
- Duplex, and batch feeding of a whole stack
- Trim to the sheet, skip blank sides, colour drop-out
- Whole stack as one long image (`continuous`), for a receipt
- Pages can be rotated or deleted individually before saving
- JPEG, PNG and PDF out, with a PDF quality setting
- A diagnostic log in the app, because a scan happens with the USB port
  occupied and nothing can be watched from a computer

## Build

    export JAVA_HOME=/path/to/jdk-17
    echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties
    ./gradlew :app:assembleDebug
    # -> app/build/outputs/apk/debug/app-debug.apk

    adb install -r app/build/outputs/apk/debug/app-debug.apk

Needs JDK 17 and an Android SDK with platform 34. The Gradle wrapper fetches
Gradle itself on first run.

## Hardware notes

**The Auto Start switch on the back must be OFF.** With it on, the unit
enumerates as USB mass storage under a different product id and presents no
scanner interface at all. The app registers both ids so it still launches and
tells you, rather than silently not appearing.

**Power.** The scanner is bus-powered and asks for 500 mA — the whole USB 2.0
budget — to run its motor and lamp. A USB-C phone supplies at least that, and a
Pixel 10 Pro drives it with no hub; it reports the scanner as *charging*
throughout a scan. Older micro-USB OTG hosts often cannot, and a powered hub is
then the answer.

## Notes for anyone porting this

The driver is a transliteration of `../python/driver.py`, which remains the
reference. Four things behave differently under Android's `UsbDeviceConnection`
than under libusb, and each one shows up as an immediate `-1` from
`bulkTransfer` where the Python driver simply works:

1. A bulk IN buffer **smaller than the endpoint's max packet size** (512 here)
   fails outright. Every command ends with a 4-byte status read, so short
   replies are read into a full-packet buffer and trimmed.
2. A single transfer is **capped at 16 KB**. A 1 MiB read becomes 64 transfers.
3. **`setConfiguration` is not done for you**, and without it a device left
   half-configured by a dead session stays mute.
4. When the device has nothing to send yet it **stalls rather than waiting**.
   libusb blocks on the NAKs; here it must be retried, not treated as an error.

And one trap worth stating plainly: `CLEAR_FEATURE(ENDPOINT_HALT)` **resets the
endpoint's data toggle**. Calling it on an endpoint that was not halted
desynchronises host and device, after which every transfer is silently ignored
— including writes — and the state survives re-opening the device. Only clear a
halt on a genuine error path.

## Status

Scans, saves and shares on real hardware. Not yet done: the eSCL bridge has no
Android equivalent, there is no deskew, and the app has only been run against
one device.

## License

See [`../LICENSE`](../LICENSE). Not affiliated with or endorsed by Canon;
written from independent analysis of the hardware, for interoperability.
