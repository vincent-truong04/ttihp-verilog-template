<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

> **Placeholder design:** this is a fixed hello-world UART transmitter. It will be replaced by the programmable
> protocol core in a later revision.

## How it works

The design is a fixed-format UART transmitter running from a 50 MHz system clock. It sends one byte per request
as 8N1 (1 start bit, 8 data bits LSB-first, 1 stop bit) at 115200 baud on `uo_out[0]` (UART_TX). The TX line
idles high, including during reset.

- `ui_in[7:0]` (DATA0–DATA7): the byte to send. It is latched when a transmission starts.
- `uio_in[0]` (GO): pulse high to start sending the byte on `ui_in`. GO is synchronized to the clock and
  triggers on its rising edge. Pulses while a frame is being sent are ignored.
- `uo_out[0]` (UART_TX): serial output.
- `uo_out[1]` (BUSY): high while a frame is being sent.

## How to test

Apply a 50 MHz clock, hold `rst_n` low for a few cycles, then release it. Connect `uo_out[0]` to the RX line of a
UART receiver set to 115200 baud, 8N1. Set a byte on `ui_in[7:0]`, then pulse `uio_in[0]` (GO) high. The byte
appears at the receiver, and `uo_out[1]` (BUSY) stays high until the stop bit finishes. Wait for BUSY to go low
before sending the next byte.

## External hardware

A UART receiver, such as a USB-to-serial adapter, to observe `uo_out[0]`.
