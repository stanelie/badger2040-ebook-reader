USB_VID = 0x2E8A
USB_PID = 0x101B
USB_PRODUCT = "Badger 2040"
USB_MANUFACTURER = "Pimoroni"

CHIP_VARIANT = RP2040
CHIP_FAMILY = rp2

EXTERNAL_FLASH_DEVICES = "W25Q16JVxQ"

CIRCUITPY__EVE = 1
CIRCUITPY_PICODVI = 0
CIRCUITPY_USB_HOST = 0

# Include the badger2040 wake button module
SRC_C += boards/pimoroni_badger2040_stan/badger2040.c

# Enable alarm module for deep sleep
CIRCUITPY_ALARM = 1

