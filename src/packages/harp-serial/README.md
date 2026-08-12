# harp-serial

Serial transport for [`harp-device`](../harp-device). Provides `SerialTransport`
and the `open_serial_device` factory, which pairs a `Device` class with a serial
port. This is the package that pulls in `pyserial`.

## Usage

Like the builtin `open`, the returned device is connected and ready; use it in a
`with` block for guaranteed cleanup:

```python
from harp.device import Device, WhoAmI
from harp.serial import open_serial_device

with open_serial_device(Device, port="COM3", baudrate=1_000_000) as dev:
    print(dev.read(WhoAmI).parsed)
```

Pass any `Device` subclass (e.g. a generated device class) instead of the base
`Device` to talk to a specific device. `reply_timeout` and `raise_on_error` are
passed to that shared device layer.

The TCP equivalent has the same construction path and returns the same device
type:

```python
from harp.tcp import accept_tcp_device

with accept_tcp_device(Device, host="0.0.0.0", port=9999) as dev:
    print(dev.read(WhoAmI).parsed)
```

It is named `accept_tcp_device`, rather than `open_tcp_device`, because supported
ESP32 firmware initiates the TCP connection to the Python host.
