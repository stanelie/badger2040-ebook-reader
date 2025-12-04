// This file is part of the CircuitPython project: https://circuitpython.org
//
// SPDX-FileCopyrightText: Copyright (c) 2021 Scott Shawcroft for Adafruit Industries
//
// SPDX-License-Identifier: MIT

#include "supervisor/board.h"

#include "mpconfigboard.h"
#include "shared-bindings/busio/SPI.h"
#include "shared-bindings/fourwire/FourWire.h"
#include "shared-bindings/microcontroller/Pin.h"
#include "shared-module/displayio/__init__.h"
#include "supervisor/shared/board.h"
#include "badger-shared.h"

#include "hardware/gpio.h"
#include "hardware/structs/iobank0.h"

digitalio_digitalinout_obj_t enable_pin_obj;

// Button pin definitions for Badger 2040
#define BUTTON_DOWN_PIN  11
#define BUTTON_A_PIN     12
#define BUTTON_B_PIN     13
#define BUTTON_C_PIN     14
#define BUTTON_UP_PIN    15
#define BUTTON_USER_PIN  23
#define ENABLE_3V3_PIN   10

// Mask of all front button pins
#define BUTTON_MASK ((1 << BUTTON_DOWN_PIN) | (1 << BUTTON_A_PIN) | \
                     (1 << BUTTON_B_PIN) | (1 << BUTTON_C_PIN) | \
                     (1 << BUTTON_UP_PIN) | (1 << BUTTON_USER_PIN))

// Static storage for wake button state
static volatile uint32_t wake_button_state = 0;
static volatile bool wake_state_captured = false;

// Pin definitions
#define LED_PIN 25

// Forward declarations to satisfy -Wmissing-prototypes
static void preinit_badger_wake(void) __attribute__((constructor(101)));
static void preinit_badger_led_off(void) __attribute__((constructor(65535)));

// =============================================================================
// CRITICAL: This function runs BEFORE main() via constructor attribute!
// This is the key to fast button wake detection.
// Priority 101 = runs very early
// =============================================================================
static void preinit_badger_wake(void) {
    // Immediately latch power on using direct register writes
    // (SDK functions may not be fully initialized yet)
    sio_hw->gpio_oe_set = 1u << ENABLE_3V3_PIN;
    sio_hw->gpio_set = 1u << ENABLE_3V3_PIN;
    
    // Configure button pins as inputs with pull-downs using direct register access
    // This is faster than SDK functions and works before full init
    const uint8_t button_pins[] = {
        BUTTON_DOWN_PIN, BUTTON_A_PIN, BUTTON_B_PIN,
        BUTTON_C_PIN, BUTTON_UP_PIN
    };
    
    for (size_t i = 0; i < sizeof(button_pins); i++) {
        uint8_t pin = button_pins[i];
        // Set as input
        sio_hw->gpio_oe_clr = 1u << pin;
        // Enable pull-down via pads register
        pads_bank0_hw->io[pin] = PADS_BANK0_GPIO0_IE_BITS | PADS_BANK0_GPIO0_PDE_BITS;
        // Set GPIO function
        iobank0_hw->io[pin].ctrl = 5;  // SIO function
    }
    
    // Small delay for pins to settle (just a few cycles)
    for (volatile int i = 0; i < 100; i++) {
        __asm volatile ("nop");
    }
    
    // Capture button states NOW - before anything else runs
    wake_button_state = sio_hw->gpio_in & BUTTON_MASK;
    wake_state_captured = true;
    
    // If woken by a button, turn on LED immediately for user feedback
    // DEBUG: Always turn on LED to verify constructor runs
    iobank0_hw->io[LED_PIN].ctrl = 5;  // SIO function
    sio_hw->gpio_oe_set = 1u << LED_PIN;
    sio_hw->gpio_set = 1u << LED_PIN;
}

// =============================================================================
// Turn off LED just before main() starts
// Priority 65535 = runs as late as possible before main()
// =============================================================================
static void preinit_badger_led_off(void) {
    // Turn off LED - CircuitPython is about to start
    sio_hw->gpio_clr = 1u << LED_PIN;
}

#define DELAY 0x80

enum reg {
    PSR      = 0x00,
    PWR      = 0x01,
    POF      = 0x02,
    PFS      = 0x03,
    PON      = 0x04,
    PMES     = 0x05,
    BTST     = 0x06,
    DSLP     = 0x07,
    DTM1     = 0x10,
    DSP      = 0x11,
    DRF      = 0x12,
    DTM2     = 0x13,
    LUT_VCOM = 0x20,
    LUT_WW   = 0x21,
    LUT_BW   = 0x22,
    LUT_WB   = 0x23,
    LUT_BB   = 0x24,
    PLL      = 0x30,
    TSC      = 0x40,
    TSE      = 0x41,
    TSR      = 0x43,
    TSW      = 0x42,
    CDI      = 0x50,
    LPD      = 0x51,
    TCON     = 0x60,
    TRES     = 0x61,
    REV      = 0x70,
    FLG      = 0x71,
    AMV      = 0x80,
    VV       = 0x81,
    VDCS     = 0x82,
    PTL      = 0x90,
    PTIN     = 0x91,
    PTOU     = 0x92,
    PGM      = 0xa0,
    APG      = 0xa1,
    ROTP     = 0xa2,
    CCSET    = 0xe0,
    PWS      = 0xe3,
    TSSET    = 0xe5
};

