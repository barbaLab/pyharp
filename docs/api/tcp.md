# TCP API

The TCP package accepts connections initiated by ESP32 devices and can select
or discover callback devices through a host listener. It uses the same shared
`harp.device.Device` class and `ITransport` contract as `harp-serial`; only the
factory verb differs (`accept_tcp_device` instead of `open_serial_device`). See the
[`harp-tcp` package guide](https://github.com/harp-tech/pyharp/tree/main/src/packages/harp-tcp)
for complete examples.

::: harp.tcp.TcpTransport

::: harp.tcp.TcpDeviceIdentity

::: harp.tcp.TcpDeviceList

::: harp.tcp.accept_tcp_device

::: harp.tcp.accept_tcp_devices
