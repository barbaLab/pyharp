# harp-tcp

TCP/Wi-Fi transport for [`harp-device`](../harp-device). ESP32 Harp firmware is
the TCP client: it connects to the controller endpoint configured in its network
registers. The Python host therefore listens for and accepts one or more device
connections.

The transport uses only Python's standard library. Harp stream framing,
request/reply correlation, and event delivery remain in `harp-device`, so they
behave the same way over serial and TCP.

## Serial-equivalent usage

Both factories accept a `Device` subclass, construct it over an `ITransport`,
open it, and return that same subclass. Consequently all code inside the
context manager is transport-independent:

```python
from harp.device import Device
from harp.serial import open_serial_device
from harp.tcp import accept_tcp_device

# Serial
with open_serial_device(Device, port="COM3", reply_timeout=2.0) as device:
    value = device.read(Device.registers.WhoAmI).parsed

# TCP: the ESP32 connects outward to this listener
with accept_tcp_device(Device, host="0.0.0.0", port=9999, reply_timeout=2.0) as device:
    value = device.read(Device.registers.WhoAmI).parsed
```

The implementation layout is deliberately parallel: `harp.serial._serial`
contains `SerialTransport` and its factory, while `harp.tcp._tcp` contains
`TcpTransport` and its factories.

## Accept one ESP32 client

The listener may select a callback device using any combination of its common
identity registers. Invalid and non-matching candidates are closed while the
listener continues waiting.

```python
from harp.tcp import accept_tcp_device
from my_harp_device import Device

with accept_tcp_device(
    Device,
    host="0.0.0.0",
    port=9999,
    device_name="BehavBox",
    window=25.0,
) as device:
    ...
```

`setup_cb(raw_socket)` can be supplied when work must happen immediately after
`accept()` and before Harp traffic starts, such as a USB-to-Wi-Fi handoff.

## Discover every callback device

```python
from harp.device import Device
from harp.tcp import accept_tcp_devices

with accept_tcp_devices(Device, port=9999, window=10.0) as devices:
    for device in devices:
        identity = devices.identity(device)
        print(identity.name, identity.who_am_i, identity.serial_number, identity.peer)

    behavior = devices.by_name("BehavBox")
    device_1216 = devices.by_id(1216)
```

Discovery probes accepted sockets concurrently, caches `WhoAmI`, `DeviceName`,
and `SerialNumber`, and sorts the result by `WhoAmI` and serial number. Exiting
the list's context manager closes every discovered device.

## Low-level transport

`TcpTransport` structurally implements `harp.device.ITransport` and wraps an
already-accepted socket:

```python
from harp.tcp import TcpTransport

transport = TcpTransport(accepted_socket, timeout=3.0)
device = Device(transport).open()
```

The accept factories create this transport for you. As with `SerialTransport`,
`TcpTransport` is passed to the shared `Device` class and contains no Harp
protocol logic of its own. Reads preserve arbitrary TCP chunking: the shared
`harp-device` framer reassembles partial messages and separates multiple
messages received in one chunk.