enum PSR_FLAGS {
    RES_96x230   = 0b00000000,
    RES_96x252   = 0b01000000,
    RES_128x296  = 0b10000000,
    RES_160x296  = 0b11000000,

    LUT_OTP      = 0b00000000,
    LUT_REG      = 0b00100000,

    FORMAT_BWR   = 0b00000000,
    FORMAT_BW    = 0b00010000,

    SCAN_DOWN    = 0b00000000,
    SCAN_UP      = 0b00001000,

    SHIFT_LEFT   = 0b00000000,
    SHIFT_RIGHT  = 0b00000100,

    BOOSTER_OFF  = 0b00000000,
    BOOSTER_ON   = 0b00000010,

    RESET_SOFT   = 0b00000000,
    RESET_NONE   = 0b00000001
};

enum PWR_FLAGS_1 {
    VDS_EXTERNAL = 0b00000000,
    VDS_INTERNAL = 0b00000010,

    VDG_EXTERNAL = 0b00000000,
    VDG_INTERNAL = 0b00000001
};

enum PWR_FLAGS_2 {
    VCOM_VD      = 0b00000000,
    VCOM_VG      = 0b00000100,

    VGHL_16V     = 0b00000000,
    VGHL_15V     = 0b00000001,
    VGHL_14V     = 0b00000010,
    VGHL_13V     = 0b00000011
};

enum BOOSTER_FLAGS {
    START_10MS = 0b00000000,
    START_20MS = 0b01000000,
    START_30MS = 0b10000000,
    START_40MS = 0b11000000,

    STRENGTH_1 = 0b00000000,
    STRENGTH_2 = 0b00001000,
    STRENGTH_3 = 0b00010000,
    STRENGTH_4 = 0b00011000,
    STRENGTH_5 = 0b00100000,
    STRENGTH_6 = 0b00101000,
    STRENGTH_7 = 0b00110000,
    STRENGTH_8 = 0b00111000,

    OFF_0_27US = 0b00000000,
    OFF_0_34US = 0b00000001,
    OFF_0_40US = 0b00000010,
    OFF_0_54US = 0b00000011,
    OFF_0_80US = 0b00000100,
    OFF_1_54US = 0b00000101,
    OFF_3_34US = 0b00000110,
    OFF_6_58US = 0b00000111
};

enum PFS_FLAGS {
    FRAMES_1   = 0b00000000,
    FRAMES_2   = 0b00010000,
    FRAMES_3   = 0b00100000,
    FRAMES_4   = 0b00110000
};

enum TSE_FLAGS {
    TEMP_INTERNAL = 0b00000000,
    TEMP_EXTERNAL = 0b10000000,

    OFFSET_0      = 0b00000000,
    OFFSET_1      = 0b00000001,
    OFFSET_2      = 0b00000010,
    OFFSET_3      = 0b00000011,
    OFFSET_4      = 0b00000100,
    OFFSET_5      = 0b00000101,
    OFFSET_6      = 0b00000110,
    OFFSET_7      = 0b00000111,

    OFFSET_MIN_8  = 0b00001000,
    OFFSET_MIN_7  = 0b00001001,
    OFFSET_MIN_6  = 0b00001010,
    OFFSET_MIN_5  = 0b00001011,
    OFFSET_MIN_4  = 0b00001100,
    OFFSET_MIN_3  = 0b00001101,
    OFFSET_MIN_2  = 0b00001110,
    OFFSET_MIN_1  = 0b00001111
};

enum PLL_FLAGS {
    HZ_29      = 0b00111111,
    HZ_33      = 0b00111110,
    HZ_40      = 0b00111101,
    HZ_50      = 0b00111100,
    HZ_67      = 0b00111011,
    HZ_100     = 0b00111010,
    HZ_200     = 0b00111001
};

