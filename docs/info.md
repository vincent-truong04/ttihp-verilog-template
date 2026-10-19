<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

> **Work in progress:** the protocol emulator logic is still being developed. The current `src/project.v` is the template placeholder, so the behavior described below is not yet implemented.

## How it works

The Programmable Protocol Emulator is a digital design that emulates serial communication protocols on the
Tiny Tapeout pins. It runs from a 50 MHz system clock and drives serial data out on `uo[0]` (UART_TX).

## How to test

Apply a 50 MHz clock, hold `rst_n` low for a few cycles, then release it. Connect `uo[0]` to the RX line of a
UART receiver, such as a USB-serial adapter, and watch the transmitted data.

## External hardware

A UART receiver, such as a USB-to-serial adapter, to observe `uo[0]`.