const uint8_t display_start_sequence[] = {
    PWR, 5, VDS_INTERNAL | VDG_INTERNAL, VCOM_VD | VGHL_16V, 0b101011, 0b101011, 0b101011,
    PON, DELAY, 200,
    BTST, 3, (START_10MS | STRENGTH_3 | OFF_6_58US), (START_10MS | STRENGTH_3 | OFF_6_58US), (START_10MS | STRENGTH_3 | OFF_6_58US),
    PSR, 1, (RES_128x296 | LUT_REG | FORMAT_BW | SCAN_UP | SHIFT_RIGHT | BOOSTER_ON | RESET_NONE),
    PFS, 1, FRAMES_1,
    TSE, 1, TEMP_INTERNAL | OFFSET_0,
    TCON, 1, 0x22,
    CDI, 1, 0b01001100,
    PLL, 1, HZ_100,

    LUT_VCOM, 44,
    0x00, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x8c, 0x8c, 0x00, 0x00, 0x04,
    0x00, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00,

    LUT_WW, 42,
    0x54, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x60, 0x8c, 0x8c, 0x00, 0x00, 0x04,
    0xa8, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,

    LUT_BW, 42,
    0x54, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x60, 0x8c, 0x8c, 0x00, 0x00, 0x04,
    0xa8, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,

    LUT_WB, 42,
    0xa8, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x60, 0x8c, 0x8c, 0x00, 0x00, 0x04,
    0x54, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,

    LUT_BB, 42,
    0xa8, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x60, 0x8c, 0x8c, 0x00, 0x00, 0x04,
    0x54, 0x64, 0x64, 0x37, 0x00, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
};

const uint8_t display_stop_sequence[] = {
    POF, 0x00
};

const uint8_t refresh_sequence[] = {
    DRF, 0x00
};

// Public functions for the badger2040 Python module
uint32_t board_get_wake_button_state(void) {
    return wake_button_state;
}

bool board_woken_by_button(void) {
    return wake_button_state != 0;
}

bool board_button_pressed_to_wake(uint8_t pin) {
    return (wake_button_state & (1 << pin)) != 0;
}

bool board_button_wake_get_once(uint8_t pin) {
    uint32_t mask = 1 << pin;
    bool was_pressed = (wake_button_state & mask) != 0;
    wake_button_state &= ~mask;
    return was_pressed;
}

void board_clear_wake_button_state(void) {
    wake_button_state = 0;
}

void board_halt(void) {
    common_hal_digitalio_digitalinout_set_value(&enable_pin_obj, false);
}

void board_init(void) {
    // Note: preinit_badger_wake() has already run before main()!
    // Power is already latched and button state is already captured.
    
    // Set up the enable pin through CircuitPython's HAL for later use
    enable_pin_obj.base.type = &digitalio_digitalinout_type;
    common_hal_digitalio_digitalinout_construct(&enable_pin_obj, &pin_GPIO10);
    common_hal_digitalio_digitalinout_switch_to_output(&enable_pin_obj, true, DRIVE_MODE_PUSH_PULL);
    common_hal_digitalio_digitalinout_never_reset(&enable_pin_obj);

    // Set up the SPI object used to control the display
    fourwire_fourwire_obj_t *bus = &allocate_display_bus()->fourwire_bus;
    busio_spi_obj_t *spi = &bus->inline_bus;
    common_hal_busio_spi_construct(spi, &pin_GPIO18, &pin_GPIO19, &pin_GPIO16, false);
    common_hal_busio_spi_never_reset(spi);

    bus->base.type = &fourwire_fourwire_type;
    common_hal_fourwire_fourwire_construct(bus,
        spi,
        &pin_GPIO20,
        &pin_GPIO17,
        &pin_GPIO21,
        1200000,
        0,
        0);

    epaperdisplay_epaperdisplay_obj_t *display = &allocate_display()->epaper_display;
    display->base.type = &epaperdisplay_epaperdisplay_type;

    epaperdisplay_construct_args_t args = EPAPERDISPLAY_CONSTRUCT_ARGS_DEFAULTS;
    args.bus = bus;
    args.start_sequence = display_start_sequence;
    args.start_sequence_len = sizeof(display_start_sequence);
    args.stop_sequence = display_stop_sequence;
    args.stop_sequence_len = sizeof(display_stop_sequence);
    args.width = 296;
    args.height = 128;
    args.ram_width = 160;
    args.ram_height = 296;
    args.rotation = 270;
    args.write_black_ram_command = DTM2;
    args.black_bits_inverted = true;
    args.write_color_ram_command = DTM1;
    args.refresh_sequence = refresh_sequence;
    args.refresh_sequence_len = sizeof(refresh_sequence);
    args.refresh_time = 1.0;
    args.busy_pin = &pin_GPIO26;
    args.seconds_per_frame = 2.0;
    common_hal_epaperdisplay_epaperdisplay_construct(display, &args);
}

void board_deinit(void) {
    epaperdisplay_epaperdisplay_obj_t *display = &displays[0].epaper_display;
    if (display->base.type == &epaperdisplay_epaperdisplay_type) {
        while (common_hal_epaperdisplay_epaperdisplay_get_busy(display)) {
            RUN_BACKGROUND_TASKS;
        }
    }
    common_hal_displayio_release_displays();
}